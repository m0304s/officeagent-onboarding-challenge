"""파일명 → 파서 배선. 파서 교체 지점이다.

지원 포맷 목록을 `DocumentFormat` 열거형이 아니라 등록된 파서에서 만든다 — 열거형에서
만들면 파서를 뺀 구성에서도 그 포맷을 지원한다고 응답한다.
"""

from collections.abc import Sequence

from app.adapters.parsers.selection import PdfExtractionChoice
from app.adapters.parsers.text import TextParser
from app.adapters.protocols import DocumentParser
from app.core.documents import DocumentFormat, file_extension
from app.core.exceptions import UnsupportedDocumentFormat


class ParserRegistry:
    """확장자로 포맷과 파서를 찾는다."""

    def __init__(self, parsers: Sequence[DocumentParser]) -> None:
        table: dict[DocumentFormat, DocumentParser] = {}
        for parser in parsers:
            for document_format in parser.formats:
                if document_format in table:
                    # 조용히 덮어쓰면 어느 파서가 실제로 쓰이는지가 등록 순서에 달리고,
                    # 배선 실수가 기동이 아니라 업로드 시점에 드러난다.
                    raise ValueError(f"{document_format} 포맷에 파서가 둘 이상 등록되었다")
                table[document_format] = parser
        self._parsers = table

    @property
    def supported_formats(self) -> tuple[str, ...]:
        """오류 응답에 싣는 지원 포맷 목록. 정렬해 두어 응답이 등록 순서에 흔들리지 않는다."""
        return tuple(sorted(document_format.value for document_format in self._parsers))

    def resolve(self, filename: str) -> tuple[DocumentFormat, DocumentParser]:
        """파일명에서 포맷과 파서를 찾는다. 없으면 `UnsupportedDocumentFormat`.

        내용 스니핑으로 추측하지 않는다 — 틀리면 그 실패가 훨씬 설명하기 어려워진다."""
        extension = file_extension(filename)
        document_format = DocumentFormat.from_extension(extension) if extension else None
        parser = self._parsers.get(document_format) if document_format else None

        if document_format is None or parser is None:
            raise UnsupportedDocumentFormat(
                f"지원하지 않는 문서 포맷입니다: {extension or '(확장자 없음)'}",
                supported_formats=list(self.supported_formats),
            )
        return document_format, parser


def default_parsers(choice: PdfExtractionChoice) -> tuple[DocumentParser, ...]:
    """운영 구성. PDF 는 고른 방식의 파서 하나만 등록한다.

    기본 인자를 두지 않는 이유는 "서명은 지정했는데 파서는 기본값"이 계속 가능해져서다."""
    return (TextParser(), choice.parser)
