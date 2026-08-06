"""`DocumentParser` 구현체들과 파일명으로 그중 하나를 고르는 레지스트리."""

from app.adapters.parsers.pdf import PdfParser
from app.adapters.parsers.pdf_markdown import PdfMarkdownParser
from app.adapters.parsers.registry import ParserRegistry, default_parsers
from app.adapters.parsers.selection import (
    PDF_EXTRACTION_VERSION,
    PdfExtraction,
    PdfExtractionChoice,
    select_pdf_extraction,
)
from app.adapters.parsers.text import TextParser

__all__ = [
    "PDF_EXTRACTION_VERSION",
    "ParserRegistry",
    "PdfExtraction",
    "PdfExtractionChoice",
    "PdfMarkdownParser",
    "PdfParser",
    "TextParser",
    "default_parsers",
    "select_pdf_extraction",
]
