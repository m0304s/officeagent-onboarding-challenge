"""SSE 프레이밍 — `QaEvent` 를 전송 형식으로 바꾸고 조용한 구간에 주석을 끼운다.

형식이 `api/` 에 있는 이유는 전송의 관심사이기 때문이다. 서비스가 프레임 문자열을 만들면
서비스 테스트가 SSE 문법을 단언하게 되어 전송을 바꿀 때 함께 깨진다.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from pydantic import BaseModel, Field

from app.api.queries import SearchResultView
from app.core.answers import Citation, FinishReason
from app.core.cache import CacheLayer
from app.core.documents import DocumentFormat
from app.core.reranking import ORDERED_BY_FUSION, ORDERED_BY_RERANK
from app.services.qa import (
    AnswerEvent,
    DoneEvent,
    ErrorEvent,
    FailureReason,
    QaEvent,
    SourcesEvent,
)

logger = logging.getLogger(__name__)

#: 이벤트가 아니라 주석이라 클라이언트 파서에 나타나지 않는다. 계약을 건드리지 않은 채
#: 프록시의 유휴 연결 종료만 막는다.
KEEP_ALIVE = ": keep-alive\n\n"


class CitationView(BaseModel):
    """답변이 실제로 인용한 근거 하나.

    같은 스트림의 `sources` 항목과 같은 값이다 — 인용은 새 사실을 만들지 않는다."""

    marker: int
    document_id: str
    filename: str
    format: DocumentFormat
    revision: str
    chunk_index: int
    score: float
    char_start: int
    char_end: int
    page: int | None = None

    @classmethod
    def of(cls, citation: Citation) -> "CitationView":
        return cls(
            marker=citation.marker,
            document_id=citation.document_id,
            filename=citation.filename,
            format=citation.format,
            revision=citation.revision,
            chunk_index=citation.chunk_index,
            score=citation.score,
            char_start=citation.location.char_start,
            char_end=citation.location.char_end,
            page=citation.location.page,
        )


class SourcesPayload(BaseModel):
    """`sources` 이벤트 — 무엇을 근거로 답하려는가.

    `results` 뒤의 두 필드까지 `/search` 응답과 같다 — 뷰가 하나면 되는 이유다."""

    results: list[SearchResultView]
    count: int
    top_k: int
    target_documents: int
    ordered_by: str = Field(
        default=ORDERED_BY_FUSION,
        description=(
            f"`results` 의 순서를 정한 신호 — 리랭킹 점수면 `{ORDERED_BY_RERANK}`,"
            f" 융합 점수면 `{ORDERED_BY_FUSION}`"
        ),
    )
    reranker: str | None = Field(
        default=None,
        description="이번 검색에서 실제로 순서를 정한 리랭커 이름. 꺼졌거나 축소됐으면 null",
    )


class AnswerPayload(BaseModel):
    """`answer` 이벤트 — 생성기가 내보낸 조각 하나."""

    text: str


class DonePayload(BaseModel):
    """`done` 이벤트 — 종료 사유·답변 전문·인용·버려진 마커 수·소요 시간·캐시 판정.

    구조를 모델이 아니라 서버가 보증한다 — 모델이 형식을 어겨도 이 모양은 흔들리지 않는다."""

    finish_reason: FinishReason
    answer: str
    citations: list[CitationView]
    dropped_markers: int
    #: 이번 요청의 소요 시간이지 캐시된 복사가 아니다.
    elapsed_ms: int
    cache_hit: bool = Field(description="이 답변이 캐시에서 왔는가")
    cache_layer: CacheLayer | None = Field(
        default=None, description="히트한 층 — 정확 매치 / 유사 매치. 미스면 null"
    )
    cache_similarity: float | None = Field(
        default=None,
        description="유사 매치 판정에 쓰인 코사인 유사도. 정확 매치와 미스에서는 null",
    )


class ErrorPayload(BaseModel):
    """`error` 이벤트 — 오류 봉투와 같은 어휘에 시도 수와 마지막 사유를 붙인다.

    한 겹 더 싸지 않는다 — 이벤트 이름이 이미 "이건 오류다"를 말한다."""

    code: str
    message: str
    attempts: int
    reason: FailureReason


def frame(event: QaEvent) -> str:
    """이벤트 하나를 `event:`/`data:`/빈 줄 프레임으로.

    데이터는 항상 JSON 객체 하나이고, 본문의 개행은 JSON 이스케이프가 삼킨다."""
    return f"event: {event.name.value}\ndata: {_payload(event).model_dump_json()}\n\n"


#: 스트림이 끝났다는 큐 신호. `None` 을 쓰지 않는 이유는 이벤트와 구분이 이름으로
#: 드러나야 하기 때문이다.
_END = object()


async def frames(
    events: AsyncIterator[QaEvent], *, heartbeat_seconds: float
) -> AsyncIterator[str]:
    """이벤트 스트림을 프레임 스트림으로. 조용한 구간에는 주석을 끼운다.

    다음 이벤트를 기다리는 동안만 주석을 내므로 이벤트 사이 간격이 계약을 바꾸지 않는다."""
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    pump = asyncio.create_task(_pump(events, queue))
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), heartbeat_seconds)
            except TimeoutError:
                yield KEEP_ALIVE
                continue
            if event is _END:
                break
            yield frame(event)
        # 펌프가 예외로 끝났으면 여기서 다시 난다 — 삼키면 스트림이 종료 이벤트 없이 닫힌다.
        await pump
    finally:
        # 클라이언트가 끊으면 여기로 온다. 펌프를 끊는 것이 곧 생성 중단이라, 이 정리를
        # 빠뜨리면 취소된 요청 하나가 프로세스 하나를 남긴다.
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump


async def _pump(events: AsyncIterator[QaEvent], queue: "asyncio.Queue[Any]") -> None:
    """서비스 스트림을 통째로 한 태스크에서 돌려 큐로 옮긴다.

    조각마다 태스크를 만들면 서비스가 든 동시 상한이 반납 시점에 터진다."""
    try:
        async with aclosing(events) as stream:
            async for event in stream:
                await queue.put(event)
    finally:
        await queue.put(_END)


def _payload(event: QaEvent) -> BaseModel:
    if isinstance(event, SourcesEvent):
        return SourcesPayload(
            results=[SearchResultView.of(chunk) for chunk in event.results],
            count=event.count,
            top_k=event.top_k,
            target_documents=event.target_documents,
            ordered_by=event.ordered_by,
            reranker=event.reranker,
        )
    if isinstance(event, AnswerEvent):
        return AnswerPayload(text=event.text)
    if isinstance(event, DoneEvent):
        return DonePayload(
            finish_reason=event.answer.finish_reason,
            answer=event.answer.text,
            citations=[CitationView.of(citation) for citation in event.answer.citations],
            dropped_markers=event.answer.dropped_markers,
            elapsed_ms=event.elapsed_ms,
            cache_hit=event.cache.hit,
            cache_layer=event.cache.layer,
            cache_similarity=event.cache.similarity,
        )
    if isinstance(event, ErrorEvent):
        return ErrorPayload(
            code=event.code.value,
            message=event.message,
            attempts=event.attempts,
            reason=event.reason,
        )
    raise TypeError(f"직렬화할 수 없는 이벤트: {type(event).__name__}")
