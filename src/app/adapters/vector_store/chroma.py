"""Chroma 벡터 스토어 — 쓰기·삭제·집계·질의.

**거리 지표는 이 파일 밖으로 나가지 않는다.** Chroma 는 코사인 *거리*(작을수록 가깝다)를
돌려주는데, 그 방향이 상위 계층까지 새면 저장소를 바꿔 지표가 달라지는 순간 임계값 비교가
조용히 뒤집힌다 — 타입은 그대로라 어디서도 오류가 나지 않는다. 그래서 여기서 유사도로
바꿔 내보낸다.

**Chroma 서버 모드**로 접속한다(별도 컨테이너). 그래서 "도달 가능"의 의미가 네트워크
도달이며, 그 점검은 `vector_store/probe.py` 가 담당한다.

파이썬 클라이언트는 동기 HTTP 라 호출이 블로킹이다. 모든 연산을 스레드풀로 내보내는 이유가
그것이다 — 네트워크 왕복이 이벤트 루프 위에서 일어나면 수집 한 건이 헬스 응답까지 세운다.
"""

import asyncio
import logging
import threading
from collections.abc import Sequence
from typing import Any

from app.adapters.vector_store.client import ChromaEndpoint, create_client, parse_url
from app.core.documents import Chunk, ChunkLocation, DocumentFormat, StoredIndexVersion
from app.core.exceptions import StorageUnavailable
from app.core.retrieval import ScoredChunk

logger = logging.getLogger(__name__)

#: 컬렉션 이름의 앞부분. 뒤에 벡터 차원이 붙는다 (`collection_for`).
COLLECTION_PREFIX = "document_chunks"

#: 차원을 모르는 호출자(테스트·수동 점검)를 위한 기본값.
DEFAULT_COLLECTION = COLLECTION_PREFIX


def collection_for(dimension: int) -> str:
    """벡터 차원마다 컬렉션을 나눈다.

    **Chroma 는 컬렉션 하나에 차원 하나만 허용하고, 그 차원은 컬렉션을 비운 뒤에도
    남는다.** 실측: 4차원으로 쓴 컬렉션에서 모든 청크를 지운 뒤 8차원을 넣으면
    `Collection expecting embedding with dimension of 4, got 8` 로 거절된다.

    이름을 나누지 않으면 **차원이 다른 모델로 바꾼 뒤 복구가 불가능해진다.** 기동 정리가
    구 서명 청크를 지우고 `stale` 로 표시해도, 재업로드가 같은 컬렉션에 새 차원을 넣으려다
    영구히 실패한다 — "재업로드하면 복구된다"는 약속이 그 순간 거짓이 된다.

    차원이 같은 두 모델은 컬렉션을 공유한다. 그건 문제가 되지 않는다 — 모델이 달라지면
    `index_signature` 가 달라지고, 검색과 삭제가 그 값으로 걸러내기 때문이다. 여기서
    나누는 것은 **Chroma 가 강제하는 제약 하나**뿐이다.
    """
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

    async def query(
        self,
        embedding: Sequence[float],
        *,
        top_k: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[ScoredChunk]:
        """대상 삼중항으로 좁힌 뒤 벡터에 가까운 청크부터 돌려준다."""
        # 빈 목록은 **대상 없음**이다. 저장소 필터 API 에서 "조건 없음 = 전체"가 흔한
        # 관습이라, 여기서 `where=None` 으로 흘려보내면 문서가 하나도 유효하지 않을 때
        # 검색이 전체 탐색으로 뒤집힌다 — 잔여 청크만 남은 저장소에서 사용자가 지운
        # 문서가 검색된다. 저장소를 아예 건드리지 않는다.
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

    def _query(
        self,
        embedding: list[float],
        top_k: int,
        versions: tuple[StoredIndexVersion, ...],
    ) -> list[ScoredChunk]:
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
            _scored_chunk(metadata, text, distance)
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
            # **임베딩은 우리가 만들어 넣는다.** 비워 두면 라이브러리가 자기 기본
            # 모델을 쓰려고 가중치를 받으러 나가고, 그러면 색인 서명이 가리키는
            # 모델과 실제로 벡터를 만든 모델이 어긋난다.
            embedding_function=None,
        )

    async def _offload(self, operation, *args):
        """네트워크 I/O 를 이벤트 루프 밖으로 내보내고, 실패를 도메인 예외로 바꾼다.

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

    `page` 는 값이 있을 때만 키를 넣는다. Chroma 가 널 메타데이터를 허용하지 않고,
    센티널 값(-1 같은)을 쓰면 소비자가 그 규약을 알아야 한다.
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


def _first_batch(response: dict[str, Any], key: str) -> list[Any]:
    """질의 응답에서 첫 묶음을 꺼낸다. 없으면 빈 목록.

    Chroma 는 질의 벡터마다 한 묶음씩 돌려주고, 요청하지 않은 필드는 `None` 이다.
    벡터를 하나만 보내므로 묶음도 하나다.
    """
    batches = response.get(key) or []
    return list(batches[0]) if batches else []


def _scored_chunk(metadata: dict[str, Any], text: str, distance: float) -> ScoredChunk:
    """응답 한 줄을 결과 값 객체로 옮긴다.

    `page` 는 **없을 수 있다** — 쪽 개념이 없는 포맷(txt·md)은 저장 시 키 자체를 넣지
    않는다(`_metadata`). 그 규약의 반대편이 여기다.
    """
    return ScoredChunk(
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
        score=_similarity(distance),
    )


def _similarity(distance: float) -> float:
    """코사인 거리(작을수록 가깝다)를 `[0, 1]` 유사도(클수록 가깝다)로 바꾼다.

    클램프하는 이유는 하한 설정의 정의역을 `[0, 1]` 로 고정하기 위해서다. 정규화된
    벡터의 코사인 유사도는 이론상 음수가 될 수 있지만 현재 임베딩에서 실제로 관측되지
    않는다. **클램프가 그 사실을 숨기므로** 실제로 발동하면 경고를 남긴다 — 조용히 0 으로
    만들면 모델이 바뀌어 점수 분포가 이동했다는 신호가 사라진다.
    """
    similarity = 1.0 - distance
    if not 0.0 <= similarity <= 1.0:
        logger.warning(
            "유사도가 [0, 1] 밖이라 잘라냈습니다 — 점수 분포가 이동했을 수 있습니다",
            extra={"distance": distance, "similarity": similarity},
        )
        return min(1.0, max(0.0, similarity))
    return similarity


def _version_filter(versions: Sequence[StoredIndexVersion]) -> dict[str, Any]:
    """대상 삼중항 목록을 Chroma 필터로 조립한다.

    삼중항 하나가 `$and` 셋이고, 여럿이면 그것들을 `$or` 로 묶는다. `$or` 는 피연산자가
    둘 이상일 때만 유효하므로 하나짜리는 `$and` 를 그대로 쓴다.

    **호출자가 빈 목록을 걸러 준다**(`query`). 여기서 빈 필터를 만들면 그것이 곧 전체
    검색이라 계약이 뒤집힌다.
    """
    clauses = [
        {
            "$and": [
                {"document_id": version.document_id},
                {"revision": version.revision},
                {"index_signature": version.index_signature},
            ]
        }
        for version in versions
    ]
    return clauses[0] if len(clauses) == 1 else {"$or": clauses}


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
