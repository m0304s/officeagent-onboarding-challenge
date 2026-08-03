"""검색 엔드포인트 — 질의 하나에 근거 청크 상위 K개.

답변을 만들지 않아 `/qa` 가 생겨도 남는다 — 원인이 검색인지 생성인지 가르는 지점이다.
`POST` 인 이유는 부수 효과가 아니라 요청 모양이다 (`ARCHITECTURE.md` 검색 파이프라인).
"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.adapters.protocols import Embedder
from app.config import Settings
from app.core.documents import DocumentFormat
from app.core.exceptions import EmptyQuery, InvalidTopK, QueryTooLong
from app.core.retrieval import ScoredChunk
from app.services.retrieval import RetrievalResult, RetrievalService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


def get_retrieval_service(request: Request) -> RetrievalService:
    """앱 팩토리가 배선한 서비스를 꺼낸다. 모듈 전역 싱글턴을 두지 않는다."""
    return request.app.state.retrieval_service


def get_embedder(request: Request) -> Embedder:
    """토큰 상한 판정에 임베더가 필요하다.

    상한이 모델 선언값이라 설정에 없고, 판정이 임베딩보다 앞서야 한다."""
    return request.app.state.embedder


def get_settings_of(request: Request) -> Settings:
    return request.app.state.settings


RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
SettingsDep = Annotated[Settings, Depends(get_settings_of)]


class SearchRequest(BaseModel):
    """질의 한 건.

    하한만 여기 있다 — 배포와 무관한 사실이라 스키마에 속하고 OpenAPI 에 드러난다."""

    query: str = Field(description="질문 한 문장")
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="돌려받을 근거 청크 수. 주지 않으면 서버 기본값을 쓴다",
    )


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


class SearchResponse(BaseModel):
    """검색 응답.

    `top_k` 를 되싣는 이유는 요청이 생략했을 때 적용된 값이 응답에만 드러나서다."""

    query: str
    top_k: int
    results: list[SearchResultView]
    count: int


@router.post("/search", response_model=SearchResponse, summary="근거 청크 검색")
async def search(
    request: Request,
    body: SearchRequest,
    service: RetrievalServiceDep,
    embedder: EmbedderDep,
    settings: SettingsDep,
) -> SearchResponse:
    """질의와 가까운 청크를 유사도 내림차순으로 최대 K개.

    대상은 지금 유효한 청크뿐이고, 하한에 걸린 빈 결과는 오류가 아니라 `200` 이다."""
    _reject_invalid(body, embedder, settings)
    await _reject_beyond_the_input_window(body.query, embedder, settings)

    result = await service.search(body.query, top_k=body.top_k)

    _log_search(request, result)
    return SearchResponse(
        query=result.query,
        top_k=result.top_k,
        results=[SearchResultView.of(chunk) for chunk in result.chunks],
        count=result.count,
    )


def _reject_invalid(body: SearchRequest, embedder: Embedder, settings: Settings) -> None:
    """토크나이저에 닿기 전에 끝나는 판정들 — 내용 없음, 문자 수, K 상한.

    공짜로 판정되는 것을 스레드 하나 빌린 뒤에 판정할 이유가 없어 앞에 둔다."""
    if not body.query.strip():
        raise EmptyQuery("질의가 비어 있습니다")

    # 막는 것은 절단이 아니라 임의 길이 입력이 토크나이저에 들어가는 것이다.
    # 절단은 아래 토큰 가드가 막는다.
    if len(body.query) > settings.retrieval_max_query_chars:
        raise _too_long(
            f"질의가 문자 수 상한({settings.retrieval_max_query_chars}자)을 넘었습니다",
            embedder,
            settings,
        )

    # 상한만 여기서 본다. 하한(`>= 1`)은 요청 모델이 이미 `validation_error` 로 끝냈다.
    if body.top_k is not None and body.top_k > settings.retrieval_max_top_k:
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


def _log_search(request: Request, result: RetrievalResult) -> None:
    """질의 한 건을 요청과 묶어 한 줄로. 질의 문자열과 청크 본문은 싣지 않는다.

    `target_documents` 가 있어야 빈 결과의 이유가 갈린다."""
    logger.info(
        "검색 요청을 처리했습니다",
        extra={
            "request_id": getattr(request.state, "request_id", None),
            "top_k": result.top_k,
            "result_count": result.count,
            "top_score": result.top_score,
            "target_documents": result.target_documents,
        },
    )
