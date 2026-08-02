"""청킹.

검색 품질은 여기서 재지 않는다 — 그건 실물 모델과 질의 경로가 있어야 재는 것이고
retrieval change 의 몫이다. 여기서 고정하는 것은 **분할이 원문을 훼손하지 않는다**는
성질이다. 청크 본문이 원문의 부분 문자열이 아니거나, 경계에서 내용이 사라지거나,
청크가 모델 입력 창을 넘으면 검색 이전에 이미 틀린 것을 저장한 셈이 된다.
"""

import random

import pytest

from app.core.chunking import (
    CHUNK_SPLITTERS,
    ChunkStrategy,
    get_splitter,
    resplit,
    split_recursive,
)
from app.core.documents import ChunkLocation, TextChunk, TextSegment

SIZE = 100
OVERLAP = 20

# 문단·줄·한국어 문장 종결이 모두 들어 있는 산문. 구분자 우선순위가 실제로
# 작동하는지 보려면 경계가 여러 층으로 있어야 한다.
PROSE = (
    "재택근무는 주 2회까지 사용할 수 있습니다. 부서장 승인 후 인사 시스템에 등록하면 됩니다. "
    "등록하지 않은 재택은 근태에 반영되지 않습니다.\n"
    "교육비는 연 200만원 한도로 지원합니다. 도서 구입비도 같은 한도를 씁니다.\n\n"
    "연차는 입사일 기준으로 부여됩니다. 미사용 연차는 다음 해로 이월되지 않으니 "
    "연말 전에 소진해 주세요. 반차는 오전과 오후 중 선택할 수 있습니다."
)


def one_segment(text: str, page: int | None = None) -> list[TextSegment]:
    return [TextSegment(text=text, page=page)]


# ── 크기와 겹침 ──────────────────────────────────────────────────────────


def test_document_shorter_than_the_limit_becomes_one_chunk():
    """상한보다 짧으면 자를 이유가 없다. 본문은 앞뒤 공백을 덜어낸 원문 그대로다."""
    text = "  짧은 문서입니다. 자를 이유가 없다.  "

    chunks = split_recursive(one_segment(text), SIZE, OVERLAP)

    assert len(chunks) == 1
    assert chunks[0].text == text.strip()


def test_long_document_is_split_and_every_chunk_respects_the_limit():
    chunks = split_recursive(one_segment(PROSE), SIZE, OVERLAP)

    assert len(chunks) >= 2
    assert all(len(chunk.text) <= SIZE for chunk in chunks)


@pytest.mark.parametrize(("size", "overlap"), [(100, 20), (100, 50), (60, 30), (300, 60), (40, 10)])
def test_adjacent_chunks_overlap_in_the_source(size, overlap):
    """문단 경계에 걸친 문장이 양쪽 청크 어디에서도 온전히 읽히게 하는 성질이다.

    한 설정만 보면 특정 크기에서만 성립하는 구현을 통과시킨다. 겹침 비율을 바꿔 가며 본다.
    """
    chunks = split_recursive(one_segment(PROSE), size, overlap)
    ordered = sorted(chunks, key=lambda c: c.location.char_start)

    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert earlier.location.char_end > later.location.char_start


def test_a_whitespace_only_boundary_may_not_overlap():
    """겹칠 구간이 공백뿐이면 겹치지 않아도 된다 — 보존할 문맥이 없다.

    계약의 유일한 예외를 명시적으로 고정한다. 예외를 문서에만 적어 두면, 나중에 겹침이
    사라지는 진짜 회귀가 났을 때 "그 예외인가 보다" 하고 넘어가게 된다.
    """
    text = "a b c"  # 청크 사이가 공백 한 칸뿐인 극단적 설정

    chunks = split_recursive(one_segment(text), 3, 2)

    assert [chunk.text for chunk in chunks] == ["a", "b", "c"]
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        gap = text[earlier.location.char_end : later.location.char_start]
        assert not gap.strip(), "겹치지 않았다면 그 사이는 공백뿐이어야 한다"


def test_no_non_whitespace_character_is_lost():
    """경계에서 잘려 사라지는 내용이 없어야 한다.

    사라진 내용은 검색되지 않는데, 저장은 성공했으므로 아무도 눈치채지 못한다.
    """
    chunks = split_recursive(one_segment(PROSE), SIZE, OVERLAP)

    covered = set()
    for chunk in chunks:
        covered.update(range(chunk.location.char_start, chunk.location.char_end))

    missing = {i for i, char in enumerate(PROSE) if not char.isspace()} - covered
    assert not missing, f"원문에서 사라진 문자: {sorted(missing)[:10]}"


def test_chunks_strictly_advance_through_the_source():
    """청크는 앞으로 나아가야 한다 — 앞 청크에 통째로 담긴 청크가 나오면 안 된다.

    이 단언이 없으면 `['사내 복리후생 안내', '생 안내', '내']` 같은 결과가 다른 불변식을
    **전부 만족한 채** 통과한다. 구간 일치·상한·무손실·겹침 넷은 저런 조각도 지킨다.
    """
    chunks = split_recursive(one_segment(PROSE), SIZE, OVERLAP)

    for earlier, later in zip(chunks, chunks[1:], strict=False):
        assert later.location.char_start > earlier.location.char_start
        assert later.location.char_end > earlier.location.char_end


def test_an_early_lone_structural_boundary_does_not_shatter_the_document():
    """이른 자리에 구조 경계가 하나만 있을 때 거기서 끊고 멈추면 안 된다.

    회귀 테스트다. 자르기만 하고 **합치지 않으면** 짧은 제목 뒤에 문단 구분자가 없는
    문서에서 10자짜리 청크와 그 꼬리 조각들이 쏟아진다.
    """
    text = "사내 복리후생 안내\n\n" + ("재택근무는 주 2회까지 가능합니다. " * 40)

    chunks = split_recursive(one_segment(text), 600, 100)

    assert all(len(chunk.text) > 100 for chunk in chunks), [c.text for c in chunks]
    # 제목은 버려지지도, 혼자 청크가 되지도 않는다 — 뒤 본문과 합쳐진다.
    assert chunks[0].text.startswith("사내 복리후생 안내")
    assert len(chunks[0].text) > len("사내 복리후생 안내")


def test_chunk_text_is_exactly_the_span_it_points_at():
    """본문과 위치가 어긋나면 출처가 엉뚱한 곳을 가리킨다."""
    chunks = split_recursive(one_segment(PROSE), SIZE, OVERLAP)

    for chunk in chunks:
        span = PROSE[chunk.location.char_start : chunk.location.char_end]
        assert chunk.text == span


def test_long_token_without_any_separator_still_respects_the_limit():
    """구분자가 하나도 없으면 상한에서 그대로 자른다 — 상한도, 무손실도 지킨다."""
    text = "가" * (SIZE * 3 + 7)

    chunks = split_recursive(one_segment(text), SIZE, OVERLAP)

    assert all(len(chunk.text) <= SIZE for chunk in chunks)

    covered = set()
    for chunk in chunks:
        covered.update(range(chunk.location.char_start, chunk.location.char_end))
    assert covered == set(range(len(text))), "긴 토큰에서도 원문이 남김없이 덮여야 한다"


def test_whitespace_only_text_produces_no_chunks():
    """청크가 0개인 문서는 저장되지 않는다. 빈 청크를 만들어 채우지 않는다."""
    assert split_recursive(one_segment("   \n\n\t  "), SIZE, OVERLAP) == ()


def test_chunks_never_start_or_end_with_whitespace():
    chunks = split_recursive(one_segment(PROSE), SIZE, OVERLAP)

    assert all(chunk.text == chunk.text.strip() for chunk in chunks)


# ── 구분자 우선순위 ──────────────────────────────────────────────────────


def test_paragraph_boundary_is_preferred_over_a_mid_sentence_cut():
    """문단 경계가 상한 안에 있으면 거기서 끊는다.

    문장 중간을 자르면 그 조각이 검색됐을 때 출처로 보여줄 텍스트가 말이 되지 않는다.
    """
    first = "첫 문단입니다. 짧습니다."
    text = f"{first}\n\n두 번째 문단입니다. 이쪽도 짧습니다."

    chunks = split_recursive(one_segment(text), len(first) + 6, 5)

    assert chunks[0].text == first


def test_sentence_boundary_is_used_when_no_line_break_fits():
    sentence = "재택근무는 주 2회까지 사용할 수 있습니다. "
    text = sentence + "부서장 승인이 필요합니다. " + "인사 시스템에 등록합니다."

    chunks = split_recursive(one_segment(text), len(sentence) + 8, 5)

    assert chunks[0].text == sentence.strip()


# ── 세그먼트 경계 ────────────────────────────────────────────────────────


def test_chunks_never_cross_a_segment_boundary():
    """청크가 두 쪽에 걸치면 "몇 쪽인가"에 답이 둘이 되어 출처가 모호해진다."""
    segments = [
        TextSegment(text="첫 쪽 내용입니다.", page=1),
        TextSegment(text="둘째 쪽 내용입니다.", page=2),
    ]

    chunks = split_recursive(segments, SIZE, OVERLAP)

    assert [chunk.location.page for chunk in chunks] == [1, 2]
    assert chunks[0].text == "첫 쪽 내용입니다."
    assert chunks[1].text == "둘째 쪽 내용입니다."


def test_offsets_are_relative_to_their_own_segment():
    """PDF 의 주소는 (page, 오프셋)이다. 실재하지 않는 연결 문자열을 가정하지 않는다."""
    segments = [
        TextSegment(text="첫 쪽 내용입니다.", page=1),
        TextSegment(text="둘째 쪽 내용입니다.", page=2),
    ]

    chunks = split_recursive(segments, SIZE, OVERLAP)

    assert chunks[1].location.char_start == 0


def test_empty_segment_contributes_nothing():
    segments = [
        TextSegment(text="텍스트가 있는 쪽입니다.", page=1),
        TextSegment(text="", page=2),  # 텍스트 레이어가 없는 쪽
    ]

    chunks = split_recursive(segments, SIZE, OVERLAP)

    assert [chunk.location.page for chunk in chunks] == [1]


# ── 재분할 (토큰 가드가 부른다) ──────────────────────────────────────────


def test_resplit_makes_smaller_chunks_and_keeps_source_offsets():
    """재분할 뒤에도 출처는 원문의 같은 자리를 가리켜야 한다."""
    base = 1000
    body = PROSE[:200]
    chunk = TextChunk(
        text=body,
        location=ChunkLocation(char_start=base, char_end=base + len(body), page=3),
    )

    pieces = resplit(chunk, size=50, overlap=10)

    assert len(pieces) > 1
    assert all(len(piece.text) <= 50 for piece in pieces)
    assert all(piece.location.page == 3 for piece in pieces)
    for piece in pieces:
        start = piece.location.char_start - base
        end = piece.location.char_end - base
        assert piece.text == body[start:end]


def test_resplit_of_a_chunk_that_already_fits_returns_it_unchanged():
    chunk = TextChunk(text="짧은 청크", location=ChunkLocation(char_start=5, char_end=10))

    pieces = resplit(chunk, size=SIZE, overlap=OVERLAP)

    assert [piece.text for piece in pieces] == ["짧은 청크"]
    assert pieces[0].location.char_start == 5


# ── 전략 레지스트리 ──────────────────────────────────────────────────────


def test_every_declared_strategy_has_a_splitter_registered():
    """열거형에 값만 더하고 함수 등록을 빠뜨리면, 설정은 통과하는데 수집이 터진다."""
    assert set(CHUNK_SPLITTERS) == set(ChunkStrategy)


def test_default_strategy_resolves_to_the_recursive_splitter():
    assert get_splitter(ChunkStrategy.RECURSIVE) is split_recursive


@pytest.mark.parametrize(
    ("size", "overlap"),
    [(0, 0), (100, 100), (100, 200), (100, -1), (100, 0)],
)
def test_invalid_size_and_overlap_are_rejected(size, overlap):
    """겹침이 크기 이상이면 전진하지 않고, `0`이면 겹치지 않는다. 둘 다 즉시 실패한다.

    `(100, 0)`은 설정 계층이 이미 막지만 여기서도 막아야 한다. 순수 함수는 설정을
    거치지 않고 호출될 수 있어서, 한쪽만 막으면 계약이 호출 경로에 따라 달라진다.
    """
    with pytest.raises(ValueError):
        split_recursive(one_segment(PROSE), size, overlap)


def test_invariants_hold_across_generated_inputs():
    """다섯 불변식을 무작위 입력으로 확인한다.

    고정 픽스처는 내가 상상한 문서만 검사한다. 실제로 발견된 두 결함(이른 구조 경계에서
    청크가 부서지는 것, 앞 청크가 뒤 청크에 삼켜지는 것)은 전부 여기서 나왔다.
    시드를 고정해 실패가 재현되게 한다.
    """
    rng = random.Random(20260801)
    alphabet = ["가", "나", "다. ", "요. ", ". ", " ", "\n", "\n\n", "a", "Z", "\t", "  \n  "]

    for _ in range(400):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))
        size = rng.randint(2, 60)
        overlap = rng.randint(1, size - 1)
        chunks = split_recursive(one_segment(text), size, overlap)
        context = f"text={text!r} size={size} overlap={overlap}"

        covered = set()
        for chunk in chunks:
            start, end = chunk.location.char_start, chunk.location.char_end
            assert chunk.text == text[start:end], context
            assert len(chunk.text) <= size, context
            assert chunk.text == chunk.text.strip() and chunk.text, context
            covered.update(range(start, end))

        expected = {i for i, char in enumerate(text) if not char.isspace()}
        assert not expected - covered, context

        for earlier, later in zip(chunks, chunks[1:], strict=False):
            assert later.location.char_start > earlier.location.char_start, context
            assert later.location.char_end > earlier.location.char_end, context
            if earlier.location.char_end <= later.location.char_start:
                gap = text[earlier.location.char_end : later.location.char_start]
                assert not gap.strip(), context
