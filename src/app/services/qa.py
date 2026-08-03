"""답변 생성 오케스트레이션 — `sources` → `answer`* → (`done` | `error`).

`prepare` 와 `stream` 이 갈린 것이 첫 계약이다 — 상태 코드가 첫 바이트와 함께 확정되므로
그 경계가 코드 구조여야 한다. 여기 있는 것은 정책뿐이다 (`ARCHITECTURE.md`).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

import anyio

from app.adapters.protocols import AnswerGenerator
from app.core.answers import Answer, FinishReason, build_citations
from app.core.exceptions import (
    ErrorCode,
    LlmGenerationFailed,
    LlmTimeout,
    LlmUnauthenticated,
)
from app.core.prompting import ParsedAnswer, Verdict, VerdictSplitter, build_prompt, parse_answer
from app.core.retrieval import ScoredChunk
from app.services.retrieval import RetrievalResult, RetrievalService

logger = logging.getLogger(__name__)


class QaEventName(StrEnum):
    """스트림이 내보내는 이벤트 이름 넷. 이것이 어휘의 전부다.

    데이터 안의 `type` 이 아니라 이름인 것은 `EventSource` 가 바로 분기하기 때문이다."""

    SOURCES = "sources"
    ANSWER = "answer"
    DONE = "done"
    ERROR = "error"


class FailureReason(StrEnum):
    """`error` 이벤트가 말하는 마지막 실패의 원인.

    코드는 종료 사건을, 사유는 그 원인을 가리킨다 — 갈려야 운영자가 처분을 정한다."""

    TIMEOUT = "timeout"
    GENERATION_FAILED = "generation_failed"
    UNAUTHENTICATED = "unauthenticated"


@dataclass(frozen=True)
class SourcesEvent:
    """무엇을 근거로 답하려는가 — 항상 첫 이벤트.

    검색이 생성보다 빨라 먼저 보낸다. `/search` 와 같은 모양이라 뷰가 하나면 된다."""

    name: ClassVar[QaEventName] = QaEventName.SOURCES

    results: tuple[ScoredChunk, ...]
    top_k: int
    target_documents: int

    @property
    def count(self) -> int:
        return len(self.results)

    @classmethod
    def of(cls, result: RetrievalResult) -> "SourcesEvent":
        return cls(
            results=result.chunks,
            top_k=result.top_k,
            target_documents=result.target_documents,
        )


@dataclass(frozen=True)
class AnswerEvent:
    """생성기가 내보낸 조각 하나 — 판정 줄을 걷어낸 뒤의 본문.

    서버가 조각을 다시 만지지 않는다 — 쪼개면 연출이고 합치면 도착을 되돌린다."""

    name: ClassVar[QaEventName] = QaEventName.ANSWER

    text: str


@dataclass(frozen=True)
class DoneEvent:
    """정상 종료 — 답변 전문·종료 사유·검증된 인용·소요 시간.

    전문을 다시 싣는 것은 의도한 중복이다 — 조립 책임을 클라이언트에 지우지 않는다."""

    name: ClassVar[QaEventName] = QaEventName.DONE

    answer: Answer
    elapsed_ms: int


@dataclass(frozen=True)
class ErrorEvent:
    """실패 종료 — 코드·메시지·소진된 시도 수·마지막 실패 사유.

    한 겹 더 싸지 않는다 — 이벤트 이름이 이미 오류를 말한다. 어댑터 문자열도 안 옮긴다."""

    name: ClassVar[QaEventName] = QaEventName.ERROR

    code: ErrorCode
    message: str
    attempts: int
    reason: FailureReason


#: 스트림이 내보내는 값의 전부. 소비자(`api/sse.py`)가 `isinstance` 로 갈라 직렬화한다.
QaEvent = SourcesEvent | AnswerEvent | DoneEvent | ErrorEvent


@dataclass(frozen=True)
class QaContext:
    """스트림이 열리기 전에 이미 확정된 것들.

    `stream` 이 질문을 다시 받지 않아 두 단계 사이에 값이 갈릴 자리가 없다."""

    question: str
    result: RetrievalResult
    request_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)

    @property
    def sources(self) -> tuple[ScoredChunk, ...]:
        return self.result.chunks


class QaService:
    """질문 하나를 이벤트 시퀀스로 바꾼다 — 검색은 스트림 밖에서, 생성은 안에서."""

    def __init__(
        self,
        retrieval: RetrievalService,
        generator: AnswerGenerator,
        *,
        timeout_seconds: float,
        max_attempts: int,
        retry_backoff_seconds: float,
        concurrency: int,
    ) -> None:
        self._retrieval = retrieval
        self._generator = generator
        # 정책이 아니라 예산이라 그대로 어댑터에 넘긴다 — 중단 대상과 회수 방법을
        # 아는 곳이 거기뿐이다. 시도 횟수와 백오프는 정책이라 여기 남는다.
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds

        # 생성 하나가 프로세스 하나라 상한이 없으면 메모리가 동시 요청 수에 비례한다.
        # 걸린 요청은 대기한다 — `sources` 는 이미 나가 느린 생성과 구분되지 않는다.
        self._limiter = anyio.CapacityLimiter(concurrency)

    # ── 1단계: 스트림 밖 ────────────────────────────────────────────────

    async def prepare(
        self,
        question: str,
        *,
        top_k: int | None = None,
        request_id: str | None = None,
    ) -> QaContext:
        """검색까지. 예외를 잡지 않는다.

        잡아서 `error` 로 바꾸면 같은 장애가 `/search` 와 `/qa` 에서 다르게 보인다."""
        started_at = time.monotonic()
        result = await self._retrieval.search(question, top_k=top_k)
        return QaContext(
            question=question,
            result=result,
            request_id=request_id,
            started_at=started_at,
        )

    # ── 2단계: 스트림 안 ────────────────────────────────────────────────

    async def stream(self, context: QaContext) -> AsyncIterator[QaEvent]:
        """`sources` → `answer`* → (`done` | `error`). 종료 이벤트는 정확히 하나다.

        종료 없는 닫힘을 허용하면 클라이언트가 매번 타임아웃으로 추정해야 한다."""
        yield SourcesEvent.of(context.result)

        if not context.sources:
            yield self._no_evidence(context)
            return

        # `async for` 는 순회를 멈출 때 대상 생성기를 닫아 주지 않는다 — 여기서 닫지
        # 않으면 안쪽의 `aclosing` 이 영영 돌지 않는다.
        async with aclosing(self._generate(context)) as events:
            async for event in events:
                yield event

    def _no_evidence(self, context: QaContext) -> DoneEvent:
        """근거 0건 — 생성기를 부르지 않고, 문구도 만들지 않고 `done` 으로 끝낸다.

        빈 문맥에서 모델이 쓸 재료는 학습된 지식뿐이라 애초에 묻지 않는다."""
        answer = Answer.no_evidence()
        self._log_done(context, answer=answer, attempts=0)
        return DoneEvent(answer=answer, elapsed_ms=self._elapsed_ms(context))

    async def _generate(self, context: QaContext) -> AsyncIterator[QaEvent]:
        """시도를 소진하거나 성공할 때까지 — 조각은 도착하는 즉시 나간다."""
        sources = context.sources
        prompt = build_prompt(context.question, sources)

        # 재시도는 이어 쓰기가 아니라 처음부터 다시 쓰기라, 조각이 나간 뒤에 재시도하면
        # 앞부분이 두 번 적힌 답변이 보인다.
        emitted = False

        for attempt in range(1, self._max_attempts + 1):
            splitter = VerdictSplitter()
            raw: list[str] = []
            try:
                async with self._limiter:
                    generation = self._generator.generate(
                        prompt, timeout_seconds=self._timeout_seconds
                    )
                    # `aclosing` 이 취소 경로의 전부다 — 없으면 정리 시점이 가비지
                    # 컬렉션에 달려 취소된 요청 하나가 프로세스 하나씩을 남긴다.
                    async with aclosing(generation) as pieces:
                        async for piece in pieces:
                            raw.append(piece)
                            for body in splitter.feed(piece):
                                emitted = True
                                yield AnswerEvent(text=body)

                # 개행 없이 끝나는 출력이 버퍼에 갇히지 않게 한다.
                for body in splitter.finish():
                    emitted = True
                    yield AnswerEvent(text=body)

                parsed = parse_answer("".join(raw), len(sources))
                if not parsed.has_body:
                    # 성공으로 받으면 형식만 지킨 회차가 `stop` 으로 집계되고 사용자에게는
                    # 근거 없음과 같은 빈 화면이 된다.
                    raise LlmGenerationFailed("생성 출력에 본문이 없습니다")

            except LlmUnauthenticated:
                # 재시도하지 않는다 — 백오프를 돌아도 자격증명이 생기지 않는다. 코드를
                # 나누는 것은 소비자가 할 일이 다르기 때문이다.
                yield self._failed(
                    context,
                    code=ErrorCode.LLM_UNAUTHENTICATED,
                    message="답변 생성기가 인증되지 않았습니다",
                    attempts=attempt,
                    reason=FailureReason.UNAUTHENTICATED,
                )
                return

            except (LlmTimeout, LlmGenerationFailed) as exc:
                reason = (
                    FailureReason.TIMEOUT
                    if isinstance(exc, LlmTimeout)
                    else FailureReason.GENERATION_FAILED
                )
                if emitted or attempt == self._max_attempts:
                    yield self._failed(
                        context,
                        code=ErrorCode.LLM_UNAVAILABLE,
                        message="답변 생성에 실패했습니다",
                        attempts=attempt,
                        reason=reason,
                    )
                    return
                # 지터를 넣지 않는다 — 몰릴 규모가 없고, 넣으면 대기 순서 단언이
                # 확률적이 된다.
                await asyncio.sleep(self._retry_backoff_seconds * 2 ** (attempt - 1))
                continue

            answer = _assemble(parsed, sources)
            self._log_done(context, answer=answer, attempts=attempt)
            if not parsed.verdict_line_present:
                # 형식 위반은 응답이 아니라 로그로 잡는다 — 이 경고가 늘어나는 것이
                # 프롬프트 회귀의 신호다.
                logger.warning(
                    "생성 출력에 판정 줄이 없어 답변으로 간주했습니다",
                    extra={"request_id": context.request_id},
                )
            yield DoneEvent(answer=answer, elapsed_ms=self._elapsed_ms(context))
            return

    # ── 조립과 관측 ─────────────────────────────────────────────────────

    def _failed(
        self,
        context: QaContext,
        *,
        code: ErrorCode,
        message: str,
        attempts: int,
        reason: FailureReason,
    ) -> ErrorEvent:
        logger.warning(
            "답변 생성이 실패로 끝났습니다",
            extra={
                "request_id": context.request_id,
                "source_count": context.result.count,
                "target_documents": context.result.target_documents,
                "error_code": code.value,
                "failure_reason": reason.value,
                "attempts": attempts,
                "elapsed_ms": self._elapsed_ms(context),
            },
        )
        return ErrorEvent(code=code, message=message, attempts=attempts, reason=reason)

    def _log_done(self, context: QaContext, *, answer: Answer, attempts: int) -> None:
        """요청 하나가 무엇을 했는지 한 줄로. 질문·근거·답변 본문은 싣지 않는다.

        `dropped_markers` 는 프롬프트가 나빠졌다는 가장 이른 신호다."""
        logger.info(
            "답변 생성 요청을 처리했습니다",
            extra={
                "request_id": context.request_id,
                "source_count": context.result.count,
                "target_documents": context.result.target_documents,
                "finish_reason": answer.finish_reason.value,
                "citation_count": len(answer.citations),
                "dropped_markers": answer.dropped_markers,
                "attempts": attempts,
                "elapsed_ms": self._elapsed_ms(context),
            },
        )

    @staticmethod
    def _elapsed_ms(context: QaContext) -> int:
        return int((time.monotonic() - context.started_at) * 1000)


def _assemble(parsed: ParsedAnswer, sources: Sequence[ScoredChunk]) -> Answer:
    """파싱 결과와 근거로 답변 한 건을 조립한다.

    판정이 마커를 이기고, 본문의 마커는 지우지 않는다 — 지우면 흘러간 문장과 갈린다."""
    if parsed.verdict is Verdict.INSUFFICIENT:
        return Answer(text=parsed.body, finish_reason=FinishReason.INSUFFICIENT_EVIDENCE)
    return Answer(
        text=parsed.body,
        finish_reason=FinishReason.STOP,
        citations=build_citations(parsed.markers, sources),
        dropped_markers=parsed.dropped_markers,
    )
