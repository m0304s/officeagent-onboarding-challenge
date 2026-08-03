"""질의를 받는 두 엔드포인트(`/search`·`/qa`)의 공통 경계.

상한 판정과 근거 뷰가 한 곳에 있어야 두 경로의 거부와 출처가 같은 코드·같은 형식으로
나간다. 갈리면 같은 질의가 경로마다 다른 답을 받는다.
"""

import asyncio
from typing import Annotated

from fastapi import Depends, Request
from pydantic import BaseModel

from app.adapters.protocols import Embedder
from app.config import Settings
from app.core.documents import DocumentFormat
from app.core.exceptions import EmptyQuery, InvalidTopK, QueryTooLong
from app.core.retrieval import ScoredChunk
from app.services.qa import QaService
from app.services.retrieval import RetrievalService


def get_retrieval_service(request: Request) -> RetrievalService:
    """앱 팩토리가 배선한 서비스를 꺼낸다. 모듈 전역 싱글턴을 두지 않는다."""
    return request.app.state.retrieval_service


def get_qa_service(request: Request) -> QaService:
    return request.app.state.qa_service


def get_embedder(request: Request) -> Embedder:
    """토큰 상한 판정에 임베더가 필요하다.

    상한이 모델 선언값이라 설정에 없고, 판정이 임베딩보다 앞서야 한다."""
    return request.app.state.embedder


def get_settings_of(request: Request) -> Settings:
    return request.app.state.settings


RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
QaServiceDep = Annotated[QaService, Depends(get_qa_service)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
SettingsDep = Annotated[Settings, Depends(get_settings_of)]


class SearchResultView(BaseModel):
    """근거 청크 하나 — 정체성·본문·출처·점수.

    출처를 함께 싣는 것은 인용에 문서를 다시 조회하지 않게 하려는 것이다."""

    document_id: str
    filename: str
    format: DocumentFormat
    revision: str
    chunk_index: int
    text: str
    score: float
    char_start: int
    char_end: int
    page: int | None = None

    @classmethod
    def of(cls, chunk: ScoredChunk) -> "SearchResultView":
        return cls(
            document_id=chunk.document_id,
            filename=chunk.filename,
            format=chunk.format,
            revision=chunk.revision,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            score=chunk.score,
            char_start=chunk.location.char_start,
            char_end=chunk.location.char_end,
            page=chunk.location.page,
        )


async def enforce_query_limits(
    query: str,
    top_k: int | None,
    embedder: Embedder,
    settings: Settings,
) -> None:
    """질의 하나가 처리 가능한지 판정한다. 통과하지 못하면 도메인 예외를 던진다.

    두 엔드포인트가 같은 함수를 부르므로 코드도 형식도 갈릴 자리가 없다."""
    _reject_invalid(query, top_k, embedder, settings)
    await _reject_beyond_the_input_window(query, embedder, settings)


def _reject_invalid(
    query: str, top_k: int | None, embedder: Embedder, settings: Settings
) -> None:
    """토크나이저에 닿기 전에 끝나는 판정들 — 내용 없음, 문자 수, K 상한.

    공짜로 판정되는 것을 스레드 하나 빌린 뒤에 판정할 이유가 없어 앞에 둔다."""
    if not query.strip():
        raise EmptyQuery("질의가 비어 있습니다")

    # 막는 것은 절단이 아니라 임의 길이 입력이 토크나이저에 들어가는 것이다.
    # 절단은 아래 토큰 가드가 막는다.
    if len(query) > settings.retrieval_max_query_chars:
        raise _too_long(
            f"질의가 문자 수 상한({settings.retrieval_max_query_chars}자)을 넘었습니다",
            embedder,
            settings,
        )

    # 상한만 여기서 본다. 하한(`>= 1`)은 요청 모델이 이미 `validation_error` 로 끝냈다.
    if top_k is not None and top_k > settings.retrieval_max_top_k:
        raise InvalidTopK(
            f"top_k 는 {settings.retrieval_max_top_k} 이하여야 합니다",
            max_top_k=settings.retrieval_max_top_k,
        )


async def _reject_beyond_the_input_window(
    query: str, embedder: Embedder, settings: Settings
) -> None:
    """조용한 절단을 막는 가드 — 실제로 인코딩되는 문자열 기준으로 센다.

    자르지 않는 이유는 조용히 자르면 사용자가 전부 반영됐다고 믿기 때문이다."""
    tokens = await asyncio.to_thread(embedder.count_query_tokens, query)
    if tokens > embedder.max_input_tokens:
        raise _too_long(
            f"질의가 임베딩 입력 창({embedder.max_input_tokens} 토큰)을 넘었습니다",
            embedder,
            settings,
        )


def _too_long(reason: str, embedder: Embedder, settings: Settings) -> QueryTooLong:
    """어느 쪽에 걸렸든 두 상한을 함께 싣는다.

    걸린 쪽만 실으면 코드는 하나인데 항목이 둘인 응답이 되어 소비자가 분기를 든다."""
    return QueryTooLong(
        reason,
        max_query_chars=settings.retrieval_max_query_chars,
        max_query_tokens=embedder.max_input_tokens,
    )
