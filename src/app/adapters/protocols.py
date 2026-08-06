"""어댑터 계약.

소비자가 생기는 change 에서만 정의한다 — 미리 정한 인터페이스는 사용처가 생기는 순간
거의 틀린 것으로 드러난다. 값 객체는 `core/` 에 둔다 (`ARCHITECTURE.md` 어댑터 계약).
"""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from app.core.cache import CachedAnswer, CacheLookup
from app.core.documents import (
    Chunk,
    Document,
    DocumentFormat,
    ExtractedDocument,
    StoredIndexVersion,
)
from app.core.models import ProbeResult
from app.core.retrieval import RetrievedChunk


@runtime_checkable
class HealthProbe(Protocol):
    """의존성 하나의 도달 가능 여부를 보고한다.

    예외를 던져도 서비스가 잡지만, 판별 가능한 실패는 `ProbeResult` 쪽이 사유가 구체적이다."""

    name: str

    async def check(self) -> ProbeResult: ...


@runtime_checkable
class DocumentParser(Protocol):
    """업로드된 바이트에서 텍스트를 추출한다.

    동기다 — 호출부가 스레드풀 오프로드를 의식하게 만든다 (`ARCHITECTURE.md` 문서 파서)."""

    formats: frozenset[DocumentFormat]

    def parse(self, data: bytes) -> ExtractedDocument: ...


@runtime_checkable
class Embedder(Protocol):
    """텍스트를 벡터로 만든다.

    역할 접두사를 어댑터에 가두고 `signature` 로만 색인 서명 재료를 꺼낸다 (`ARCHITECTURE.md`)."""

    dimension: int
    max_input_tokens: int
    signature: str

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    def count_document_tokens(self, text: str) -> int: ...

    def count_query_tokens(self, text: str) -> int: ...

    async def warm_up(self) -> None: ...


@runtime_checkable
class VectorStore(Protocol):
    """청크와 벡터를 보관하고, 벡터 하나에 가까운 청크를 돌려준다.

    `query` 가 벡터를 받는 것은 저장소를 임베딩 모델에서 떼어 놓기 위해서다 (`ARCHITECTURE.md`)."""

    async def add_chunks(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        *,
        filename: str,
        document_format: DocumentFormat,
    ) -> None: ...

    async def delete_document(
        self,
        document_id: str,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int:
        """조건에 맞는 청크를 지우고 지운 개수를 돌려준다.

        축을 하나만 좁히는 경로가 필요하다 — 재색인은 `revision` 이 그대로인 채 일어난다."""
        ...

    async def count_chunks(
        self,
        document_id: str | None = None,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int: ...

    async def list_stored_versions(self) -> list[StoredIndexVersion]:
        """저장된 삼중항 전체. 레지스트리만 보면 잔여 청크의 존재를 알 수 없다."""
        ...

    async def backfill_version_keys(self) -> int:
        """대상 집합 필터가 읽는 형태로 저장되지 않은 청크를 보정하고 그 개수를 돌려준다.

        보정되지 않은 청크는 저장소에 있으면서 검색되지 않는다 — 오류가 아니라 부재로 보인다."""
        ...

    async def query(
        self,
        embedding: Sequence[float],
        *,
        top_k: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[RetrievedChunk]:
        """`versions` 의 청크 중 가까운 것부터 최대 `top_k` 개, 코사인 유사도 내림차순.

        모자란 자리를 채우지 않고, `versions` 가 비면 저장소를 건드리지 않는다."""
        ...


@runtime_checkable
class LexicalIndex(Protocol):
    """청크 본문을 글자 그대로 찾는 색인. 벡터 스토어와 같은 삼중항 축을 쓴다.

    축이 어긋나면 수집이 두 색인을 한 순서로 다룰 수 없고, 재색인에서 한쪽만 지워진다."""

    async def add_chunks(
        self,
        chunks: Sequence[Chunk],
        *,
        filename: str,
        document_format: DocumentFormat,
    ) -> None: ...

    async def delete_document(
        self,
        document_id: str,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int: ...

    async def count_chunks(
        self,
        document_id: str | None = None,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int: ...

    async def list_stored_versions(self) -> list[StoredIndexVersion]: ...

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[RetrievedChunk]:
        """`versions` 의 청크 중 어휘가 겹치는 것부터 최대 `top_k` 개, 점수 내림차순.

        점수 척도가 벡터 쪽과 달라 `RetrievedChunk` 다. `versions` 가 비면 빈 목록이다."""
        ...


@runtime_checkable
class Retriever(Protocol):
    """질의 하나에 대해 자기 척도의 ranked list 를 돌려준다.

    자기 이름도 가중치도 모른다 — 그것은 설정이 정하고 검색 서비스가 들고 있다."""

    async def retrieve(
        self,
        query: str,
        *,
        depth: int,
        versions: Sequence[StoredIndexVersion],
        embedding: Sequence[float] | None = None,
    ) -> list[RetrievedChunk]:
        """`versions` 의 청크 중 관련도가 높은 것부터 최대 `depth` 개, 점수 내림차순.

        하한은 자기 단위로 판정한다. `embedding` 은 벡터를 쓰지 않는 retriever 가 무시한다."""
        ...

    async def query_vector(self, query: str) -> list[float] | None:
        """이 retriever 가 쓰는 질의 벡터. 벡터를 쓰지 않으면 `None` 이고 임베더도 안 부른다.

        검색 서비스가 요청당 한 번 부르고 그 결과를 모두에게 넘긴다 (`retrieval` 스펙)."""
        ...


@runtime_checkable
class Reranker(Protocol):
    """질의와 후보 본문을 함께 읽어 후보별 관련도를 매긴다.

    정렬도 절단도 하지 않는다 — 순서를 만드는 규칙은 `core/reranking.py` 에 있다."""

    name: str
    #: 캐시 항목의 정체성 재료. 가중치를 올리지 않고 읽힌다 (`reranking`).
    signature: str

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """후보와 같은 개수·같은 순서의 점수. 클수록 그 질의에 더 잘 답한다.

        척도가 질의마다 달라 같은 질의 안에서만 비교된다. 빈 목록이면 모델을 부르지 않는다."""
        ...

    async def warm_up(self) -> None: ...


@runtime_checkable
class AnswerGenerator(Protocol):
    """프롬프트 문자열 하나를 받아 답변 조각을 흘려보낸다.

    조각을 쪼개지도 합치지도 않고, 취소는 순회 종료로 표현한다 (`ARCHITECTURE.md`)."""

    def generate(self, prompt: str, *, timeout_seconds: float) -> AsyncIterator[str]: ...


@runtime_checkable
class ResponseCache(Protocol):
    """답변 한 건을 질의 지문에 맡아 두고, 문서 변경에 맞춰 걷어낸다.

    유사 매치에 "가장 가까운 하나"만 요구해 저장소를 갈아도 소비처가 흔들리지 않는다."""

    async def lookup_exact(self, fingerprint: str) -> CacheLookup: ...

    async def count_candidates(self, scope: str, *, polarity: bool) -> int:
        """유사 매치가 볼 후보 수. 0 이면 호출부가 질의 임베딩을 만들지 않는다.

        이 값을 묻지 않으면 캐시가 빈 상태에서도 미스마다 임베딩이 한 번 더 돈다 (결정 10)."""
        ...

    async def lookup_semantic(
        self,
        embedding: Sequence[float],
        *,
        scope: str,
        polarity: bool,
        threshold: float,
        candidates: int,
    ) -> CacheLookup:
        """같은 `scope`·같은 극성의 후보 `candidates` 개 중 임계값을 넘는 가장 가까운 항목.

        극성이 다른 항목은 유사도와 무관하게 후보에서 빠진다 (`response-cache`)."""
        ...

    async def store(
        self,
        fingerprint: str,
        entry: CachedAnswer,
        *,
        scope: str,
        polarity: bool,
        embedding: Sequence[float],
        negative: bool,
    ) -> None:
        """항목 하나를 맡긴다. 저장이 거부되는 경로는 없다 — 상한은 오래된 것을 밀어낸다.

        무효화 태그는 항목이 스스로 알고(`document_ids`), 극성과 부정 판정은 호출부가 정한다."""
        ...

    async def invalidate_document(self, document_id: str) -> int:
        """그 문서를 근거로 삼은 항목을 지우고 지운 개수를 돌려준다."""
        ...

    async def invalidate_negative(self) -> int:
        """근거 없음·답변 불가로 끝난 항목을 지우고 지운 개수를 돌려준다.

        코퍼스 내용이 바뀌면 "어디에도 없다"는 주장이 반증되기 때문이다 (design 결정 4)."""
        ...

    async def discard(self, fingerprint: str) -> None:
        """항목 하나를 지운다 — 재검증에서 버린 항목이 요청마다 비용을 다시 물지 않게."""
        ...


@runtime_checkable
class DocumentRegistry(Protocol):
    """문서의 지금 유효한 리비전에 대한 단일 답.

    벡터 스토어 메타데이터에서 유도하지 않는다 — 교체 도중에는 답이 둘이 된다."""

    async def get(self, document_id: str) -> Document | None: ...

    async def list_all(self) -> list[Document]: ...

    async def commit(self, document: Document) -> None: ...

    async def delete(self, document_id: str) -> Document | None:
        """지우고 지워진 레코드를 돌려준다. 없었으면 `None`.

        레코드를 돌려주는 이유는 따로 조회하면 그 사이에 값이 바뀌기 때문이다."""
        ...
