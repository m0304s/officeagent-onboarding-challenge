"""일반 텍스트·마크다운 파서.

마크다운 문법을 걷어내지 않는다 — 걷어내면 제목이었다는 사실과 표의 열 구분이 사라져
검색 대상으로서 정보가 준다. 갈리는 것은 응답의 `format` 값뿐이다 (`ARCHITECTURE.md`).
"""

import codecs

from app.adapters.parsers.normalization import normalize_newlines
from app.core.documents import DocumentFormat, ExtractedDocument, TextSegment
from app.core.exceptions import DocumentParseError

# BOM 이 있으면 인코딩이 확정된다. UTF-32 를 추가하게 되면 순서가 중요해진다.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

# 순서가 뒤집히면 멀쩡한 UTF-8 문서가 깨진 글자로 저장된다 — cp949 는 UTF-8 한글
# 바이트열도 뜻 없는 글자로 디코딩해내는 경우가 있다 (`ARCHITECTURE.md`).
_ENCODINGS: tuple[str, ...] = ("utf-8", "cp949")


class TextParser:
    """`.txt` · `.md` 를 세그먼트 하나로 추출한다.

    쪽 개념이 없어 `page_count` 가 `None` 이고, 그래서 빈 추출이 스캔본 PDF 와 갈린다."""

    formats = frozenset({DocumentFormat.TXT, DocumentFormat.MD})

    def parse(self, data: bytes) -> ExtractedDocument:
        text = normalize_newlines(_decode(data))
        segments = (TextSegment(text=text),) if text.strip() else ()
        return ExtractedDocument(segments=segments)


def _decode(data: bytes) -> str:
    """바이트를 텍스트로 바꾼다. 어느 후보로도 읽히지 않으면 파싱 실패다.

    `errors="replace"` 를 쓰지 않는다 — 깨진 글자가 색인되면 인용된 근거가 읽히지 않는다."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return _decode_with(data, encoding)

    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise DocumentParseError(
        "텍스트 인코딩을 판별할 수 없습니다 (UTF-8 또는 CP949 로 저장해 주세요)"
    )


def _decode_with(data: bytes, encoding: str) -> str:
    """BOM 으로 인코딩이 확정된 경우. 여기서 실패하면 내용이 손상된 것이다."""
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise DocumentParseError("텍스트를 읽을 수 없습니다 (내용이 손상되었습니다)") from exc
