"""QA 서비스 — 무엇이 몇 번 어떤 순서로 나가는가.

`VerdictSplitter` 의 경계는 `test_prompting.py` 가 문자열로 덮었고, 여기서는 그 상태
기계를 구동한 결과가 이벤트 수와 내용으로 드러나는지를 본다.
"""

import asyncio
from contextlib import aclosing

from app.core.answers import FinishReason
from app.core.exceptions import ErrorCode, LlmGenerationFailed
from tests.qa_harness import (
    QUESTION,
    VERDICT_ANSWERABLE,
    VERDICT_INSUFFICIENT,
    answers_of,
    done_of,
    error_of,
    make_qa_harness,
    names,
    sources_of,
)
from tests.retrieval_harness import GUIDE, POLICY
from tests.stubs import GenerationTurn

# ── 이벤트 시퀀스 (4.11) ─────────────────────────────────────────────────


async def test_the_stream_starts_with_sources_and_ends_with_done():
    harness = make_qa_harness(
        GenerationTurn(chunks=(VERDICT_ANSWERABLE, "교육비는 연 200만원까지 지원됩니다 [1]."))
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert names(events)[0] == "sources"
    assert names(events)[-1] == "done"
    assert names(events).count("answer") >= 1
    assert "error" not in names(events)


async def test_exactly_one_terminal_event_and_it_is_last():
    """종료가 하나여야 클라이언트 상태 기계가 "끝났다/끊겼다"를 추정하지 않는다."""
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE, "답변입니다.")))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    terminals = [name for name in names(events) if name in {"done", "error"}]
    assert terminals == ["done"]
    assert names(events)[-1] in {"done", "error"}


async def test_joined_answer_chunks_equal_the_final_answer():
    harness = make_qa_harness(
        GenerationTurn(
            chunks=(VERDICT_ANSWERABLE, "교육비는 ", "연 200만원까지 ", "지원됩니다 [1].")
        )
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert "".join(answers_of(events)) == done_of(events).answer.text


async def test_a_single_chunk_answer_is_not_split_further():
    """조각을 서버가 쪼개면 "스트리밍하고 있다"는 겉모습만 얻고 도착 시각은 그대로다."""
    whole = "교육비는 연 200만원까지 지원됩니다 [1]. 신청은 인사팀에 합니다."
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE + whole,)))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert answers_of(events) == [whole]
    assert done_of(events).answer.text == whole


async def test_the_server_neither_merges_nor_splits_chunks_after_the_verdict():
    pieces = ("첫 조각. ", "둘째 조각. ", "셋째 조각.")
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE, *pieces)))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert answers_of(events) == list(pieces)


async def test_sources_carries_the_search_result_shape():
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE, "답변입니다.")))
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)

    events = await harness.ask()

    sources = sources_of(events)
    # 같은 질문을 검색 경로에 직접 보낸 결과와 대조한다 — 두 경로가 같은 사실을 다르게
    # 보여 주면 소비자가 뷰를 둘 들어야 한다.
    expected = await harness.retrieval.retrieval.search(QUESTION)
    assert sources.count == len(expected.chunks)
    assert sources.top_k == expected.top_k
    assert sources.target_documents == expected.target_documents
    assert [chunk.document_id for chunk in sources.results] == [
        chunk.document_id for chunk in expected.chunks
    ]


async def test_top_k_narrows_the_sources_event():
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE, "답변입니다.")))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask(top_k=2)

    sources = sources_of(events)
    assert sources.top_k == 2
    assert sources.count <= 2


# ── 판정 줄 버퍼링 (4.12) ────────────────────────────────────────────────


async def test_the_verdict_line_never_reaches_the_client():
    harness = make_qa_harness(
        GenerationTurn(chunks=(VERDICT_ANSWERABLE + "교육비는 연 200만원입니다 [1].",))
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert all("VERDICT" not in text for text in answers_of(events))
    assert "VERDICT" not in done_of(events).answer.text
    assert done_of(events).answer.text.startswith("교육비는")


async def test_a_verdict_split_across_chunks_holds_events_until_it_settles():
    """실측 모양 그대로 — 판정 줄 하나가 여러 델타에 걸쳐 온다.

    이벤트 수는 같을 수 있어, 몇 번째 조각에서 나왔는지를 함께 본다."""
    harness = make_qa_harness(
        GenerationTurn(chunks=("VERDICT", ": ANSWERABLE\n앞부분입니다.", " 뒷부분입니다."))
    )
    await harness.ingest("policy.txt", POLICY)

    observed = await harness.ask_watching_chunks()

    answers = [(event.text, chunks) for event, chunks in observed if event.name.value == "answer"]
    assert [text for text, _ in answers] == ["앞부분입니다.", " 뒷부분입니다."]
    # 첫 `answer` 는 두 번째 조각에서야 나온다 — 첫 조각 동안에는 하나도 안 나갔다.
    assert [chunks for _, chunks in answers] == [2, 3]


async def test_chunks_after_the_verdict_pass_through_one_for_one():
    pieces = ("하나. ", "둘. ", "셋.")
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE, *pieces)))
    await harness.ingest("policy.txt", POLICY)

    observed = await harness.ask_watching_chunks()

    answers = [(event.text, chunks) for event, chunks in observed if event.name.value == "answer"]
    assert [text for text, _ in answers] == list(pieces)
    # 조각 하나에 이벤트 하나 — 서버가 더 모으지도 쪼개지도 않았다.
    assert [chunks for _, chunks in answers] == [2, 3, 4]


async def test_an_output_without_a_verdict_line_keeps_its_first_line():
    """형식 위반이 곧 환각은 아니다 — 본문을 버리지 않고 첫 줄도 자르지 않는다."""
    raw = "교육비는 연 200만원까지 지원됩니다.\n신청은 인사팀에 합니다."
    harness = make_qa_harness(GenerationTurn(chunks=(raw,)))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert done_of(events).answer.text == raw
    assert "".join(answers_of(events)) == raw
    assert done_of(events).answer.finish_reason is FinishReason.STOP


# ── 거절 두 갈래 (4.13) ──────────────────────────────────────────────────


async def test_no_evidence_never_calls_the_generator():
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE, "답변입니다.")))

    events = await harness.ask()

    assert harness.generator.calls == 0, "근거가 없는데 생성기를 불렀다"
    assert names(events) == ["sources", "done"]
    assert sources_of(events).count == 0
    done = done_of(events)
    assert done.answer.finish_reason is FinishReason.NO_EVIDENCE
    assert done.answer.text == "", "서비스가 답변 문자열을 만들었다"
    assert done.answer.citations == ()


async def test_a_score_floor_that_empties_the_result_is_still_no_evidence():
    """근거가 0건인 경로가 둘이지만(문서 없음·하한) 관측 결과는 같아야 한다."""
    harness = make_qa_harness(
        GenerationTurn(chunks=(VERDICT_ANSWERABLE, "답변입니다.")), min_score=1.0
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert sources_of(events).count == 0
    assert sources_of(events).target_documents == 1, "문서는 있었는데 대상이 0으로 잡혔다"
    assert harness.generator.calls == 0
    assert done_of(events).answer.finish_reason is FinishReason.NO_EVIDENCE
    assert "error" not in names(events)


async def test_insufficient_evidence_keeps_the_sources_and_drops_the_verdict_line():
    reason = "제공된 근거는 교육비 한도를 말하지 않습니다."
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_INSUFFICIENT + reason,)))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    done = done_of(events)
    assert done.answer.finish_reason is FinishReason.INSUFFICIENT_EVIDENCE
    assert done.answer.text == reason
    assert done.answer.citations == ()
    assert sources_of(events).count >= 1, "무엇을 보고 판단했는지가 사라졌다"
    assert names(events)[-1] == "done"


async def test_the_two_refusals_are_told_apart_by_finish_reason():
    """사용자가 할 일이 다르다 — 문서를 올릴 것인가, 질문을 바꿀 것인가."""
    empty = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE, "답변입니다.")))
    refusing = make_qa_harness(GenerationTurn(chunks=(VERDICT_INSUFFICIENT + "부족합니다.",)))
    await refusing.ingest("policy.txt", POLICY)

    without_evidence = await empty.ask()
    with_evidence = await refusing.ask()

    assert done_of(without_evidence).answer.finish_reason is FinishReason.NO_EVIDENCE
    assert done_of(with_evidence).answer.finish_reason is FinishReason.INSUFFICIENT_EVIDENCE
    assert empty.generator.calls == 0
    assert refusing.generator.calls == 1


# ── 빈 `answer` 의 유일성 (4.14) ─────────────────────────────────────────


async def test_a_stop_stream_never_ends_with_an_empty_answer():
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE, "답변입니다.")))
    await harness.ingest("policy.txt", POLICY)

    done = done_of(await harness.ask())

    assert done.answer.finish_reason is FinishReason.STOP
    assert done.answer.text != ""


async def test_an_insufficient_stream_never_ends_with_an_empty_answer():
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_INSUFFICIENT + "부족합니다.",)))
    await harness.ingest("policy.txt", POLICY)

    done = done_of(await harness.ask())

    assert done.answer.finish_reason is FinishReason.INSUFFICIENT_EVIDENCE
    assert done.answer.text != ""


async def test_a_verdict_only_output_is_retried_not_accepted(monkeypatch):
    """본문 없는 출력을 성공으로 받으면 그 장애가 빈 화면으로만 드러난다."""
    harness = make_qa_harness(
        GenerationTurn(chunks=(VERDICT_ANSWERABLE,)),
        GenerationTurn(chunks=(VERDICT_ANSWERABLE, "이번엔 본문이 있습니다.")),
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert harness.generator.calls == 2
    assert done_of(events).answer.finish_reason is FinishReason.STOP
    assert done_of(events).answer.text == "이번엔 본문이 있습니다."


async def test_a_verdict_only_output_in_every_attempt_ends_with_error(monkeypatch):
    harness = make_qa_harness(
        GenerationTurn(chunks=(VERDICT_ANSWERABLE,)), max_attempts=3, monkeypatch=monkeypatch
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert harness.generator.calls == 3
    assert "done" not in names(events)
    assert names(events)[-1] == "error"
    assert error_of(events).attempts == 3
    assert error_of(events).code is ErrorCode.LLM_UNAVAILABLE


async def test_whitespace_only_bodies_count_as_no_body(monkeypatch):
    """공백뿐인 본문도 본문이 아니다 — 소비자 화면에서는 빈 답변과 같다."""
    harness = make_qa_harness(
        GenerationTurn(chunks=(VERDICT_ANSWERABLE + "   \n  ",)),
        GenerationTurn(chunks=(VERDICT_ANSWERABLE, "본문입니다.")),
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert harness.generator.calls == 2
    assert done_of(events).answer.text == "본문입니다."


# ── 동시 생성 상한과 관측 (4.8 · 4.9) ───────────────────────────────────


async def test_the_concurrency_ceiling_makes_requests_wait_not_fail():
    """상한에 걸린 요청은 실패하지 않고 대기한다 — 수집과 같은 규율이다.

    둘 다 성공해 성패로는 안 드러나므로 동시에 열려 있던 시도의 최대치로 잰다."""
    harness = make_qa_harness(
        GenerationTurn(chunks=(VERDICT_ANSWERABLE, "답변입니다."), delay=0.01),
        concurrency=1,
    )
    await harness.ingest("policy.txt", POLICY)

    both = await asyncio.gather(harness.ask(), harness.ask())

    assert [names(events)[-1] for events in both] == ["done", "done"]
    assert harness.generator.calls == 2, "대기해야 할 요청이 실패로 끝났다"
    assert harness.generator.peak_open_turns == 1, "상한을 넘겨 동시에 생성했다"


async def test_abandoning_the_stream_stops_the_generation():
    """순회를 멈추면 진행 중이던 시도가 정리된다.

    `aclosing` 이 없으면 정리가 가비지 컬렉션에 달려, 취소된 요청이 프로세스를 남긴다."""
    harness = make_qa_harness(
        GenerationTurn(chunks=(VERDICT_ANSWERABLE, "앞부분.", "뒷부분."), delay=0.01)
    )
    await harness.ingest("policy.txt", POLICY)

    context = await harness.service.prepare(QUESTION)
    stream = harness.service.stream(context)
    async with aclosing(stream):
        await anext(stream)  # sources
        await anext(stream)  # 첫 answer — 여기서 순회를 버린다

    assert harness.generator.open_turns == 0, "버려진 시도가 정리되지 않았다"


async def test_the_log_line_carries_counts_but_not_content(caplog):
    """질문·근거 본문·답변 본문은 남기지 않는다 — 검색이 지키는 규율 그대로다."""
    body = "교육비는 연 200만원까지 지원됩니다 [1]."
    harness = make_qa_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE + body,)))
    await harness.ingest("policy.txt", POLICY)

    with caplog.at_level("INFO", logger="app.services.qa"):
        events = await harness.ask()

    record = next(r for r in caplog.records if r.name == "app.services.qa")
    assert record.finish_reason == "stop"
    assert record.citation_count == len(done_of(events).answer.citations)
    assert record.dropped_markers == 0
    assert record.attempts == 1
    assert record.source_count == sources_of(events).count
    assert record.request_id == "req-test"
    assert isinstance(record.elapsed_ms, int)

    payload = " ".join(str(value) for value in vars(record).values())
    assert QUESTION not in payload, "질문 문자열이 로그에 남았다"
    assert body not in payload, "답변 본문이 로그에 남았다"
    assert sources_of(events).results[0].text not in payload, "근거 본문이 로그에 남았다"


async def test_a_failed_stream_carries_no_done_event(monkeypatch):
    harness = make_qa_harness(
        GenerationTurn(raises=LlmGenerationFailed("주입된 생성 실패")),
        max_attempts=2,
        monkeypatch=monkeypatch,
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert names(events) == ["sources", "error"], "근거는 이미 나갔어야 하고 done 은 없어야 한다"
    assert sources_of(events).count >= 1
