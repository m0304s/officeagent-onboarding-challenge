"""답변 생성 테스트 하네스.

검색 하네스(`retrieval_harness.py`) 위에 **페이크 생성기 하나만** 얹는다. 근거는 실제
수집·검색 경로로 만든다 — 근거 목록을 손으로 지어내면 `sources` 이벤트가 `/search` 와 같은
모양인지, 인용 값이 그 근거와 일치하는지가 테스트의 조작으로 항상 참이 된다.

**시간을 재지 않는다.** 재시도 백오프도 조각 도착 순서도 시각이 아니라 순서로 단언한다 —
백오프는 `sleeps` 로 관측하고(실제로 자지 않는다), 조각과 이벤트의 관계는
`ScriptedGenerator.emitted_chunks` 로 본다. 벽시계에 기대는 단언은 느린 CI 에서 흔들리고,
흔들리는 테스트는 결국 꺼진다.
"""

from dataclasses import dataclass, field

import pytest

from app.core.answers import Citation
from app.services.qa import (
    AnswerEvent,
    DoneEvent,
    ErrorEvent,
    QaEvent,
    QaService,
    SourcesEvent,
)
from tests.retrieval_harness import Harness, make_harness
from tests.stubs import GenerationTurn, ScriptedGenerator

#: 근거가 붙는 평범한 질문. 페이크 임베더의 벡터에는 의미가 없으므로 문장 자체는 검색
#: 결과를 바꾸지 않는다 — 하한을 0으로 두어 "문서가 있으면 근거가 있다"만 성립시킨다.
QUESTION = "교육비는 얼마까지 지원되나요?"

VERDICT_ANSWERABLE = "VERDICT: ANSWERABLE\n"
VERDICT_INSUFFICIENT = "VERDICT: INSUFFICIENT\n"


@dataclass
class QaHarness:
    """같은 대역 위에 선 수집·검색·답변 생성."""

    retrieval: Harness
    generator: ScriptedGenerator
    service: QaService
    #: 재시도 대기 목록. 실제로 자지 않고 여기에 초 단위 값만 쌓인다.
    sleeps: list[float] = field(default_factory=list)

    async def ingest(self, filename: str, text: str) -> None:
        await self.retrieval.ingest(filename, text)

    async def ingest_bytes(self, filename: str, data: bytes) -> None:
        """PDF 처럼 텍스트가 아닌 문서. 인용의 원문 위치가 포맷에 따라 갈리는지 볼 때 쓴다."""
        await self.retrieval.ingestion.ingest(filename, data)

    async def ask(self, question: str = QUESTION, *, top_k: int | None = None) -> list[QaEvent]:
        """질문 하나를 끝까지 읽어 이벤트 목록으로."""
        context = await self.service.prepare(question, top_k=top_k, request_id="req-test")
        return [event async for event in self.service.stream(context)]

    async def ask_watching_chunks(self, question: str = QUESTION) -> list[tuple[QaEvent, int]]:
        """이벤트마다 **그 시점까지 생성기가 내보낸 조각 수**를 함께 기록한다.

        "판정이 확정되기 전에는 `answer` 이벤트가 나가지 않는다"는 개수만으로는 확인되지
        않는다 — 이벤트 수가 맞아도 첫 조각에서 이미 하나 나갔을 수 있다. 이벤트가 몇 번째
        조각에서 나왔는지를 보면 그 성질이 순서로 고정된다.
        """
        context = await self.service.prepare(question, request_id="req-test")
        return [
            (event, self.generator.emitted_chunks) async for event in self.service.stream(context)
        ]


def make_qa_harness(
    *turns: GenerationTurn,
    monkeypatch: pytest.MonkeyPatch | None = None,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 1.0,
    timeout_seconds: float = 30.0,
    concurrency: int = 2,
    top_k: int = 5,
    min_score: float = 0.0,
) -> QaHarness:
    """대본을 받아 하네스 하나를 만든다.

    `monkeypatch` 를 주면 재시도 대기를 **기록만 하고 실제로 자지 않는다.** 백오프가 늘어난다는
    단언은 값 자체를 보면 되는데, 그걸 확인하려고 테스트가 실제로 3초를 자야 할 이유가 없다.
    """
    retrieval = make_harness(top_k=top_k, min_score=min_score)
    generator = ScriptedGenerator(turns=turns or (GenerationTurn(),))
    harness = QaHarness(
        retrieval=retrieval,
        generator=generator,
        service=QaService(
            retrieval.retrieval,
            generator,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            concurrency=concurrency,
        ),
    )
    if monkeypatch is not None:

        async def record(seconds: float) -> None:
            harness.sleeps.append(seconds)

        monkeypatch.setattr("app.services.qa.asyncio.sleep", record)
    return harness


# ── 이벤트 목록을 읽는 창 ────────────────────────────────────────────────


def names(events: list[QaEvent]) -> list[str]:
    return [event.name.value for event in events]


def answers_of(events: list[QaEvent]) -> list[str]:
    return [event.text for event in events if isinstance(event, AnswerEvent)]


def sources_of(events: list[QaEvent]) -> SourcesEvent:
    return _only(events, SourcesEvent)


def done_of(events: list[QaEvent]) -> DoneEvent:
    return _only(events, DoneEvent)


def error_of(events: list[QaEvent]) -> ErrorEvent:
    return _only(events, ErrorEvent)


def markers_of(citations: tuple[Citation, ...]) -> list[int]:
    return [citation.marker for citation in citations]


def _only(events: list[QaEvent], kind: type) -> QaEvent:
    matched = [event for event in events if isinstance(event, kind)]
    assert len(matched) == 1, f"{kind.__name__} 이 {len(matched)}개다: {names(events)}"
    return matched[0]
