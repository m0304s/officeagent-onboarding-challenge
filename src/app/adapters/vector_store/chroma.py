"""Chroma 벡터 스토어 — 쓰기·삭제·집계.

**질의(`query`)는 없다.** retrieval change 가 자기 요구를 알고 나서 붙인다. 지금 정하면
top-k·필터·거리 지표를 근거 없이 추측하게 되고, 그 추측이 문서에 적히는 순간 고치는 데
설명이 붙는다.

임베디드 퍼시스턴트 모드라 별도 컨테이너가 없다. "도달 가능"의 의미가 네트워크가 아니라
**저장 경로 접근**이며, 그 점검은 `vector_store/probe.py` 가 담당한다.
"""

import asyncio
import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.core.documents import Chunk, DocumentFormat, StoredIndexVersion
from app.core.exceptions import StorageUnavailable

logger = logging.getLogger(__name__)

#: 컬렉션 이름. 문서 청크가 들어가는 유일한 컬렉션이다.
DEFAULT_COLLECTION = "document_chunks"


class ChromaVectorStore:
    """청크와 벡터를 임베디드 Chroma 컬렉션 하나에 보관한다."""

    def __init__(self, path: Path, *, collection_name: str = DEFAULT_COLLECTION) -> None:
        self._path = Path(path)
        self._collection_name = collection_name
        self._collection: Any = None
        # 클라이언트 초기화가 여러 워커 스레드에서 동시에 일어날 수 있다.
        self._init_lock = threading.Lock()

    # ── 쓰기 ────────────────────────────────────────────────────────────

    async def add_chunks(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        *,
        filename: str,
        document_format: DocumentFormat,
    ) -> None:
        """청크와 벡터를 함께 기록한다.

        **원자적이지 않다.** 배치 쓰기에 트랜잭션이 없어, 도중에 실패하면 앞부분이
        기록된 채로 남는다. 그 되돌리기는 교체 순서를 아는 서비스가 책임진다.
        """
        if len(chunks) != len(embeddings):
            # 조용히 zip 으로 자르면 청크 일부가 벡터 없이 사라지고, 그 사실이
            # `chunk_count` 와 저장된 개수의 불일치로만 뒤늦게 드러난다.
            raise ValueError(f"청크 수({len(chunks)})와 벡터 수({len(embeddings)})가 다르다")
        if not chunks:
            return
        await self._offload(self._add_chunks, chunks, embeddings, filename, document_format)

    async def delete_document(
        self,
        document_id: str,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int:
        return await self._offload(self._delete, document_id, revision, index_signature)

    # ── 조회 ────────────────────────────────────────────────────────────

    async def count_chunks(
        self,
        document_id: str | None = None,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int:
        return await self._offload(self._count, document_id, revision, index_signature)

    async def list_stored_versions(self) -> list[StoredIndexVersion]:
        return await self._offload(self._list_versions)

    # ── 블로킹 구현 (스레드풀에서만 실행된다) ───────────────────────────

    def _add_chunks(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        filename: str,
        document_format: DocumentFormat,
    ) -> None:
        self._get_collection().add(
            ids=[chunk.id for chunk in chunks],
            embeddings=[list(vector) for vector in embeddings],
            documents=[chunk.text for chunk in chunks],
            metadatas=[_metadata(chunk, filename, document_format) for chunk in chunks],
        )

    def _delete(self, document_id: str, revision: str | None, index_signature: str | None) -> int:
        collection = self._get_collection()
        where = _where(document_id, revision, index_signature)
        # `delete(where=...)` 는 지운 개수를 알려주지 않는다. id 를 먼저 모아 두면
        # 호출자가 "정말 지워졌는가"를 응답으로 확인할 수 있다.
        ids = collection.get(where=where, include=[])["ids"]
        if not ids:
            return 0
        collection.delete(ids=ids)
        return len(ids)

    def _count(
        self, document_id: str | None, revision: str | None, index_signature: str | None
    ) -> int:
        collection = self._get_collection()
        where = _where(document_id, revision, index_signature)
        if where is None:
            return collection.count()
        return len(collection.get(where=where, include=[])["ids"])

    def _list_versions(self) -> list[StoredIndexVersion]:
        """저장된 조합 전체를 메타데이터에서 모은다.

        전수 조회다. 컬렉션이 커지면 비싸지지만, 부르는 곳이 **기동 정리 한 번**뿐이라
        인덱스를 따로 두지 않는다. 주기적으로 돌리게 되면 그때가 재고 시점이다.
        """
        stored = self._get_collection().get(include=["metadatas"])
        versions = {
            (
                metadata["document_id"],
                metadata["revision"],
                metadata["index_signature"],
            )
            for metadata in stored["metadatas"] or ()
        }
        return [
            StoredIndexVersion(
                document_id=document_id, revision=revision, index_signature=signature
            )
            for document_id, revision, signature in sorted(versions)
        ]

    # ── 클라이언트 ──────────────────────────────────────────────────────

    def _get_collection(self) -> Any:
        """블로킹. 스레드풀에서만 호출한다."""
        if self._collection is not None:
            return self._collection
        with self._init_lock:
            if self._collection is None:
                self._collection = self._open_collection()
        return self._collection

    def _open_collection(self) -> Any:
        # import 비용이 커서 모듈 최상단이 아니라 여기서 가져온다. 앱 기동을 느리게 하지 않는다.
        import chromadb

        self._path.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(self._path))
        return client.get_or_create_collection(
            name=self._collection_name,
            # 벡터를 항상 정규화해 넣으므로 코사인이 자연스러운 지표다. 값을 명시해
            # 두지 않으면 라이브러리 기본값이 바뀔 때 거리 의미가 조용히 달라진다.
            metadata={"hnsw:space": "cosine"},
            # **임베딩은 우리가 만들어 넣는다.** 비워 두면 라이브러리가 자기 기본
            # 모델을 쓰려고 가중치를 받으러 나가고, 그러면 색인 서명이 가리키는
            # 모델과 실제로 벡터를 만든 모델이 어긋난다.
            embedding_function=None,
        )

    async def _offload(self, operation, *args):
        """파일 I/O 를 이벤트 루프 밖으로 내보내고, 실패를 도메인 예외로 바꾼다.

        라이브러리 예외가 라우터까지 새면 계층 경계가 무의미해지고 내부 메시지가
        응답에 노출된다.
        """
        try:
            return await asyncio.to_thread(operation, *args)
        except Exception as exc:
            logger.warning("벡터 스토어 접근 실패", exc_info=exc)
            raise StorageUnavailable("벡터 스토어에 접근할 수 없습니다") from exc


def _metadata(chunk: Chunk, filename: str, document_format: DocumentFormat) -> dict[str, Any]:
    """청크 하나의 메타데이터.

    `page` 는 값이 있을 때만 키를 넣는다. 임베디드 벡터 스토어가 널 메타데이터를
    허용하지 않고, 센티널 값(-1 같은)을 쓰면 소비자가 그 규약을 알아야 한다.
    """
    metadata: dict[str, Any] = {
        "document_id": chunk.document_id,
        "revision": chunk.revision,
        "index_signature": chunk.index_signature,
        "filename": filename,
        "format": document_format.value,
        "chunk_index": chunk.chunk_index,
        "char_start": chunk.location.char_start,
        "char_end": chunk.location.char_end,
    }
    if chunk.location.page is not None:
        metadata["page"] = chunk.location.page
    return metadata


def _where(
    document_id: str | None, revision: str | None, index_signature: str | None
) -> dict[str, Any] | None:
    """조건을 Chroma 필터로 옮긴다. 조건이 없으면 `None`(= 전체)."""
    conditions = [
        {field: value}
        for field, value in (
            ("document_id", document_id),
            ("revision", revision),
            ("index_signature", index_signature),
        )
        if value is not None
    ]
    if not conditions:
        return None
    # 조건이 둘 이상이면 명시적 `$and` 가 필요하다.
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}
