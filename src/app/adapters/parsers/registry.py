"""파일명 → 파서 배선.

**여기가 파서 교체 지점이다.** 레지스트리는 주입받은 파서 목록만 알고 구체 클래스를
직접 만들지 않으므로, 다른 PDF 파서로 바꾸는 일이 `default_parsers()` 한 줄 수정이 된다.
테스트도 같은 통로로 대역을 넣는다 — 모듈 전역 싱글턴을 두지 않는 기존 방침의 연장이다.

**"지원 포맷"의 유일한 근거도 여기다.** `DocumentFormat` 열거형이 아니라 실제로 등록된
파서에서 목록을 만든다. 열거형에서 만들면 파서를 뺀 구성에서도 그 포맷을 지원한다고
응답하게 되고, 그 응답은 허위다.
"""

from collections.abc import Sequence

from app.adapters.parsers.pdf import PdfParser
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

        확장자가 없는 경우도 같은 예외다 — 내용을 스니핑해 포맷을 추측하지 않는다.
        추측이 틀리면 사용자가 의도하지 않은 파서가 돌아 그 실패가 훨씬 설명하기 어려워진다.
        """
        extension = file_extension(filename)
        document_format = DocumentFormat.from_extension(extension) if extension else None
        parser = self._parsers.get(document_format) if document_format else None

        if document_format is None or parser is None:
            raise UnsupportedDocumentFormat(
                f"지원하지 않는 문서 포맷입니다: {extension or '(확장자 없음)'}",
                supported_formats=list(self.supported_formats),
            )
        return document_format, parser


def default_parsers() -> tuple[DocumentParser, ...]:
    """운영 구성. 앱 팩토리가 이것을 `ParserRegistry` 에 넣는다."""
    return (TextParser(), PdfParser())
