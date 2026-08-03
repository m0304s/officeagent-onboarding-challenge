"""PDF 파서 — PyMuPDF. 교체 지점은 이 파일 하나다.

PyMuPDF 는 AGPL-3.0 이라 상용 배포로 가면 pypdf(BSD) 교체가 선행되어야 한다. 그 비용이
한 파일로 국한된다는 점이 이 구조의 값이다 (`ARCHITECTURE.md` 문서 파서).
"""

import logging

from app.adapters.parsers.normalization import normalize_newlines
from app.core.documents import DocumentFormat, ExtractedDocument, TextSegment
from app.core.exceptions import DocumentParseError

logger = logging.getLogger(__name__)


class PdfParser:
    """쪽 단위로 텍스트를 추출한다.

    쪽 경계를 유지하는 이유는 출처 표기다 — 합쳐 돌려주면 페이지 정보를 되살릴 수 없다."""

    formats = frozenset({DocumentFormat.PDF})

    def parse(self, data: bytes) -> ExtractedDocument:
        # 0 바이트를 파서로 판정하지 않는다 — PyMuPDF 는 빈 스트림에 "깨졌다" 계열
        # 예외를 던져 `empty_document` 와 뭉개진다.
        if not data:
            return ExtractedDocument(page_count=0)

        # import 비용이 큰 C 확장이다. 모듈 최상단에 두면 PDF 를 한 번도 올리지 않는
        # 배포에서도 기동이 그만큼 느려진다.
        import pymupdf

        try:
            with pymupdf.open(stream=data, filetype="pdf") as document:
                if document.needs_pass:
                    raise DocumentParseError("암호로 보호된 PDF 는 읽을 수 없습니다")
                page_count = document.page_count
                texts = [page.get_text() for page in document]
        except DocumentParseError:
            raise
        except Exception as exc:
            # 라이브러리 예외를 여기서 끊는다 — 라우터까지 새면 내부 메시지가 노출된다.
            logger.warning("PDF 파싱 실패", exc_info=exc)
            raise DocumentParseError("PDF 를 읽을 수 없습니다 (내용이 PDF 가 아닙니다)") from exc

        segments = tuple(
            TextSegment(text=normalize_newlines(text), page=number)
            for number, text in enumerate(texts, start=1)
            if text.strip()
        )
        return ExtractedDocument(segments=segments, page_count=page_count)
