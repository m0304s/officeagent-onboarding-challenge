"""인용 검증 — 조립된 `citations` 가 근거와 어긋나지 않는가.

마커는 1부터 세는 라벨이고 근거 목록은 0부터 세는 배열이라, 그 변환이 한 칸 어긋나도
형식은 멀쩡한 채 인용이 옆 청크를 가리킨다. 규칙 자체는 `test_prompting.py` 가 덮는다.
"""

from app.core.answers import FinishReason
from tests.pdf_fixtures import make_pdf
from tests.qa_harness import (
    VERDICT_ANSWERABLE,
    VERDICT_INSUFFICIENT,
    done_of,
    make_qa_harness,
    markers_of,
    sources_of,
)
from tests.retrieval_harness import POLICY
from tests.stubs import GenerationTurn


def answering(body: str) -> GenerationTurn:
    return GenerationTurn(chunks=(VERDICT_ANSWERABLE + body,))


async def test_a_citation_points_at_the_source_it_names():
    harness = make_qa_harness(answering("교육비는 연 200만원까지 지원됩니다 [1]."))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    citations = done_of(events).answer.citations
    assert markers_of(citations) == [1]
    first = sources_of(events).results[0]
    citation = citations[0]
    assert citation.document_id == first.document_id
    assert citation.filename == first.filename
    assert citation.format == first.format
    assert citation.revision == first.revision
    assert citation.chunk_index == first.chunk_index
    assert citation.location == first.location
    assert citation.score == first.score


async def test_citations_follow_the_order_they_appear_in_the_body():
    harness = make_qa_harness(answering("재택근무는 주 2회입니다 [2]. 교육비는 별도입니다 [1]."))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert markers_of(done_of(events).answer.citations) == [2, 1]


async def test_the_same_source_cited_twice_appears_once():
    harness = make_qa_harness(answering("교육비는 연 200만원 [1] 이고 신청은 인사팀 [1] 입니다."))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert markers_of(done_of(events).answer.citations) == [1]


async def test_a_marker_outside_the_source_range_is_dropped_and_counted():
    """없는 근거를 가리키는 인용은 환각 중 가장 그럴듯한 형태다 — 번호가 붙어 검증된 듯 보인다."""
    harness = make_qa_harness(answering("교육비 [1] 와 알 수 없는 근거 [7] 입니다."), top_k=2)
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    done = done_of(events)
    assert sources_of(events).count == 2
    assert markers_of(done.answer.citations) == [1]
    assert done.answer.dropped_markers == 1


async def test_a_repeated_out_of_range_marker_counts_once():
    """세는 대상이 "잘못 가리킨 근거"이지 "잘못 적은 글자"가 아니다.

    뒤집으면 그 수가 답변 길이에 비례해 흔들려 프롬프트 열화를 재는 신호로 쓸 수 없다."""
    harness = make_qa_harness(
        answering("[9] 그리고 [9] 그리고 또 [9]. 근거 [1] 도 있습니다."), top_k=2
    )
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert done_of(events).answer.dropped_markers == 1


async def test_markers_stay_in_the_answer_body():
    """지우면 스트림으로 흘러간 문장과 `done.answer` 가 달라진다."""
    body = "교육비는 연 200만원까지 지원됩니다 [1]."
    harness = make_qa_harness(answering(body))
    await harness.ingest("policy.txt", POLICY)

    events = await harness.ask()

    assert "[1]" in done_of(events).answer.text
    assert done_of(events).answer.text == body


async def test_an_answer_without_markers_is_still_a_valid_answer():
    """`stop` + 빈 `citations` 가 "답은 했는데 근거를 특정하지 못했다"를 유일하게 식별한다."""
    harness = make_qa_harness(answering("교육비는 연 200만원까지 지원됩니다."))
    await harness.ingest("policy.txt", POLICY)

    done = done_of(await harness.ask())

    assert done.answer.finish_reason is FinishReason.STOP
    assert done.answer.text != ""
    assert done.answer.citations == ()
    assert done.answer.dropped_markers == 0


async def test_a_refusal_never_carries_citations_even_with_markers():
    """판정이 마커를 이긴다 — 인용을 남기면 거절문에 출처가 붙어 답변처럼 보인다."""
    harness = make_qa_harness(
        GenerationTurn(chunks=(VERDICT_INSUFFICIENT + "근거 [1] 로는 답할 수 없습니다.",))
    )
    await harness.ingest("policy.txt", POLICY)

    done = done_of(await harness.ask())

    assert done.answer.finish_reason is FinishReason.INSUFFICIENT_EVIDENCE
    assert done.answer.citations == ()
    assert done.answer.dropped_markers == 0, "정책이 무시한 마커를 버려진 마커로 셌다"
    assert "[1]" in done.answer.text, "본문의 마커는 지우지 않는다"


async def test_a_citation_from_a_pdf_carries_a_page_number():
    harness = make_qa_harness(answering("재택근무는 주 2회까지 가능합니다 [1]."))
    await harness.ingest_bytes(
        "policy.pdf",
        make_pdf(["재택근무는 주 2회까지 가능합니다.", "교육비는 연 200만원까지 지원합니다."]),
    )

    events = await harness.ask()

    citation = done_of(events).answer.citations[0]
    assert citation.location.page is not None
    assert 1 <= citation.location.page <= 2
