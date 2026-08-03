"""답변 생성 오케스트레이션 — 이벤트 시퀀스와 정책.

```
prepare:  질의 검증(API) → 검색                     ← 예외가 그대로 올라간다 (상태 코드)
stream:   sources → [판정 줄 분리 → answer*] → done | error
```

**두 단계로 갈라져 있는 것이 이 파일의 첫 계약이다.** 상태 코드는 첫 바이트와 함께
확정되므로 스트림을 연 뒤에 발견한 실패는 어떤 방법으로도 상태 코드로 알릴 수 없다.
검색까지를 `prepare` 로 빼면 그 경계가 코드 구조가 된다 — 검증·저장소 실패는 스트림
**밖에서** 기존 오류 핸들러가 `/search` 와 똑같은 봉투로 끝내고, `stream` 부터의 실패만
`error` 이벤트가 된다. 둘을 한 async generator 에 넣으면 첫 `yield` 전의 예외가 이미
`200` 과 헤더가 나간 뒤에 터져 **본문 없는 200** 이 되는데, 그 형태는 스펙의 어떤
시나리오로도 잡히지 않는다.

**답변 문자열을 만드는 곳은 생성기뿐이며 예외가 없다.** 근거가 0건이면 생성기를 부르지
않고 끝내되 서비스가 거절 문구를 쓰지도 않는다 — `finish_reason` 이 사실을 나르고 표현은
소비자가 정한다. 이 규칙에 예외를 두지 않는 것이 "하드코딩된 응답" 경계를 해석 문제가
아니라 구조 문제로 만든다.

**정책만 여기 있다.** 판정 줄을 어떻게 알아보는지(`core/prompting.py`), 답변이 스스로
지키는 불변식(`core/answers.py`), 한 시도를 언제 끊는지(어댑터)는 전부 밖에 있다. 여기
있는 것은 시도 횟수·백오프·재시도 가부와 이벤트를 언제 내보내는가뿐이다.

계층 규칙: 이 모듈은 어댑터의 **프로토콜**만 알고 구현체를 모른다. SSE 프레이밍도 모른다 —
`QaEvent` 값 객체를 yield 하고 `event:`/`data:` 로 바꾸는 일은 `api/` 가 한다.
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

    **이름 있는 SSE 이벤트를 쓰는 이유**는 이름이 프로토콜 수준의 개념이고 브라우저
    `EventSource` 가 `addEventListener("sources", ...)` 로 바로 분기하기 때문이다. 데이터
    안의 `type` 필드로 구분하면 모든 소비자가 파싱 후 분기를 다시 만들어야 한다.
    """

    SOURCES = "sources"
    ANSWER = "answer"
    DONE = "done"
    ERROR = "error"


class FailureReason(StrEnum):
    """`error` 이벤트가 말하는 **마지막 실패의 원인**.

    오류 코드와 역할이 다르다 — 코드는 종료 사건("시도가 다 떨어졌다")을 가리키고 이쪽은
    그 사건의 원인을 가리킨다. 최소한 시간 초과와 그 밖의 생성 실패가 갈려야 운영자가
    상한을 올릴 일인지 다른 것을 볼 일인지 판단할 수 있다.
    """

    TIMEOUT = "timeout"
    GENERATION_FAILED = "generation_failed"
    UNAUTHENTICATED = "unauthenticated"


@dataclass(frozen=True)
class SourcesEvent:
    """무엇을 근거로 답하려는가 — **항상 첫 이벤트**.

    생성이 초 단위인 데 비해 검색은 그보다 훨씬 빠르다. 근거를 먼저 보내면 클라이언트는
    답변을 기다리는 동안 근거를 보여줄 수 있고, 답이 틀렸을 때 사용자가 검증할 재료도 그
    시점에 이미 손에 있다.

    `results` 는 `/search` 응답과 **같은 모양**이다 — 같은 `ScoredChunk` 를 그대로 싣는다.
    두 엔드포인트가 같은 사실을 다르게 보여 주면 소비자가 뷰를 둘 들어야 한다.
    """

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

    **서버가 조각을 다시 만지지 않는다.** 쪼개면 없는 진행을 만들어 내는 연출이고(첫 글자가
    도착하는 시각은 1밀리초도 앞당겨지지 않는다), 합치면 실제로 앞당겨진 도착을 되돌리는
    일이다. 유일한 예외가 판정 줄 제거이고, 그건 재조립이 아니라 확정 전까지 보류였다가
    본문만 흘려보내는 것이다.
    """

    name: ClassVar[QaEventName] = QaEventName.ANSWER

    text: str


@dataclass(frozen=True)
class DoneEvent:
    """정상 종료 — 답변 전문·종료 사유·검증된 인용·소요 시간.

    답변 전문을 **다시** 싣는 것은 중복이지만 의도한 중복이다. 조각을 이어 붙이는 책임을
    모든 클라이언트에 지우지 않으면서, "이어 붙인 결과 = 서버의 최종본"이라는 불변식을
    스펙이 검증 가능하게 만든다.

    `elapsed_ms` 가 `Answer` 안이 아니라 여기 있는 이유는 소요 시간이 답변의 성질이 아니라
    그 답변을 만든 **요청의 측정값**이기 때문이다.
    """

    name: ClassVar[QaEventName] = QaEventName.DONE

    answer: Answer
    elapsed_ms: int


@dataclass(frozen=True)
class ErrorEvent:
    """실패 종료 — 코드·메시지·소진된 시도 수·마지막 실패 사유.

    오류 봉투와 **같은 어휘**를 쓰되 봉투 구조까지 같게 하지는 않는다(`{"error": {...}}` 로
    한 겹 더 싸지 않는다). SSE 는 이벤트 이름이 이미 "이건 오류다"를 말하므로 중첩이
    정보를 더하지 않는다.

    메시지에 어댑터가 준 문자열을 옮기지 않는다 — 내부 사정이 응답으로 새는 자리다.
    """

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

    이 값이 손에 있다는 사실이 곧 "요청이 유효하고 근거 수집이 끝났다"는 뜻이다. 그래서
    `stream` 은 인자로 질문도 `top_k` 도 다시 받지 않는다 — 두 단계 사이에 값이 갈릴
    자리를 만들지 않는다.

    `started_at` 이 여기 있는 이유는 소요 시간이 **검색을 포함한 요청 전체**의 측정값이기
    때문이다. `stream` 에서 재기 시작하면 검색이 느린 배포에서 그 사실이 어디에도 남지 않는다.
    """

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
        # 한 시도의 상한. **정책이 아니라 예산이라 그대로 어댑터에 넘긴다** — 중단할
        # 대상(턴)과 회수 방법을 아는 곳이 거기뿐이다. 시도 횟수와 백오프는 반대로
        # 정책이라 여기 남는다. 어댑터에 두면 생성기를 바꿀 때 정책이 따라 이사한다.
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds

        # 동시 생성 총량. 생성 하나가 세션 하나이고 세션 하나가 프로세스 하나라, 상한이
        # 없으면 컨테이너 메모리가 동시 요청 수에 비례한다. **상한에 걸린 요청은
        # 실패하지 않고 대기한다** — 수집과 같은 규율이다. 대기 중에도 `sources` 는 이미
        # 나갔고 하트비트가 연결을 유지하므로 사용자에게는 느린 생성과 구분되지 않는다.
        self._limiter = anyio.CapacityLimiter(concurrency)

    # ── 1단계: 스트림 밖 ────────────────────────────────────────────────

    async def prepare(
        self,
        question: str,
        *,
        top_k: int | None = None,
        request_id: str | None = None,
    ) -> QaContext:
        """검색까지. **예외를 잡지 않는다.**

        여기서 나는 `StorageUnavailable` 은 기존 오류 핸들러가 `/search` 와 완전히 같은
        코드와 상태로 끝낸다. 잡아서 `error` 이벤트로 바꾸면 저장소가 죽은 상태의 관측이
        `/search`(503)와 `/qa`(200 + error)에서 갈리고, 같은 장애가 엔드포인트마다 다른
        형태로 보이면 모니터링이 두 규칙을 들어야 한다.

        질의 문자열의 유효성(빈 질의·길이 상한·K 경계)은 API 계층이 이미 판정했다 —
        거부된 요청이 임베딩을 유발하지 않으려면 그 판정이 임베딩 이전에 끝나야 한다.
        """
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

        종료 이벤트를 배타적으로 하나 두는 이유는 클라이언트의 상태 기계가 단순해지기
        때문이다. 종료 없이 연결이 닫히는 경우를 계약이 허용하면 클라이언트는 모든
        스트림에 대해 "정상 종료인가 끊긴 것인가"를 타임아웃으로 추정해야 한다.
        """
        yield SourcesEvent.of(context.result)

        if not context.sources:
            yield self._no_evidence(context)
            return

        # 위임에도 `aclosing` 이 필요하다 — `async for` 는 순회를 멈출 때 대상 생성기를
        # 닫아 주지 않는다. 여기서 닫지 않으면 연결이 끊겼을 때 안쪽의 `aclosing` 이
        # 영영 돌지 않아 취소 경로가 통째로 사라진다.
        async with aclosing(self._generate(context)) as events:
            async for event in events:
                yield event

    def _no_evidence(self, context: QaContext) -> DoneEvent:
        """근거 0건 — 생성기를 부르지 않고, 문구도 만들지 않고 끝낸다.

        문맥이 빈 프롬프트에서 모델이 쓸 수 있는 재료는 학습된 지식뿐이다. "문서에 없으면
        답하지 마라"는 지시로 그것을 막는 것보다 애초에 묻지 않는 편이 확실하고 빠르고 싸다.
        **오류가 아니다** — `error` 가 아니라 `done` 으로 끝난다.
        """
        answer = Answer.no_evidence()
        self._log_done(context, answer=answer, attempts=0)
        return DoneEvent(answer=answer, elapsed_ms=self._elapsed_ms(context))

    async def _generate(self, context: QaContext) -> AsyncIterator[QaEvent]:
        """시도를 소진하거나 성공할 때까지 — 조각은 도착하는 즉시 나간다."""
        sources = context.sources
        prompt = build_prompt(context.question, sources)

        # **조각이 하나라도 나갔는가.** 재시도 가부의 절반이 이 한 줄에 걸린다. 재시도는
        # 답변을 이어 쓰는 것이 아니라 처음부터 다시 쓰는 것이라, 이미 절반이 도착한
        # 클라이언트에 두 번째 시도를 이어 붙이면 앞부분이 두 번 적힌 답변이 보인다.
        # 스트리밍이 재시도 정책에 거는 제약이며, 스트리밍이 없었다면 없었을 규칙이다.
        emitted = False

        for attempt in range(1, self._max_attempts + 1):
            splitter = VerdictSplitter()
            raw: list[str] = []
            try:
                async with self._limiter:
                    generation = self._generator.generate(
                        prompt, timeout_seconds=self._timeout_seconds
                    )
                    # **`aclosing` 이 취소 경로의 전부다.** 소비자가 순회를 멈추면(연결
                    # 종료) 여기서 `aclose` 가 돌고 어댑터의 `finally` 가 그 시도가 만든
                    # 자원을 회수한다. 이것이 없으면 정리 시점이 가비지 컬렉션에 달리고,
                    # 그때는 취소된 요청 하나가 프로세스 하나씩을 남긴다.
                    async with aclosing(generation) as pieces:
                        async for piece in pieces:
                            raw.append(piece)
                            for body in splitter.feed(piece):
                                emitted = True
                                yield AnswerEvent(text=body)

                # 개행 없이 끝나는 출력(짧은 한 줄 답변, 판정 줄만 낸 회차)이 버퍼에
                # 갇히지 않게 한다. 생성이 끝났음을 아는 곳이 여기뿐이다.
                for body in splitter.finish():
                    emitted = True
                    yield AnswerEvent(text=body)

                parsed = parse_answer("".join(raw), len(sources))
                if not parsed.has_body:
                    # **본문 없는 출력은 성공이 아니다.** 성공으로 받으면 그 장애가
                    # 조용해진다 — 모델이 형식만 지키고 내용을 못 낸 회차가 `stop` 으로
                    # 집계되고, 사용자에게는 근거 없음과 똑같은 빈 화면으로 보인다.
                    raise LlmGenerationFailed("생성 출력에 본문이 없습니다")

            except LlmUnauthenticated:
                # **재시도하지 않는다.** 백오프를 몇 번 돌아도 자격증명이 생기지 않는다.
                # 코드를 생성 실패 일반과 나누는 이유는 소비자가 할 일이 다르기 때문이다 —
                # 이쪽은 자격증명 주입 경로를 확인하는 일이다.
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
                # `base * 2^(n-1)`. 지터를 넣지 않는다 — 동시 생성 상한이 한 자릿수라
                # 동기화된 재시도가 몰릴 규모가 없고, 넣으면 "두 번째 대기가 첫 번째보다
                # 길다"는 스펙 단언이 확률적이 된다.
                await asyncio.sleep(self._retry_backoff_seconds * 2 ** (attempt - 1))
                continue

            answer = _assemble(parsed, sources)
            self._log_done(context, answer=answer, attempts=attempt)
            if not parsed.verdict_line_present:
                # 형식 위반은 응답이 아니라 로그로 잡는다 — 그 본문은 근거만 주어진
                # 프롬프트에서 나온 것이라 형식 위반이 곧 환각은 아니고, 마커 검증은
                # 그대로 돌아 출처 보증도 그대로다. 이 경고가 늘어나는 것이
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
        """요청 하나가 무엇을 했는지 한 줄로.

        **질문 문자열·근거 본문·답변 본문은 싣지 않는다.** 검색이 이미 같은 규율을 지키고
        있고, 답변은 질문과 근거를 합친 것이라 셋 중 가장 민감하다. 대신 세는 값을 남긴다 —
        특히 `dropped_markers` 는 프롬프트가 나빠졌다는 가장 이른 신호다.
        """
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

    **판정이 마커를 이긴다.** `INSUFFICIENT` 면 본문에 마커가 남아 있어도 인용을 만들지
    않는다 — "답할 수 없다"면서 근거를 인용하는 출력은 모순이고, 인용을 남기면 거절문에
    출처가 붙어 답변처럼 보인다. 그때 `dropped_markers` 도 세지 않는다: 그 수가 재는 것은
    "없는 근거를 가리킨 마커"이지 "정책이 무시한 마커"가 아니라, 섞으면 프롬프트 열화를
    재는 신호로 쓸 수 없다.

    **본문의 마커는 지우지 않는다.** 지우면 스트림으로 흘러간 문장과 `done.answer` 가
    달라져, 둘 다 받은 클라이언트가 무엇을 표시할지 알 수 없다.
    """
    if parsed.verdict is Verdict.INSUFFICIENT:
        return Answer(text=parsed.body, finish_reason=FinishReason.INSUFFICIENT_EVIDENCE)
    return Answer(
        text=parsed.body,
        finish_reason=FinishReason.STOP,
        citations=build_citations(parsed.markers, sources),
        dropped_markers=parsed.dropped_markers,
    )
