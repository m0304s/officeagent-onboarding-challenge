"""프롬프트 조립·출력 파싱·판정 줄 분리.

**LLM 도, 비동기도, 페이크도 없다 — 문자열 단언뿐이다.** 이 층을 순수 함수로 둔 이유가
그것이고(design 결정 10), 그래서 이 파일은 `pytest` 한 줄에서 구독·네트워크 없이 밀리초에
끝난다.

세 묶음이 있다.

1. **프롬프트 회귀** — 문맥에 근거가 번호·파일명·위치와 함께 들어가는가, 지시문이 환각을
   막는 문장들을 갖고 있는가. 마지막 테스트가 가장 중요하다: 프롬프트가 지시하는 판정 줄과
   파서가 인식하는 판정 줄이 **같은 문자열**인가. 둘이 갈리면 모델은 시킨 대로 쓰는데
   서버가 못 알아듣고, 그 증상은 "판정 줄 없음" 경로로 조용히 흡수된다.
2. **파서 경계** — design 결정 4의 표를 그대로 덮는다.
3. **판정 줄 분리** — 실측이 만든 부품이라(판정 한 줄이 델타 일곱 조각에 걸쳐 온다) 조각
   경계의 모든 경우를 덮고, **임의의 분할에서 `parse_answer` 와 결과가 같음**을 단언한다.
   그 일치가 "`answer` 이벤트를 이어 붙인 것 = `done.answer`" 라는 스펙 불변식의 근거다.
"""

import itertools
import re

import pytest

from app.core.documents import ChunkLocation, DocumentFormat
from app.core.prompting import (
    MAX_VERDICT_LINE_CHARS,
    PROMPT_VERSION,
    ParsedAnswer,
    Verdict,
    VerdictSplitter,
    build_prompt,
    parse_answer,
)
from app.core.retrieval import ScoredChunk


def chunk(**overrides) -> ScoredChunk:
    fields = {
        "document_id": "doc-1",
        "revision": "rev-1",
        "index_signature": "sig-1",
        "chunk_index": 0,
        "text": "교육비는 연 200만원까지 지원됩니다.",
        "location": ChunkLocation(char_start=120, char_end=540),
        "filename": "company-policy.txt",
        "format": DocumentFormat.TXT,
        "score": 0.8,
    }
    return ScoredChunk(**{**fields, **overrides})


PDF_CHUNK = chunk(
    document_id="doc-2",
    filename="development-guide.pdf",
    format=DocumentFormat.PDF,
    text="배포는 금요일에 하지 않는다.",
    location=ChunkLocation(char_start=0, char_end=40, page=3),
)


def emit(text: str, pieces: list[str] | None = None) -> tuple[VerdictSplitter, list[str]]:
    """조각들을 분리기에 통과시키고 `(분리기, 내보낸 조각들)` 을 돌려준다.

    `pieces` 를 주지 않으면 `text` 전체를 한 조각으로 넣는다. `finish()` 까지 부르는 것이
    계약의 일부다 — 개행 없이 끝나는 출력은 그 호출이 있어야 버퍼에서 나온다.
    """
    splitter = VerdictSplitter()
    emitted: list[str] = []
    for piece in pieces if pieces is not None else [text]:
        emitted.extend(splitter.feed(piece))
    emitted.extend(splitter.finish())
    return splitter, emitted


def every_splitting(text: str, parts: int):
    """`text` 를 빈 조각 없이 `parts` 개로 자르는 모든 방법."""
    for cuts in itertools.combinations(range(1, len(text)), parts - 1):
        bounds = (0, *cuts, len(text))
        yield [text[start:end] for start, end in itertools.pairwise(bounds)]


# ── 1. 프롬프트 조립 회귀 (2.5) ──────────────────────────────────────────


class TestContextCarriesEveryPieceOfEvidence:
    def test_every_source_appears_with_its_number(self):
        """번호가 곧 마커의 정의역이다 — 빠진 번호가 있으면 그 근거는 인용될 수 없다."""
        sources = [chunk(text=f"근거 본문 {n}") for n in range(1, 4)]

        prompt = build_prompt("질문", sources)

        for marker, source in enumerate(sources, start=1):
            assert f"[{marker}] " in prompt
            assert source.text in prompt

    def test_numbering_starts_at_one_and_has_no_gaps(self):
        prompt = build_prompt("질문", [chunk(), chunk(), chunk()])

        assert re.findall(r"^\[(\d+)\] ", prompt, flags=re.MULTILINE) == ["1", "2", "3"]

    def test_filename_is_shown_next_to_the_number(self):
        """모델이 "어느 문서에서 왔는가"를 답변 문장에 녹일 수 있어야 한다."""
        prompt = build_prompt("질문", [chunk(filename="company-policy.txt")])

        assert "[1] company-policy.txt" in prompt

    def test_text_source_shows_a_character_range(self):
        prompt = build_prompt("질문", [chunk(location=ChunkLocation(char_start=120, char_end=540))])

        assert "(문자 120–540)" in prompt

    def test_pdf_source_shows_a_page(self):
        """PDF 의 문자 오프셋은 **그 쪽 안의** 값이라 적으면 문서 전체 기준으로 오해된다."""
        prompt = build_prompt("질문", [PDF_CHUNK])

        assert "(3쪽)" in prompt
        assert "문자 0–40" not in prompt

    def test_question_is_in_the_prompt(self):
        prompt = build_prompt("교육비 지원 한도는 얼마인가요?", [chunk()])

        assert "교육비 지원 한도는 얼마인가요?" in prompt

    def test_evidence_comes_before_the_question(self):
        """질문을 마지막에 두는 것이 지시를 덮어쓰기 어렵게 만든다."""
        prompt = build_prompt("질문 문장", [chunk(text="근거 본문")])

        assert prompt.index("근거 본문") < prompt.index("질문 문장")


class TestInstructionsStateWhatMustNotHappen:
    def test_forbids_looking_outside_the_given_evidence(self):
        """에이전트 CLI 를 좁히는 네 겹 중 마지막 방어선이다 — 프롬프트 본문의 지시."""
        prompt = build_prompt("질문", [chunk()])

        assert "제공된 근거 밖을 조회하지 마십시오" in prompt

    def test_demands_a_refusal_instead_of_invention(self):
        prompt = build_prompt("질문", [chunk()])

        assert "지어내지" in prompt

    def test_demands_markers_on_sentences_taken_from_evidence(self):
        prompt = build_prompt("질문", [chunk()])

        assert "`[n]` 형태로" in prompt

    def test_demands_a_non_empty_body(self):
        """본문 없는 출력은 생성 실패로 분류되므로, 그 회차를 줄이는 것이 프롬프트의 몫이다."""
        prompt = build_prompt("질문", [chunk()])

        assert "본문을 비워 두지 마십시오" in prompt


class TestThePromptAndTheParserAgreeOnTheFormat:
    """**이 파일에서 가장 중요한 테스트다.**

    프롬프트가 지시하는 판정 줄과 파서가 인식하는 판정 줄이 갈리면, 모델은 시킨 대로 쓰는데
    서버가 못 알아듣는다. 그 증상은 오류가 아니라 "판정 줄 없음" 경로로 조용히 흡수되어
    `INSUFFICIENT` 가 영원히 나오지 않는 형태로만 드러난다 — 거절이 사라지는 것이라
    환각 억제가 통째로 무력해지는데 어디에도 실패가 남지 않는다.
    """

    def test_the_prompt_shows_exactly_the_lines_the_parser_accepts(self):
        prompt = build_prompt("질문", [chunk()])

        instructed = re.findall(r"^VERDICT: \w+$", prompt, flags=re.MULTILINE)

        assert len(instructed) == len(Verdict)
        for line in instructed:
            parsed = parse_answer(f"{line}\n본문", source_count=1)
            assert parsed.verdict_line_present, f"프롬프트가 지시한 {line!r} 을 파서가 모른다"

    def test_both_verdicts_are_reachable_from_the_prompt(self):
        prompt = build_prompt("질문", [chunk()])

        reachable = {
            parse_answer(f"{line}\n본문", source_count=1).verdict
            for line in re.findall(r"^VERDICT: \w+$", prompt, flags=re.MULTILINE)
        }

        assert reachable == set(Verdict)


class TestEmptyEvidenceNeverReachesTheModel:
    def test_building_a_prompt_without_evidence_is_an_error(self):
        """근거 0건이면 생성기를 부르지 않는다는 결정을 구조로 만든 자리다."""
        with pytest.raises(ValueError, match="근거"):
            build_prompt("질문", [])


class TestPromptVersion:
    def test_is_a_non_empty_stable_identifier(self):
        """다음 change 의 캐시 키에 들어갈 값이다 — 비면 프롬프트 개선이 캐시에 가려진다."""
        assert PROMPT_VERSION
        assert PROMPT_VERSION.strip() == PROMPT_VERSION


# ── 2. 파서 경계 (2.6) ───────────────────────────────────────────────────


class TestVerdictLine:
    def test_answerable_leaves_the_rest_as_the_answer(self):
        parsed = parse_answer("VERDICT: ANSWERABLE\n교육비는 200만원입니다 [1].", source_count=1)

        assert parsed.verdict is Verdict.ANSWERABLE
        assert parsed.body == "교육비는 200만원입니다 [1]."
        assert parsed.verdict_line_present

    def test_insufficient_leaves_the_rest_as_the_refusal(self):
        parsed = parse_answer("VERDICT: INSUFFICIENT\n근거에 연차 규정이 없습니다.", source_count=2)

        assert parsed.verdict is Verdict.INSUFFICIENT
        assert parsed.body == "근거에 연차 규정이 없습니다."

    def test_blank_lines_between_the_verdict_and_the_body_are_dropped(self):
        parsed = parse_answer("VERDICT: ANSWERABLE\n\n\n답변입니다.", source_count=1)

        assert parsed.body == "답변입니다."

    def test_a_missing_verdict_line_is_read_as_answerable(self):
        """형식 위반이 곧 환각은 아니다 — 그 본문도 근거만 주어진 프롬프트에서 나왔다."""
        parsed = parse_answer("교육비는 200만원입니다 [1].", source_count=1)

        assert parsed.verdict is Verdict.ANSWERABLE
        assert not parsed.verdict_line_present

    def test_a_missing_verdict_line_keeps_the_whole_output(self):
        """첫 줄을 잘라 버리면 짧은 답변에서는 답변 전체가 사라진다."""
        raw = "교육비는 200만원입니다.\n신청은 부서장 승인 후 가능합니다."

        assert parse_answer(raw, source_count=1).body == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "VERDICT: MAYBE\n본문",
            "verdict: answerable\n본문",
            "VERDICT:ANSWERABLE\n본문",
            "VERDICT: ANSWERABLE \n본문",
            " VERDICT: ANSWERABLE\n본문",
            "판정: ANSWERABLE\n본문",
        ],
    )
    def test_only_the_exact_line_counts_as_a_verdict(self, raw):
        """관대하게 받으면 분리기가 버퍼 상한을 가질 수 없다 — 뒤에 공백이 몇 개 더 올지 모른다.

        느슨한 인식의 대가는 스트리밍이고, 엄격한 인식의 대가는 형식 위반 회차에서
        판정 줄이 본문에 남는 것뿐이다. 후자가 훨씬 싸다.
        """
        parsed = parse_answer(raw, source_count=1)

        assert not parsed.verdict_line_present
        assert parsed.body == raw

    def test_a_verdict_line_with_no_body_parses_to_an_empty_body(self):
        parsed = parse_answer("VERDICT: ANSWERABLE", source_count=1)

        assert parsed.verdict is Verdict.ANSWERABLE
        assert parsed.body == ""


class TestBodyPresence:
    @pytest.mark.parametrize("raw", ["VERDICT: ANSWERABLE", "VERDICT: ANSWERABLE\n", ""])
    def test_output_without_a_body_is_reported_as_empty(self, raw):
        """빈 본문은 생성 실패로 분류돼 재시도에 태워진다 — 그 판정의 입력이 이 플래그다."""
        assert not parse_answer(raw, source_count=1).has_body

    def test_whitespace_only_body_counts_as_empty(self):
        assert not parse_answer("   \n\t ", source_count=1).has_body

    def test_a_real_body_is_reported_as_present(self):
        assert parse_answer("VERDICT: ANSWERABLE\n답변", source_count=1).has_body


class TestMarkers:
    def test_no_markers_is_a_valid_answer(self):
        """마커 없는 답변도 유효하다 — "답은 했는데 근거를 특정하지 못했다"."""
        parsed = parse_answer("VERDICT: ANSWERABLE\n답변입니다.", source_count=2)

        assert parsed.markers == ()
        assert parsed.dropped_markers == 0

    def test_markers_follow_their_first_appearance_in_the_body(self):
        parsed = parse_answer("VERDICT: ANSWERABLE\n먼저 [2] 그리고 [1].", source_count=2)

        assert parsed.markers == (2, 1)

    def test_a_repeated_marker_is_kept_once(self):
        parsed = parse_answer("VERDICT: ANSWERABLE\n[1] 그리고 또 [1].", source_count=2)

        assert parsed.markers == (1,)
        assert parsed.dropped_markers == 0

    def test_markers_outside_the_evidence_range_are_dropped_and_counted(self):
        """없는 근거를 가리키는 인용은 겉보기에 가장 그럴듯한 형태의 환각이다."""
        parsed = parse_answer("VERDICT: ANSWERABLE\n[1] 그리고 [7].", source_count=2)

        assert parsed.markers == (1,)
        assert parsed.dropped_markers == 1

    @pytest.mark.parametrize("raw_marker", ["[0]", "[3]", "[99]"])
    def test_zero_and_beyond_the_end_are_both_outside(self, raw_marker):
        parsed = parse_answer(f"VERDICT: ANSWERABLE\n{raw_marker}", source_count=2)

        assert parsed.markers == ()
        assert parsed.dropped_markers == 1

    def test_a_repeated_out_of_range_marker_counts_once(self):
        """세는 대상은 "잘못 가리킨 근거"이지 "잘못 적은 글자"가 아니다.

        뒤집으면 이 수가 답변 길이에 비례해 흔들려, 프롬프트 열화를 재는 신호로 쓸 수 없다.
        """
        parsed = parse_answer("VERDICT: ANSWERABLE\n[7] ... [7] ... [7]", source_count=2)

        assert parsed.dropped_markers == 1

    def test_markers_are_read_from_the_body_only(self):
        """판정 줄에 마커가 섞여 들어와도 본문의 인용으로 세지 않는다."""
        parsed = parse_answer("VERDICT: ANSWERABLE\n답변", source_count=1)

        assert parsed.markers == ()

    def test_markers_are_reported_even_for_a_refusal(self):
        """파서는 사실만 말한다 — 판정이 마커를 이기게 하는 것은 서비스의 정책이다."""
        parsed = parse_answer("VERDICT: INSUFFICIENT\n근거가 부족합니다 [1].", source_count=1)

        assert parsed.verdict is Verdict.INSUFFICIENT
        assert parsed.markers == (1,)

    def test_the_marker_stays_in_the_body(self):
        """지우면 스트림으로 흘러간 문장과 `done.answer` 가 달라진다."""
        parsed = parse_answer("VERDICT: ANSWERABLE\n교육비는 200만원입니다 [1].", source_count=1)

        assert "[1]" in parsed.body


class TestParsedAnswerDefaults:
    def test_an_answer_with_no_markers_defaults_to_empty(self):
        assert ParsedAnswer(verdict=Verdict.ANSWERABLE, body="본문").markers == ()


# ── 3. 판정 줄 분리 (2.7) ────────────────────────────────────────────────


class TestVerdictSplitterBoundaries:
    def test_verdict_and_body_in_one_piece(self):
        splitter, emitted = emit("VERDICT: ANSWERABLE\n교육비는 200만원입니다.")

        assert splitter.verdict is Verdict.ANSWERABLE
        assert emitted == ["교육비는 200만원입니다."]

    def test_the_shape_the_measurement_actually_produced(self):
        """실측에서 판정 줄 하나가 델타 일곱 조각에 걸쳐 왔다. 그때 이벤트가 나가면 안 된다."""
        splitter = VerdictSplitter()
        pieces = ["VER", "DICT", ": ANSW", "ERABLE", "\n"]

        assert [splitter.feed(piece) for piece in pieces] == [[], [], [], [], []]
        assert splitter.verdict is Verdict.ANSWERABLE
        assert splitter.feed("교육비는") == ["교육비는"]

    def test_insufficient_is_recognised_the_same_way(self):
        splitter, emitted = emit("", ["VERDICT: INSUF", "FICIENT\n", "근거가 부족합니다."])

        assert splitter.verdict is Verdict.INSUFFICIENT
        assert emitted == ["근거가 부족합니다."]

    def test_output_without_a_verdict_line_leaves_immediately(self):
        """하필 형식을 어긴 회차에서만 스트리밍이 죽는 일이 없어야 한다."""
        splitter = VerdictSplitter()

        assert splitter.feed("교육비는 200만원입니다.") == ["교육비는 200만원입니다."]
        assert splitter.settled
        assert splitter.verdict is None

    def test_the_first_character_of_a_verdictless_output_is_not_eaten(self):
        _, emitted = emit("", ["교", "육비는 200만원입니다."])

        assert "".join(emitted) == "교육비는 200만원입니다."

    def test_a_prefix_that_stops_being_a_prefix_is_flushed_whole(self):
        """`VERDICT` 로 시작하는 답변 문장도 있을 수 있다 — 붙들고 있으면 안 된다."""
        splitter = VerdictSplitter()

        assert splitter.feed("VERDICT") == []
        assert splitter.feed("가 무엇인지 설명하면") == ["VERDICT가 무엇인지 설명하면"]
        assert splitter.verdict is None

    def test_only_a_verdict_line_and_then_the_end(self):
        """본문 없는 출력. `finish()` 가 없으면 이 경우가 영원히 버퍼에 갇힌다."""
        splitter, emitted = emit("VERDICT: INSUFFICIENT")

        assert splitter.verdict is Verdict.INSUFFICIENT
        assert emitted == []

    def test_a_truncated_verdict_line_at_the_end_is_body(self):
        splitter, emitted = emit("VERDICT: ANSWERABL")

        assert splitter.verdict is None
        assert emitted == ["VERDICT: ANSWERABL"]

    def test_pieces_after_the_verdict_pass_through_one_for_one(self):
        """확정 뒤에는 조각 수와 이벤트 수가 같다 — 서버가 다시 만지지 않는다."""
        _, emitted = emit("", ["VERDICT: ANSWERABLE\n", "첫째", "둘째", "셋째"])

        assert emitted == ["첫째", "둘째", "셋째"]

    def test_the_piece_that_settles_the_verdict_carries_its_body_out(self):
        _, emitted = emit("", ["VERDICT: ANSW", "ERABLE\n본문 앞부분", "마지막"])

        assert emitted == ["본문 앞부분", "마지막"]

    def test_blank_lines_after_the_verdict_are_trimmed_without_stalling(self):
        _, emitted = emit("", ["VERDICT: ANSWERABLE\n", "\n", "\n답", "변"])

        assert emitted == ["답", "변"]

    def test_never_emits_an_empty_string(self):
        """빈 문자열을 내보내면 내용 없는 `answer` 이벤트가 나간다."""
        _, emitted = emit("", ["VERDICT: ANSWERABLE\n", "", "답변", ""])

        assert "" not in emitted

    def test_finish_is_idempotent_after_settling(self):
        splitter, _ = emit("VERDICT: ANSWERABLE\n본문")

        assert splitter.finish() == []


class TestVerdictSplitterHoldsBackNoMoreThanTheVerdictLine:
    def test_what_is_held_never_exceeds_the_longest_verdict_line(self):
        """무한정 자라는 경로가 없다는 것이 결정 10의 두 번째 규칙이 사는 이유다."""
        splitter = VerdictSplitter()
        held = ""

        for character in "VERDICT: INSUFFICIENT":
            emitted = splitter.feed(character)
            held = "" if emitted else held + character
            assert len(held) <= MAX_VERDICT_LINE_CHARS

    def test_a_long_body_is_never_held(self):
        splitter = VerdictSplitter()
        body = "가" * 10_000

        assert splitter.feed(body) == [body]


class TestTheSplitterAndTheParserNeverDisagree:
    """**"조각을 이어 붙인 것 = `done.answer`" 라는 스펙 불변식의 근거가 이 묶음이다.**

    서비스는 스트리밍에 `VerdictSplitter` 를, 종료 조립에 `parse_answer` 를 쓴다. 두 경로가
    본문을 다르게 잘라내면 클라이언트가 받는 두 값이 어긋나고, 그때 클라이언트는 무엇을
    표시해야 할지 알 수 없다. 같은 상수를 공유한다는 사실만으로는 부족해서 — 앞쪽 공백
    처리처럼 규칙이 두 벌인 자리가 있다 — 임의의 분할에 대해 직접 단언한다.
    """

    OUTPUTS = [
        "VERDICT: ANSWERABLE\n교육비는 200만원입니다 [1].",
        "VERDICT: INSUFFICIENT\n근거가 부족합니다.",
        "VERDICT: ANSWERABLE\n\n\n답변입니다.",
        "VERDICT: ANSWERABLE\n",
        "VERDICT: INSUFFICIENT",
        "VERDICT: ANSWERABL",
        "VERDICT: MAYBE\n본문입니다.",
        "VERDICT: ANSWERABLE \n본문입니다.",
        "교육비는 200만원입니다.\n둘째 줄입니다.",
        "VERDICT 라는 표시에 대하여",
        "짧다",
        "",
    ]

    @pytest.mark.parametrize("raw", OUTPUTS)
    @pytest.mark.parametrize("parts", [1, 2, 3])
    def test_the_streamed_body_equals_the_parsed_body(self, raw, parts):
        for pieces in every_splitting(raw, parts) if parts > 1 else [[raw]]:
            _, emitted = emit(raw, pieces)

            assert "".join(emitted) == parse_answer(raw, source_count=2).body, pieces

    @pytest.mark.parametrize("raw", OUTPUTS)
    @pytest.mark.parametrize("parts", [1, 2, 3])
    def test_the_streamed_verdict_equals_the_parsed_verdict(self, raw, parts):
        parsed = parse_answer(raw, source_count=2)

        for pieces in every_splitting(raw, parts) if parts > 1 else [[raw]]:
            splitter, _ = emit(raw, pieces)

            assert (splitter.verdict is not None) == parsed.verdict_line_present, pieces
            if parsed.verdict_line_present:
                assert splitter.verdict is parsed.verdict, pieces
