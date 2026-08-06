"""구조 보존 PDF 파서 — pymupdf4llm. 평문 파서(`pdf.py`)와 나란히 산다.

폴백을 두지 않는다. 표 탐지가 실패했다고 평문으로 물러서면 같은 설정에서 문서마다 다른
모양이 나오고, 추출 방식이 서명 축이라는 결정이 무의미해진다.
"""

import logging
from collections.abc import Sequence
from typing import Any

from app.adapters.parsers.normalization import normalize_newlines
from app.core.documents import DocumentFormat, ExtractedDocument, TextSegment
from app.core.exceptions import DocumentParseError

logger = logging.getLogger(__name__)

#: 이것만 남은 쪽은 내용이 없는 쪽이다 — 셀이 빈 격자는 `| |` 로, 구분선은 하이픈 줄로
#: 남아 `strip()` 판정만으로는 스캔본이 기호뿐인 청크로 저장된다.
_STRUCTURE_ONLY = set("#|-*> \t\n")


class PdfMarkdownParser:
    """쪽 단위로 마크다운을 추출한다. 제목은 헤딩으로, 격자 표는 마크다운 표로 나온다."""

    formats = frozenset({DocumentFormat.PDF})

    def parse(self, data: bytes) -> ExtractedDocument:
        # 0 바이트를 파서로 판정하지 않는다 — 빈 스트림에 "깨졌다" 계열 예외가 나
        # `empty_document` 와 뭉개진다 (`pdf.py` 와 같은 근거).
        if not data:
            return ExtractedDocument(page_count=0)

        # import 비용이 큰 C 확장이다. 최상단에 두면 PDF 를 한 번도 올리지 않는 배포도
        # 기동이 그만큼 느려진다.
        import pymupdf
        import pymupdf4llm

        _use_classic_path(pymupdf4llm)

        try:
            with pymupdf.open(stream=data, filetype="pdf") as document:
                if document.needs_pass:
                    raise DocumentParseError("암호로 보호된 PDF 는 읽을 수 없습니다")
                # 쪽 수의 출처가 텍스트 추출과 독립이어야 스캔본 판정이 성립한다.
                page_count = document.page_count
                pages = pymupdf4llm.to_markdown(
                    document,
                    page_chunks=True,
                    page_separators=False,
                    write_images=False,
                )
        except DocumentParseError:
            raise
        except Exception as exc:
            # 라이브러리 예외를 여기서 끊는다 — 라우터까지 새면 내부 메시지가 노출된다.
            logger.warning("PDF 마크다운 추출 실패", exc_info=exc)
            raise DocumentParseError("PDF 를 읽을 수 없습니다 (내용이 PDF 가 아닙니다)") from exc

        return ExtractedDocument(segments=_to_segments(pages), page_count=page_count)


def _to_segments(pages: Sequence[dict[str, Any]]) -> tuple[TextSegment, ...]:
    """쪽 조각을 세그먼트로 옮긴다. 본문은 변환 결과 그대로 둔다."""
    return tuple(
        TextSegment(text=normalize_newlines(str(page.get("text", ""))), page=_page_number(page))
        for page in pages
        if not _is_blank(str(page.get("text", "")))
    )


def _page_number(page: dict[str, Any]) -> int:
    """조각이 들고 있는 쪽 번호를 읽는다. 목록 순번으로 세지 않는다."""
    # 순번으로 세면 변환기가 쪽을 건너뛰는 순간 그 뒤 출처가 통째로 밀리는데 오류는
    # 나지 않는다 — 답변은 계속 나오고 인용만 틀린다. 조용한 실패를 던지는 실패로 바꾼다.
    metadata = page.get("metadata")
    number = metadata.get("page") if isinstance(metadata, dict) else None
    if not isinstance(number, int) or number < 1:
        raise DocumentParseError("PDF 쪽 번호를 확인할 수 없습니다")
    return number


def _use_classic_path(pymupdf4llm: Any) -> None:
    """레이아웃 경로를 끈다 (design 결정 8).

    라이브러리가 import 시점에 켜는데, 그 경로는 이미지가 든 문서 뒤 다음 문서를 훼손한다."""
    pymupdf4llm.use_layout(False)


def _is_blank(text: str) -> bool:
    """구조 기호만 남은 쪽은 텍스트가 없는 쪽으로 본다."""
    return set(text) <= _STRUCTURE_ONLY
