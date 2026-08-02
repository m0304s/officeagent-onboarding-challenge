"""수집 서비스 — 추출·분할 구간.

저장까지 이어지는 경로는 `test_ingestion_pipeline.py` 가 덮는다. 여기서 고정하는 것은
저장소를 건드리기 **전** 구간의 세 가지다. 포맷 판정이 레지스트리를 통해 이뤄지는지,
파싱이 이벤트 루프를 막지 않는지, 그리고 "내용이 없다"의 세 갈래(미지원 · 빈 문서 ·
텍스트 레이어 없음)가 서로 다른 예외로 갈라지는지.
"""

import asyncio

import pytest

from app.core.documents import DocumentFormat
from app.core.exceptions import (
    EmptyDocument,
    NoExtractableText,
    UnsupportedDocumentFormat,
)
from tests.ingestion_harness import LONG_KOREAN, make_service
from tests.pdf_fixtures import BLANK_PAGE, make_pdf
from tests.stubs import StubParser

# ── 성공 경로 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "expected_format"),
    [("policy.txt", DocumentFormat.TXT), ("guide.md", DocumentFormat.MD)],
)
async def test_text_documents_become_chunks(filename, expected_format):
    result = await make_service().extract_chunks(filename, LONG_KOREAN.encode("utf-8"))

    assert result.format is expected_format
    assert result.chunk_count > 1
    assert all(chunk.text for chunk in result.chunks)


async def test_chunk_text_is_a_substring_of_the_extracted_source():
    """청크 본문이 원문에 실재해야 출처 표기가 성립한다."""
    result = await make_service().extract_chunks("policy.txt", LONG_KOREAN.encode("utf-8"))

    for chunk in result.chunks:
        assert chunk.text in LONG_KOREAN


async def test_chunks_respect_the_configured_size():
    result = await make_service(size=200, overlap=40).extract_chunks(
        "policy.txt", LONG_KOREAN.encode("utf-8")
    )

    assert all(len(chunk.text) <= 200 for chunk in result.chunks)


async def test_pdf_chunks_carry_their_page_number():
    """출처 표기의 페이지 번호가 여기서 나온다. 쪽 경계를 넘는 청크는 없어야 한다."""
    data = make_pdf(["첫째 쪽 본문입니다.", "둘째 쪽 본문입니다."])

    result = await make_service().extract_chunks("manual.pdf", data)

    pages = {chunk.location.page for chunk in result.chunks}
    assert pages == {1, 2}
    assert result.page_count == 2


async def test_identity_is_derived_from_the_filename_and_the_bytes():
    service = make_service()
    data = LONG_KOREAN.encode("utf-8")

    first = await service.extract_chunks("policy.txt", data)
    same_name_new_content = await service.extract_chunks("policy.txt", data + b"!")

    assert first.document_id == same_name_new_content.document_id  # 같은 문서
    assert first.revision != same_name_new_content.revision  # 다른 내용
    assert first.byte_size == len(data)


# ── 오류 경로 — 세 갈래가 뭉개지지 않는다 ────────────────────────────────


async def test_unsupported_format_is_rejected_before_parsing():
    with pytest.raises(UnsupportedDocumentFormat):
        await make_service().extract_chunks("report.docx", b"whatever")


@pytest.mark.parametrize("data", [b"", b"   \n\n\t  "])
async def test_documents_without_content_are_empty_documents(data):
    with pytest.raises(EmptyDocument):
        await make_service().extract_chunks("policy.txt", data)


async def test_a_pdf_without_a_text_layer_is_not_an_empty_document():
    """스캔본과 빈 파일을 같은 코드로 뭉개면 클라이언트가 OCR 필요 여부를 알 수 없다."""
    data = make_pdf([BLANK_PAGE, BLANK_PAGE])

    with pytest.raises(NoExtractableText) as exc_info:
        await make_service().extract_chunks("scanned.pdf", data)

    assert not isinstance(exc_info.value, EmptyDocument)
    assert "OCR" in str(exc_info.value)
    assert exc_info.value.extra["page_count"] == 2


async def test_an_empty_pdf_is_an_empty_document_not_a_parse_error():
    """0 바이트가 `document_parse_error` 가 되면 빈 파일과 깨진 파일이 뭉개진다."""
    with pytest.raises(EmptyDocument):
        await make_service().extract_chunks("policy.pdf", b"")


async def test_only_pages_with_text_produce_chunks():
    data = make_pdf([BLANK_PAGE, "본문이 있는 쪽입니다.", BLANK_PAGE])

    result = await make_service().extract_chunks("partial.pdf", data)

    assert {chunk.location.page for chunk in result.chunks} == {2}
    assert result.page_count == 3


# ── 오프로드 ─────────────────────────────────────────────────────────────


async def test_parsing_does_not_block_the_event_loop():
    """파싱이 이벤트 루프 위에서 돌면 문서 하나가 서비스 전체를 멈춘다.

    파서 프로토콜을 동기로 선언한 이유가 이것이다 — 호출부가 오프로드를 의식하게 만든다.
    느린 파서를 주입하고, 그동안 다른 코루틴이 실제로 진행하는지를 본다.
    """
    delay = 0.2
    service = make_service(parsers=[StubParser(delay=delay)])
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(delay / 20)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        await service.extract_chunks("policy.txt", b"data")
    finally:
        ticker.cancel()

    assert ticks > 1, "파싱 동안 이벤트 루프가 멈췄다"
