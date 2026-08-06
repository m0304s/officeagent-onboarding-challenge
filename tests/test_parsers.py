"""문서 파서 어댑터 — 원문 보존, 쪽 경계, 예외 격리.

"쪽은 있는데 텍스트가 없다"(스캔본)와 "내용 자체가 없다"(빈 파일)를 구분해 돌려주는지도
여기서 고정한다 — 두 경우는 클라이언트가 할 일이 다르다.
"""

import pytest

from app.adapters.parsers import (
    ParserRegistry,
    PdfExtraction,
    PdfMarkdownParser,
    PdfParser,
    TextParser,
    default_parsers,
    select_pdf_extraction,
)
from app.adapters.protocols import DocumentParser
from app.core.documents import DocumentFormat, ExtractedDocument
from app.core.exceptions import DocumentParseError, UnsupportedDocumentFormat
from tests.pdf_fixtures import (
    BLANK_PAGE,
    SCANNED_PAGES,
    STRUCTURED_PAGES,
    make_encrypted_pdf,
    make_layered_and_scanned_pdf,
    make_pdf,
    make_scanned_pdf,
    make_structured_pdf,
)

KOREAN = "사내 복리후생 안내\n\n교육비는 연 200만원까지 지원한다."


# ── 프로토콜 준수 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("parser", [TextParser(), PdfParser(), PdfMarkdownParser()])
def test_parsers_satisfy_the_protocol(parser):
    """레지스트리가 구체 타입이 아니라 프로토콜에만 의존해야 교체가 성립한다."""
    assert isinstance(parser, DocumentParser)
    assert parser.formats


def test_every_declared_format_has_a_parser_in_the_default_wiring():
    """열거형에 있는데 파서가 없는 포맷이 있으면 "지원한다"가 거짓이 된다.

    거짓의 방향은 둘 중 하나여야 한다 — 열거형에서 빼거나, 파서를 만들거나."""
    choice = select_pdf_extraction(PdfExtraction.MARKDOWN)
    registry = ParserRegistry(default_parsers(choice))

    assert set(registry.supported_formats) == {f.value for f in DocumentFormat}


# ── 텍스트·마크다운 ─────────────────────────────────────────────────────


def test_text_document_becomes_one_segment_with_the_original_text():
    extracted = TextParser().parse(KOREAN.encode("utf-8"))

    assert len(extracted.segments) == 1
    assert extracted.segments[0].text == KOREAN
    assert extracted.segments[0].page is None


def test_text_has_no_page_count():
    """쪽 개념이 없다. 0 으로 채우면 "쪽이 0개인 PDF"와 구분되지 않는다."""
    assert TextParser().parse(b"hello").page_count is None


def test_markdown_syntax_is_kept_as_is():
    """마크다운 문법을 걷어내지 않는다.

    걷어내면 제목이었다는 사실과 표의 열 구분이 사라져 검색 품질이 오히려 떨어진다."""
    source = "# 개발 가이드\n\n- 코드 리뷰는 2인 승인\n\n| 환경 | 브랜치 |\n|---|---|\n"

    extracted = TextParser().parse(source.encode("utf-8"))

    assert extracted.segments[0].text == source


def test_windows_newlines_are_normalized():
    r"""`\r\n` 이 남으면 문단 경계가 최우선 구분자 `\n\n` 에 걸리지 않는다.

    같은 내용의 문서가 만들어진 운영체제에 따라 다르게 잘리게 된다."""
    extracted = TextParser().parse(b"first\r\n\r\nsecond\rthird")

    assert extracted.segments[0].text == "first\n\nsecond\nthird"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("한글".encode(), "한글"),  # UTF-8
        (b"\xef\xbb\xbf" + "한글".encode(), "한글"),  # UTF-8 BOM 은 본문에 남지 않는다
        ("한글".encode("cp949"), "한글"),  # 윈도우 메모장 레거시
        ("한글".encode("utf-16"), "한글"),  # BOM 이 있으면 UTF-16 도 읽는다
    ],
)
def test_common_korean_encodings_are_decoded(data, expected):
    assert TextParser().parse(data).segments[0].text == expected


def test_undecodable_bytes_are_a_parse_error_not_replacement_characters():
    """깨진 글자로 채워 넣지 않는다.

    `errors="replace"` 로 넘기면 인용된 근거가 읽을 수 없는 문자열이 된다."""
    with pytest.raises(DocumentParseError):
        TextParser().parse(b"\x80\xff\x80\xff")


def test_parse_errors_do_not_leak_the_library_exception():
    """응답에 내부 예외 메시지가 실리지 않는다 — 메시지는 우리가 쓴 것이어야 한다."""
    with pytest.raises(DocumentParseError) as exc_info:
        TextParser().parse(b"\x80\x81\x82\x83\x84\x85")

    message = str(exc_info.value)
    assert "UnicodeDecodeError" not in message
    assert "codec" not in message


@pytest.mark.parametrize("data", [b"", b"   \n\n\t  \r\n "])
def test_documents_without_text_yield_no_segments(data):
    """공백뿐인 문서는 세그먼트가 0개다.

    쪽 수도 없으므로 상위 계층에서 "빈 문서"로 판정된다 — 스캔본 PDF 와 갈라지는 지점이 여기다."""
    extracted = TextParser().parse(data)

    assert extracted.segments == ()
    assert extracted.has_text is False
    assert extracted.page_count is None


# ── PDF ──────────────────────────────────────────────────────────────────

#: 두 구현을 한 계약에 묶는다. 상속으로 공통 로직을 뽑지 않는 근거는 `tests/README.md`
#: 에 있다 — 지켜야 하는 것은 구현의 공유가 아니라 관측되는 계약의 동일성이다.
PDF_MODES = [PdfExtraction.PLAIN, PdfExtraction.MARKDOWN]


@pytest.fixture(params=PDF_MODES, ids=[mode.value for mode in PDF_MODES])
def pdf_parser(request) -> DocumentParser:
    return select_pdf_extraction(request.param).parser


def markdown_parser() -> DocumentParser:
    return select_pdf_extraction(PdfExtraction.MARKDOWN).parser


# ── 방식에 무관한 계약 ──────────────────────────────────────────────────


def test_pdf_yields_one_segment_per_page_with_one_based_numbers(pdf_parser):
    extracted = pdf_parser.parse(make_pdf(["first page", "second page"]))

    assert extracted.page_count == 2
    assert [segment.page for segment in extracted.segments] == [1, 2]
    assert "first page" in extracted.segments[0].text
    assert "second page" in extracted.segments[1].text


def test_page_numbers_never_exceed_the_page_count(pdf_parser):
    """쪽 번호가 쪽 수를 넘으면 출처가 존재하지 않는 쪽을 가리킨다."""
    extracted = pdf_parser.parse(make_pdf(["a", "b", "c"]))

    assert extracted.page_count == 3
    assert all(1 <= segment.page <= 3 for segment in extracted.segments)


def test_pdf_text_is_extracted_for_korean(pdf_parser):
    """샘플 문서가 한국어다. 여기서 깨지면 이후 전부가 무의미해진다."""
    extracted = pdf_parser.parse(make_pdf(["사내 복리후생 안내"]))

    assert "사내 복리후생 안내" in extracted.segments[0].text


def test_pdf_without_a_text_layer_keeps_its_page_count(pdf_parser):
    """스캔본은 "쪽은 있는데 텍스트가 없다"로 드러나야 한다.

    `page_count` 를 버리면 빈 파일과 구분되지 않아 두 경우가 같은 오류 코드가 된다."""
    extracted = pdf_parser.parse(make_pdf([BLANK_PAGE, BLANK_PAGE]))

    assert extracted.segments == ()
    assert extracted.has_text is False
    assert extracted.page_count == 2


def test_image_only_pdf_yields_no_text(pdf_parser):
    """글자가 이미지로만 있는 쪽은 텍스트가 없는 쪽이다 — OCR 은 이번 범위 밖이다."""
    extracted = pdf_parser.parse(make_scanned_pdf())

    assert extracted.has_text is False
    assert extracted.page_count == len(SCANNED_PAGES)


def test_pages_without_text_are_skipped_and_numbering_still_matches(pdf_parser):
    """일부 쪽에만 텍스트가 있는 PDF 는 그 쪽에서만 청크가 나온다.

    번호는 원본 쪽 번호여야 출처가 성립한다."""
    extracted = pdf_parser.parse(make_pdf([BLANK_PAGE, "본문이 있는 쪽", BLANK_PAGE]))

    assert [segment.page for segment in extracted.segments] == [2]
    assert extracted.page_count == 3


def test_empty_bytes_are_not_reported_as_a_broken_pdf(pdf_parser):
    """0 바이트를 파싱 실패로 옮기면 빈 파일이 422 `document_parse_error` 가 된다.

    `empty_document` 와 뭉개지므로, 쪽도 텍스트도 없는 상태로 돌려준다."""
    extracted = pdf_parser.parse(b"")

    assert extracted.segments == ()
    assert extracted.page_count == 0


def test_non_pdf_bytes_with_a_pdf_extension_raise_a_domain_error(pdf_parser):
    with pytest.raises(DocumentParseError):
        pdf_parser.parse(b"this is definitely not a pdf" * 10)


def test_password_protected_pdf_is_rejected(pdf_parser):
    """암호 PDF 를 빈 문서로 돌려주면 "내용이 없는 문서"로 조용히 수집된다."""
    with pytest.raises(DocumentParseError):
        pdf_parser.parse(make_encrypted_pdf())


def test_pdf_parse_error_does_not_leak_the_library_exception(pdf_parser, caplog):
    """`pymupdf.FileDataError` 가 라우터까지 새면 계층 경계가 무의미해진다.

    진단에 필요한 원문은 사라지면 안 되므로 로그에는 남아 있어야 한다."""
    with pytest.raises(DocumentParseError) as exc_info:
        pdf_parser.parse(b"%PDF-1.7 broken")

    message = str(exc_info.value)
    assert "pymupdf" not in message.lower()
    assert "mupdf" not in message.lower()
    assert caplog.records, "원인은 로그로 남아야 한다"


def test_a_previous_document_does_not_change_the_next_extraction(pdf_parser):
    """이미지가 든 문서를 먼저 읽어도 다음 문서의 본문이 같아야 한다.

    라이브러리의 레이아웃 경로가 다시 켜지면 여기서 깨진다 (design 결정 8)."""
    alone = [segment.text for segment in pdf_parser.parse(make_structured_pdf()).segments]

    pdf_parser.parse(make_layered_and_scanned_pdf("레이어에만", "이미지에만"))
    after = [segment.text for segment in pdf_parser.parse(make_structured_pdf()).segments]

    assert after == alone


# ── 구조 보존 (markdown 방식) ───────────────────────────────────────────


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _headings(text: str) -> list[str]:
    return [line.strip() for line in _lines(text) if line.lstrip().startswith("#")]


def test_headings_are_marked_and_larger_type_gets_a_higher_level():
    """조판 크기의 차이가 헤딩 레벨의 차이로 남아야 후속 계층형 색인의 재료가 된다."""
    page = STRUCTURED_PAGES[0]

    text = markdown_parser().parse(make_structured_pdf()).segments[0].text
    headings = _headings(text)
    levels = {
        heading.split(" ", 1)[1]: len(heading) - len(heading.lstrip("#")) for heading in headings
    }

    assert page.heading in levels and page.subheading in levels
    assert levels[page.heading] < levels[page.subheading]


def test_body_lines_do_not_get_heading_markers():
    """본문까지 제목으로 승격되면 레벨이 아무것도 구분하지 못한다."""
    page = STRUCTURED_PAGES[0]

    text = markdown_parser().parse(make_structured_pdf()).segments[0].text

    for line in page.body:
        assert line in text
        assert not any(line in heading for heading in _headings(text))


def test_table_rows_keep_their_cells_on_one_line():
    """셀이 행 순서대로 흩어지면 어느 값이 어느 항목의 것인지 복원할 수 없다."""
    page = STRUCTURED_PAGES[0]

    text = markdown_parser().parse(make_structured_pdf()).segments[0].text
    rows = [line for line in _lines(text) if line.strip().startswith("|")]

    assert rows, "표 표기가 있어야 한다"
    for left, right in page.table:
        assert any(left in row and right in row for row in rows), f"{left}/{right} 가 한 줄에 없다"


def test_original_sentences_and_numbers_survive_unchanged():
    """표기는 구조를 나타내는 기호다. 원문 문장과 수치는 그대로 남는다."""
    page = STRUCTURED_PAGES[0]

    text = markdown_parser().parse(make_structured_pdf()).segments[0].text

    assert all(line in text for line in page.body)
    assert all(cell in text for row in page.table for cell in row)


def test_the_two_modes_produce_different_text_for_the_same_pdf():
    """둘이 같은 결과를 내면 설정 축이 아무것도 고르지 못한다는 뜻이다."""
    data = make_structured_pdf()

    markdown = markdown_parser().parse(data).segments[0].text
    plain = select_pdf_extraction(PdfExtraction.PLAIN).parser.parse(data).segments[0].text

    assert markdown != plain
    assert "#" in markdown and "|" in markdown
    assert "#" not in plain and "|" not in plain


def test_structure_does_not_break_page_boundaries():
    """쪽마다 제목과 표가 있어도 서로 다른 쪽의 내용이 한 세그먼트에 섞이지 않는다."""
    extracted = markdown_parser().parse(make_structured_pdf())

    assert [segment.page for segment in extracted.segments] == [1, 2]
    for segment, source in zip(extracted.segments, STRUCTURED_PAGES, strict=True):
        assert source.heading in segment.text
        others = [page for page in STRUCTURED_PAGES if page is not source]
        assert all(other.heading not in segment.text for other in others)


# ── 레지스트리 ──────────────────────────────────────────────────────────


@pytest.fixture
def registry() -> ParserRegistry:
    choice = select_pdf_extraction(PdfExtraction.MARKDOWN)
    return ParserRegistry(default_parsers(choice))


@pytest.mark.parametrize(
    ("filename", "expected_format", "expected_parser"),
    [
        ("policy.txt", DocumentFormat.TXT, TextParser),
        ("guide.md", DocumentFormat.MD, TextParser),
        ("manual.pdf", DocumentFormat.PDF, PdfMarkdownParser),
    ],
)
def test_extension_selects_the_format_and_the_parser(
    registry, filename, expected_format, expected_parser
):
    document_format, parser = registry.resolve(filename)

    assert document_format is expected_format
    assert isinstance(parser, expected_parser)


def test_one_parser_can_serve_two_formats():
    """`.txt` 와 `.md` 는 추출 방식이 같지만 응답의 `format` 값은 달라야 한다.

    파서에 단일 `format` 속성을 뒀다면 둘 중 하나가 거짓이 된다."""
    registry = ParserRegistry([TextParser()])

    txt_format, txt_parser = registry.resolve("a.txt")
    md_format, md_parser = registry.resolve("a.md")

    assert txt_format is not md_format
    assert txt_parser is md_parser


@pytest.mark.parametrize("filename", ["POLICY.TXT", "/home/user/Policy.Md", "a.PDF"])
def test_extension_matching_ignores_case_and_client_paths(registry, filename):
    """업로드 파일명에 섞여 오는 경로와 대소문자는 포맷 판정과 무관하다."""
    assert registry.resolve(filename)


@pytest.mark.parametrize("filename", ["report.docx", "archive.zip", "README", ".gitignore"])
def test_unsupported_or_missing_extensions_are_rejected(registry, filename):
    """확장자가 없으면 내용을 스니핑해 추측하지 않는다.

    추측이 틀리면 의도하지 않은 파서가 돌아 실패가 훨씬 설명하기 어려워진다."""
    with pytest.raises(UnsupportedDocumentFormat):
        registry.resolve(filename)


def test_the_error_carries_the_supported_formats(registry):
    """오류 응답에서 지원 포맷 목록을 확인할 수 있어야 한다 (spec 요구).

    메시지 문자열에 섞으면 소비자가 파싱해야 하므로 구조화된 값으로 나른다."""
    with pytest.raises(UnsupportedDocumentFormat) as exc_info:
        registry.resolve("report.docx")

    assert sorted(exc_info.value.extra["supported_formats"]) == ["md", "pdf", "txt"]


def test_the_supported_list_comes_from_what_is_registered_not_from_the_enum():
    """PDF 파서를 뺀 구성에서 `.pdf` 를 지원한다고 응답하면 허위 기재다."""
    registry = ParserRegistry([TextParser()])

    with pytest.raises(UnsupportedDocumentFormat) as exc_info:
        registry.resolve("manual.pdf")

    assert "pdf" not in exc_info.value.extra["supported_formats"]


def test_two_parsers_claiming_the_same_format_fail_at_wiring_time():
    """조용히 덮어쓰면 어느 파서가 쓰이는지가 등록 순서에 달린다.

    배선 실수가 기동이 아니라 업로드 시점에 드러나면 진단이 훨씬 어렵다."""
    with pytest.raises(ValueError):
        ParserRegistry([TextParser(), TextParser()])


def test_the_registry_only_knows_the_protocol():
    """대역 파서를 넣을 수 있어야 한다 — 이후 서비스 테스트가 이 통로로 지연을 주입한다."""

    class StubParser:
        formats = frozenset({DocumentFormat.TXT})

        def parse(self, data: bytes) -> ExtractedDocument:
            return ExtractedDocument()

    _, parser = ParserRegistry([StubParser()]).resolve("a.txt")

    assert isinstance(parser, StubParser)
