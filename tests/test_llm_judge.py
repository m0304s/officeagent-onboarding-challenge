"""판정 층의 순수 부분 — 프롬프트 조립, 판정 파싱, 결정적 문자열 검사, 골든셋 로더.

실물 CLI 를 부르는 층(`test_golden_judge`)은 구독이 있어야 도는데, 판정을 오답으로
잘못 세는 사고는 여기서 걸린다.
"""

from tests.golden_harness import load_items, load_unanswerable, spans_hold
from tests.llm_judge import (
    NO_EVIDENCE_TEXT,
    GenerationScores,
    Graded,
    Judgement,
    JudgeVerdict,
    build_judge_prompt,
    parse_judgement,
)

QUESTION = "혈중알코올농도 0.05%로 운전하다 사람을 다치게 하면 면허는 어떻게 되나요?"
REFERENCE = "면허가 취소되고 2년간 면허 취득이 불가합니다."


def test_the_prompt_carries_the_three_things_the_judge_compares():
    """판정자가 보는 것은 질문과 두 답변뿐이다."""
    prompt = build_judge_prompt(QUESTION, REFERENCE, "면허가 취소됩니다.[1]")

    assert QUESTION in prompt
    assert REFERENCE in prompt
    assert "면허가 취소됩니다.[1]" in prompt


def test_a_well_formed_verdict_is_read_with_its_reason():
    parsed = parse_judgement("JUDGE: MATCH\n두 답변이 같은 처분을 말한다.\n남은 줄")

    assert parsed.verdict is JudgeVerdict.MATCH
    assert parsed.reason == "두 답변이 같은 처분을 말한다."


def test_a_mismatch_is_read_as_a_mismatch():
    parsed = parse_judgement("JUDGE: MISMATCH\n기간이 다르다.")

    assert parsed.verdict is JudgeVerdict.MISMATCH


def test_a_broken_format_is_unjudged_rather_than_wrong():
    """형식 위반을 오답으로 세면 판정자가 흔들린 회차가 품질 하락으로 읽힌다."""
    parsed = parse_judgement("판정: 일치합니다")

    assert parsed.verdict is JudgeVerdict.UNJUDGED
    assert "판정: 일치합니다" in parsed.reason


def test_spans_ignore_whitespace_on_both_sides():
    """추출 텍스트에는 어절 공백이 없고 생성된 답변에는 있다."""
    item = load_items()[0]
    spaced = " ".join(item.expected_spans)

    assert spans_hold(item, spaced) or item.must_not_contain


def test_a_forbidden_span_fails_even_when_the_expected_ones_are_present():
    item = next(item for item in load_items() if item.must_not_contain)
    answer = "".join(item.expected_spans) + item.must_not_contain[0]

    assert not spans_hold(item, answer)


def test_every_reference_answer_passes_its_own_item():
    """골든셋 README 가 선언한 자체 검증 — 기준 답변이 자기 문항을 통과한다."""
    failed = [
        item.id
        for item in load_items() + load_unanswerable()
        if not spans_hold(item, item.reference_answer)
    ]

    assert not failed, f"기준 답변이 자기 문항에서 떨어졌다: {failed}"


def test_the_refusal_sentence_trips_no_forbidden_span():
    """근거 0건 종료를 옮긴 문장이 금지 조각을 건드리면, 옳게 거절한 답이 실패로 잡힌다."""
    tripped = [item.id for item in load_unanswerable() if not spans_hold(item, NO_EVIDENCE_TEXT)]

    assert not tripped, f"거절 문장이 금지 조각에 걸렸다: {tripped}"


def test_the_loader_splits_the_two_gates():
    """근거가 있는 문항만 검색 게이트를 거치고, 나머지는 생성만 채점된다."""
    with_evidence = load_items()
    without = load_unanswerable()

    assert len(with_evidence) == 44
    assert len(without) == 6
    assert all(item.quote for item in with_evidence)
    assert all(item.quote is None for item in without)


def test_the_tally_keeps_the_layers_apart():
    """층을 합산하지 않는다 — 문자열 통과 수와 판정 통과 수가 따로 센다."""

    def row(id: str, *, spans_ok=None, verdict=None, retrieved=True, reason="stop") -> Graded:
        return Graded(
            id=id,
            probe="grounding.x",
            retrieved=retrieved,
            finish_reason=reason,
            spans_ok=spans_ok,
            judgement=None if verdict is None else Judgement(verdict, ""),
        )

    scores = GenerationScores(
        (
            row("A", spans_ok=True, verdict=JudgeVerdict.MATCH),
            row("B", spans_ok=False, verdict=JudgeVerdict.MATCH),
            row("C", verdict=JudgeVerdict.UNJUDGED),
            row("D", retrieved=False, reason="no_evidence"),
        )
    )

    assert scores.spans_passed == 1
    assert scores.counting(JudgeVerdict.MATCH) == 2
    assert scores.counting(JudgeVerdict.UNJUDGED) == 1
    assert scores.gated_out == 1
    assert scores.finishing("stop") == 3
    assert scores.finishing("no_evidence") == 1
    assert [row.id for row in scores.disagreements] == ["B"]
