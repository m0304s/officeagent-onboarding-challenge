"""재시도 정책 — 시도 횟수, 백오프, 재시도 대상과 비대상.

**정책이 서비스에 있는지를 재는 파일이다.** 한 시도의 상한과 자원 회수는 어댑터가
(`test_llm_pool.py`), 시도 횟수·백오프·가부는 여기가 덮는다. 어댑터가 실패를 예외 셋으로
정규화하므로 생성 표면을 바꿔도 이 파일은 그대로다 — 페이크가 던지는 것도 그 셋이다.

**실제로 자지 않는다.** 백오프 단언은 대기 시간을 관측하는 것이지 겪는 것이 아니라,
하네스가 `asyncio.sleep` 을 기록으로 바꾼다. 겪게 두면 이 파일 하나가 스위트 전체보다
오래 걸린다.
"""

from app.core.answers import FinishReason
from app.core.exceptions import (
    ErrorCode,
    LlmGenerationFailed,
    LlmTimeout,
    LlmUnauthenticated,
)
from tests.qa_harness import (
    VERDICT_ANSWERABLE,
    answers_of,
    done_of,
    error_of,
    make_qa_harness,
    names,
)
from tests.retrieval_harness import POLICY
from tests.stubs import GenerationTurn

ANSWER = GenerationTurn(chunks=(VERDICT_ANSWERABLE, "교육비는 연 200만원까지 지원됩니다 [1]."))


async def test_a_timed_out_attempt_is_retried(monkeypatch):
    harness = make_qa_harness(
        GenerationTurn(raises=LlmTimeout("주입된 시간 초과")), ANSWER, monkeypatch=monkeypatch
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert harness.generator.calls == 2
    assert done_of(events).answer.finish_reason is FinishReason.STOP
    assert harness.generator.open_turns == 0, "실패한 시도가 만든 자원이 남아 있다"


async def test_exhausted_attempts_end_the_stream_with_error(monkeypatch):
    harness = make_qa_harness(
        GenerationTurn(raises=LlmTimeout("주입된 시간 초과")),
        max_attempts=3,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    error = error_of(events)
    assert harness.generator.calls == 3
    assert names(events) == ["sources", "error"]
    assert error.attempts == 3
    assert error.code is ErrorCode.LLM_UNAVAILABLE
    assert error.reason.value == "timeout"


async def test_the_last_failure_reason_tells_timeout_from_other_failures(monkeypatch):
    """뭉치면 "LLM 이 불안정하다"는 보고만 남아 상한을 올릴 일인지 알 수 없다."""
    harness = make_qa_harness(
        GenerationTurn(raises=LlmGenerationFailed("주입된 생성 실패")),
        max_attempts=2,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert error_of(events).reason.value == "generation_failed"
    assert error_of(events).code is ErrorCode.LLM_UNAVAILABLE


async def test_the_backoff_grows_between_attempts(monkeypatch):
    harness = make_qa_harness(
        GenerationTurn(raises=LlmTimeout("주입된 시간 초과")),
        max_attempts=3,
        retry_backoff_seconds=1.0,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    await harness.ask()

    assert len(harness.sleeps) == 2, "시도 3회 사이의 대기는 2회다"
    assert harness.sleeps[1] > harness.sleeps[0]
    assert harness.sleeps == [1.0, 2.0]


async def test_no_backoff_is_paid_after_the_last_attempt(monkeypatch):
    """마지막 시도 뒤의 대기는 아무도 기다리지 않는 지연이다."""
    harness = make_qa_harness(
        GenerationTurn(raises=LlmTimeout("주입된 시간 초과")),
        max_attempts=1,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    await harness.ask()

    assert harness.sleeps == []
    assert harness.generator.calls == 1


async def test_a_failure_after_the_first_chunk_is_not_retried(monkeypatch):
    """재시도는 이어 쓰는 것이 아니라 처음부터 다시 쓰는 것이다.

    이어 붙이면 사용자는 앞부분이 두 번 적힌 답변을 본다. 스트리밍이 재시도 정책에 거는
    제약이며, 스트리밍이 없었다면 존재하지 않았을 규칙이다.
    """
    harness = make_qa_harness(
        GenerationTurn(
            chunks=(VERDICT_ANSWERABLE, "앞부분입니다."),
            raises=LlmGenerationFailed("조각을 낸 뒤의 실패"),
        ),
        ANSWER,
        max_attempts=3,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert harness.generator.calls == 1, "조각이 나간 뒤에 다시 시도했다"
    assert answers_of(events) == ["앞부분입니다."]
    assert names(events) == ["sources", "answer", "error"]
    assert error_of(events).attempts == 1
    assert harness.sleeps == []


async def test_a_failure_before_any_chunk_is_retried(monkeypatch):
    harness = make_qa_harness(
        GenerationTurn(raises=LlmGenerationFailed("조각이 나가기 전의 실패")),
        ANSWER,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert harness.generator.calls == 2
    assert names(events)[-1] == "done"


async def test_missing_credentials_fail_immediately_with_their_own_code(monkeypatch):
    """백오프를 몇 번 돌아도 자격증명은 생기지 않는다 — 실패가 느려지기만 한다."""
    harness = make_qa_harness(
        GenerationTurn(raises=LlmUnauthenticated("주입된 인증 부재")),
        max_attempts=3,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    error = error_of(events)
    assert harness.generator.calls == 1, "인증 부재를 재시도했다"
    assert harness.sleeps == []
    assert error.code is ErrorCode.LLM_UNAUTHENTICATED
    assert error.code is not ErrorCode.LLM_UNAVAILABLE, "시도 소진과 같은 코드로 뭉갰다"
    assert error.attempts == 1
    assert error.reason.value == "unauthenticated"


async def test_the_error_message_carries_no_adapter_internals(monkeypatch):
    harness = make_qa_harness(
        GenerationTurn(raises=LlmGenerationFailed("세션이 stdout EOF 로 죽었습니다")),
        max_attempts=1,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert "stdout" not in error_of(events).message


async def test_each_attempt_receives_the_configured_timeout(monkeypatch):
    """상한은 정책이 아니라 예산이라 그대로 어댑터로 내려간다."""
    harness = make_qa_harness(
        GenerationTurn(raises=LlmTimeout("주입된 시간 초과")),
        max_attempts=2,
        timeout_seconds=12.5,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    await harness.ask()

    assert harness.generator.timeouts == [12.5, 12.5]


async def test_every_attempt_gets_the_same_prompt(monkeypatch):
    """재시도가 프롬프트를 바꾸면 두 시도의 실패가 같은 사건인지 알 수 없다."""
    harness = make_qa_harness(
        GenerationTurn(raises=LlmTimeout("주입된 시간 초과")), ANSWER, monkeypatch=monkeypatch
    )
    await harness.ingest("policy.txt", POLICY)

    await harness.ask()

    assert len(harness.generator.prompts) == 2
    assert harness.generator.prompts[0] == harness.generator.prompts[1]
