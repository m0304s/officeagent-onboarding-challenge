"""답변 엔드포인트 — 질문 하나에 서버 전송 이벤트 스트림.

두 단계로 갈린 것이 이 라우터의 전부다. 검증과 검색은 스트림 밖이라 상태 코드로 끝나고,
그 뒤의 실패는 스트림 안의 `error` 로만 나간다 (`ARCHITECTURE.md`).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.queries import EmbedderDep, QaServiceDep, SettingsDep, enforce_query_limits
from app.api.sse import frames

logger = logging.getLogger(__name__)

router = APIRouter(tags=["qa"])

#: 스트림이 중간 계층에 갇히지 않게 하는 헤더들. 버퍼링이 켜져 있으면 조각이 도착하는
#: 시각이 앞당겨지지 않아 스트리밍의 이득이 통째로 사라진다.
_STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class QaRequest(BaseModel):
    """질문 한 건. `/search` 와 같은 상한을 받는다.

    하한만 여기 있다 — 배포와 무관한 사실이라 스키마에 속하고 OpenAPI 에 드러난다."""

    question: str = Field(description="질문 한 문장")
    top_k: int | None = Field(
        default=None,
        ge=1,
        description="근거로 삼을 청크 수. 주지 않으면 서버 기본값을 쓴다",
    )


@router.post("/qa", summary="문서 근거 기반 답변 (SSE)")
async def qa(
    request: Request,
    body: QaRequest,
    service: QaServiceDep,
    embedder: EmbedderDep,
    settings: SettingsDep,
) -> StreamingResponse:
    """`sources` → `answer`* → (`done` | `error`) 를 흘려보낸다.

    `prepare` 를 await 한 뒤에 응답을 만든다 — 안이면 검색 실패가 본문 없는 `200` 이 된다."""
    await enforce_query_limits(body.question, body.top_k, embedder, settings)

    context = await service.prepare(
        body.question,
        top_k=body.top_k,
        request_id=getattr(request.state, "request_id", None),
    )
    return StreamingResponse(
        frames(
            service.stream(context),
            heartbeat_seconds=settings.qa_sse_heartbeat_seconds,
        ),
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )
