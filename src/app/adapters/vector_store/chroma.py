"""Chroma 벡터 스토어 — 쓰기·삭제·집계·질의.

거리 지표가 이 파일 밖으로 나가지 않는다 — 새면 저장소를 바꿀 때 임계값 비교가 오류 없이
뒤집힌다. 클라이언트가 동기 HTTP 라 모든 연산을 스레드풀로 내보낸다.
"""

import asyncio
import logging
import threading
from collections.abc import Sequence
from typing import Any

from app.adapters.vector_store.client import ChromaEndpoint, create_client, parse_url
from app.core.documents import Chunk, ChunkLocation, DocumentFormat, StoredIndexVersion
from app.core.exceptions import StorageUnavailable
from app.core.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

#: 컬렉션 이름의 앞부분. 뒤에 벡터 차원이 붙는다 (`collection_for`).
COLLECTION_PREFIX = "document_chunks"

#: 차원을 모르는 호출자(테스트·수동 점검)를 위한 기본값.
DEFAULT_COLLECTION = COLLECTION_PREFIX

#: 대상 집합 필터가 읽는 합성 축. 이 필드가 없는 청크는 검색되지 않아 기동 정리가 채운다.
VERSION_KEY = "version_key"

#: 보정 한 번에 갱신하는 청크 수. 한 요청에 다 담으면 큰 컬렉션에서 서버가 거절한다.
BACKFILL_BATCH = 200


def collection_for(dimension: int) -> str:
    """벡터 차원마다 컬렉션을 나눈다.

    Chroma 는 컬렉션당 차원 하나만 허용하고 그 차원이 컬렉션을 비워도 남기 때문이다."""
    return f"{COLLECTION_PREFIX}_d{dimension}"


class ChromaVectorStore:
    """청크와 벡터를 Chroma 서버의 컬렉션 하나에 보관한다."""

    def __init__(self, url: str, *, collection_name: str = DEFAULT_COLLECTION) -> None:
        # 주소 해석만 지금 한다. 접속은 첫 사용까지 미룬다 — 서버가 떠 있는지는 기동
        # 조건이 아니다. 반면 오타 난 주소는 여기서 기동을 막는다.
        self._endpoint: ChromaEndpoint = parse_url(url)
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

        원자적이지 않다 — 되돌리기는 교체 순서를 아는 서비스가 책임진다."""
        if len(chunks) != len(embeddings):
            # zip 으로 자르면 청크 일부가 벡터 없이 사라지고 뒤늦게만 드러난다.
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

    async def backfill_version_keys(self) -> int:
        """대상 필터가 읽는 합성 축이 없는 청크에 그것을 채우고, 채운 개수를 돌려준다."""
        return await self._offload(self._backfill_version_keys)

    async def query(
        self,
        embedding: Sequence[float],
        *,
        top_k: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[RetrievedChunk]:
        """대상 삼중항으로 좁힌 뒤 벡터에 가까운 청크부터 돌려준다."""
        # 빈 목록은 조건 없음이 아니라 대상 없음이다 — 흘려보내면 잔여 청크만 남은
        # 저장소에서 사용자가 지운 문서가 검색된다.
        if not versions:
            return []
        return await self._offload(self._query, list(embedding), top_k, tuple(versions))

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

        전수 조회지만 부르는 곳이 기동 정리 한 번뿐이라 인덱스를 두지 않는다."""
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

    def _backfill_version_keys(self) -> int:
        """합성 축이 없는 청크를 찾아 그 값만 채운다. 없으면 쓰기를 만들지 않는다.

        전수 조회인 것은 `$exists` 가 없어서다 — `_list_versions` 와 같은 값을 읽는다."""
        collection = self._get_collection()
        stored = collection.get(include=["metadatas"])
        pending = [
            (chunk_id, metadata)
            for chunk_id, metadata in zip(
                stored["ids"], stored["metadatas"] or (), strict=True
            )
            if not metadata.get(VERSION_KEY)
        ]
        for start in range(0, len(pending), BACKFILL_BATCH):
            batch = pending[start : start + BACKFILL_BATCH]
            collection.update(
                ids=[chunk_id for chunk_id, _ in batch],
                # 통째로 다시 쓴다 — 부분 갱신이 나머지 필드를 지우는 구현이 있다.
                metadatas=[
                    {**metadata, VERSION_KEY: _version_key(metadata)} for _, metadata in batch
                ],
            )
        return len(pending)

    def _query(
        self,
        embedding: list[float],
        top_k: int,
        versions: tuple[StoredIndexVersion, ...],
    ) -> list[RetrievedChunk]:
        response = self._get_collection().query(
            query_embeddings=[embedding],
            n_results=top_k,
            where=_version_filter(versions),
            # 거리를 명시적으로 요청한다. 기본 `include` 에 들어 있더라도 값을 적어 두면
            # 라이브러리 기본이 바뀔 때 점수가 조용히 사라지지 않는다.
            include=["metadatas", "documents", "distances"],
        )
        # 질의 벡터 하나를 보냈으므로 결과도 한 묶음이다. 대상이 없으면 빈 묶음이 온다.
        metadatas = _first_batch(response, "metadatas")
        documents = _first_batch(response, "documents")
        distances = _first_batch(response, "distances")
        return [
            _retrieved_chunk(metadata, text, distance)
            for metadata, text, distance in zip(metadatas, documents, distances, strict=True)
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
        logger.info("벡터 스토어에 접속합니다", extra={"vector_store": str(self._endpoint)})
        client = create_client(self._endpoint)
        return client.get_or_create_collection(
            name=self._collection_name,
            # 벡터를 항상 정규화해 넣으므로 코사인이 자연스러운 지표다. 값을 명시해
            # 두지 않으면 라이브러리 기본값이 바뀔 때 거리 의미가 조용히 달라진다.
            metadata={"hnsw:space": "cosine"},
            # 비워 두면 라이브러리가 자기 기본 모델을 받으러 나가, 색인 서명이
            # 가리키는 모델과 실제로 벡터를 만든 모델이 어긋난다.
            embedding_function=None,
        )

    async def _offload(self, operation, *args):
        """네트워크 I/O 를 루프 밖으로 내보내고 실패를 도메인 예외로 바꾼다.

        라이브러리 예외가 새면 내부 메시지가 응답에 노출된다."""
        try:
            return await asyncio.to_thread(operation, *args)
        except Exception as exc:
            logger.warning("벡터 스토어 접근 실패", exc_info=exc)
            raise StorageUnavailable("벡터 스토어에 접근할 수 없습니다") from exc


def _metadata(chunk: Chunk, filename: str, document_format: DocumentFormat) -> dict[str, Any]:
    """청크 하나의 메타데이터.

    `page` 는 있을 때만 키를 넣는다 — 널이 금지고 센티널은 규약을 하나 늘린다."""
    metadata: dict[str, Any] = {
        "document_id": chunk.document_id,
        "revision": chunk.revision,
        "index_signature": chunk.index_signature,
        # 세 축을 합친 값을 함께 둔다. 축별 조건으로 대상 집합을 표현하면 필터의 크기가
        # 문서 수에 비례하는데, 그 필터는 질의마다 새로 조립되고 서버에서 평가된다.
        VERSION_KEY: StoredIndexVersion.of(chunk).key,
        "filename": filename,
        "format": document_format.value,
        "chunk_index": chunk.chunk_index,
        "char_start": chunk.location.char_start,
        "char_end": chunk.location.char_end,
    }
    if chunk.location.page is not None:
        metadata["page"] = chunk.location.page
    return metadata


def _version_key(metadata: dict[str, Any]) -> str:
    """저장된 메타데이터에서 합성 축을 다시 만든다 — 축 셋은 이전 세대에도 들어 있다."""
    return StoredIndexVersion(
        document_id=metadata["document_id"],
        revision=metadata["revision"],
        index_signature=metadata["index_signature"],
    ).key


def _first_batch(response: dict[str, Any], key: str) -> list[Any]:
    """질의 응답에서 첫 묶음을 꺼낸다. 벡터를 하나만 보내므로 묶음도 하나다."""
    batches = response.get(key) or []
    return list(batches[0]) if batches else []


def _retrieved_chunk(metadata: dict[str, Any], text: str, distance: float) -> RetrievedChunk:
    """응답 한 줄을 결과 값 객체로 옮긴다. `page` 부재는 `_metadata` 규약의 반대편이다."""
    return RetrievedChunk(
        document_id=metadata["document_id"],
        revision=metadata["revision"],
        index_signature=metadata["index_signature"],
        chunk_index=metadata["chunk_index"],
        text=text,
        location=ChunkLocation(
            char_start=metadata["char_start"],
            char_end=metadata["char_end"],
            page=metadata.get("page"),
        ),
        filename=metadata["filename"],
        format=DocumentFormat(metadata["format"]),
        native_score=_similarity(distance),
    )


def _similarity(distance: float) -> float:
    """코사인 거리를 `[0, 1]` 유사도로 바꾼다.

    클램프가 발동하면 경고를 남긴다 — 조용히 0 으로 만들면 점수 분포 이동 신호가 사라진다."""
    similarity = 1.0 - distance
    if not 0.0 <= similarity <= 1.0:
        logger.warning(
            "유사도가 [0, 1] 밖이라 잘라냈습니다 — 점수 분포가 이동했을 수 있습니다",
            extra={"distance": distance, "similarity": similarity},
        )
        return min(1.0, max(0.0, similarity))
    return similarity


def _version_filter(versions: Sequence[StoredIndexVersion]) -> dict[str, Any]:
    """대상 삼중항 목록을 Chroma 필터로 조립한다 — 문서가 몇 건이든 조건 하나다.

    빈 목록은 호출자가 걸러 준다 — 여기서 빈 필터를 만들면 그것이 곧 전체 검색이다."""
    return {VERSION_KEY: {"$in": [version.key for version in versions]}}


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
