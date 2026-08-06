"""추출 방식 → 구현 배선. 이 축의 유일한 진실이다.

파서와 색인 서명 재료가 한 객체에서 함께 나온다 — 따로 꺼내면 마크다운으로 뽑은 청크가
평문 서명으로 저장되어도 아무 데서도 오류가 나지 않는다.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.adapters.parsers.pdf import PdfParser
from app.adapters.parsers.pdf_markdown import PdfMarkdownParser
from app.adapters.protocols import DocumentParser


class PdfExtraction(StrEnum):
    """구현된 PDF 추출 방식. 결과의 모양으로 이름 붙여 라이브러리 교체가 설정을 깨지 않는다."""

    MARKDOWN = "markdown"
    PLAIN = "plain"


#: `pymupdf4llm` 핀을 올리거나 후처리를 고치면 이 값도 함께 올린다. 산출물이 달라져도
#: 방식 이름은 그대로라, 서명이 그것을 모르면 옛 청크가 새 청크와 한 인덱스에 남는다.
PDF_EXTRACTION_VERSION = 1


@dataclass(frozen=True)
class PdfExtractionChoice:
    """고른 방식과 그 구현."""

    mode: PdfExtraction
    parser: DocumentParser

    @property
    def signature_material(self) -> str:
        """색인 서명에 넣을 재료. 이름과 버전을 한 값으로 낸다."""
        return f"{self.mode.value}:v{PDF_EXTRACTION_VERSION}"


_PARSER_FACTORIES: dict[PdfExtraction, Callable[[], DocumentParser]] = {
    PdfExtraction.MARKDOWN: PdfMarkdownParser,
    PdfExtraction.PLAIN: PdfParser,
}


def select_pdf_extraction(mode: PdfExtraction) -> PdfExtractionChoice:
    """방식 하나를 골라 파서와 서명 재료를 함께 낸다."""
    return PdfExtractionChoice(mode=mode, parser=_PARSER_FACTORIES[mode]())
