"""`DocumentParser` 구현체들과 파일명으로 그중 하나를 고르는 레지스트리."""

from app.adapters.parsers.pdf import PdfParser
from app.adapters.parsers.registry import ParserRegistry, default_parsers
from app.adapters.parsers.text import TextParser

__all__ = ["ParserRegistry", "PdfParser", "TextParser", "default_parsers"]
