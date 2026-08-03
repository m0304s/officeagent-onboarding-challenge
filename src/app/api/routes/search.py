"""검색 엔드포인트 — 질의 하나에 근거 청크 상위 K개.

답변을 만들지 않아 `/qa` 가 생겨도 남는다 — 원인이 검색인지 생성인지 가르는 지점이다.
`POST` 인 이유는 부수 효과가 아니라 요청 모양이다 (`ARCHITECTURE.md` 검색 파이프라인).
"""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.queries import (
    EmbedderDep,
    RetrievalServiceDep,
    SearchResultView,
    SettingsDep,
    enforce_query_limits,
)
from app.services.retrieval import RetrievalResult

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    """질의 한 건.

    하한만 여기 있다 — 배포와 무관한 사실이라 스키마에 속하고 OpenAPI 에 드러난다."""

    query: str = Field(description="질문 한 문장")
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="돌려받을 근거 청크 수. 주지 않으면 서버 기본값을 쓴다",
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
    await enforce_query_limits(body.query, body.top_k, embedder, settings)

    result = await service.search(body.query, top_k=body.top_k)

    _log_search(request, result)
    return SearchResponse(
        query=result.query,
        top_k=result.top_k,
        results=[SearchResultView.of(chunk) for chunk in result.chunks],
        count=result.count,
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
