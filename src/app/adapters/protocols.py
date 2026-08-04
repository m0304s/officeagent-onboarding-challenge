"""어댑터 계약.

소비자가 생기는 change 에서만 정의한다 — 미리 정한 인터페이스는 사용처가 생기는 순간
거의 틀린 것으로 드러난다. 값 객체는 `core/` 에 둔다 (`ARCHITECTURE.md` 어댑터 계약).
"""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

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
    ) -> list[RetrievedChunk]:
        """`versions` 의 청크 중 관련도가 높은 것부터 최대 `depth` 개, 점수 내림차순.

        관련성 하한을 여기서 자기 단위로 건다 — 융합 뒤에는 척도가 사라진다."""
        ...


@runtime_checkable
class AnswerGenerator(Protocol):
    """프롬프트 문자열 하나를 받아 답변 조각을 흘려보낸다.

    조각을 쪼개지도 합치지도 않고, 취소는 순회 종료로 표현한다 (`ARCHITECTURE.md`)."""

    def generate(self, prompt: str, *, timeout_seconds: float) -> AsyncIterator[str]: ...


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
