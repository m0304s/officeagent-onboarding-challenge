"""PDF 파서 — PyMuPDF.

**교체 지점은 이 파일 하나다.** 다른 파서로 바꾸려면 `DocumentParser` 프로토콜을 구현한
클래스를 만들어 레지스트리에 등록하면 되고, `services/`·`core/`·`api/` 는 한 줄도 바뀌지
않는다. 프로토콜이 반환하는 `ExtractedDocument` 가 쪽 단위 세그먼트라, 마크다운을 뱉는
모델 기반 파서도 "쪽별 텍스트"로 감싸 넣을 수 있다.

**라이선스 주의**: PyMuPDF 는 AGPL-3.0 이다. 평가용 비공개 리포 범위에서는 문제가 없지만
상용 배포로 간다면 pypdf(BSD) 교체가 선행되어야 한다. 교체 비용이 이 한 파일로 국한된다는
점이 이 구조의 값이다.
"""

import logging

from app.adapters.parsers.normalization import normalize_newlines
from app.core.documents import DocumentFormat, ExtractedDocument, TextSegment
from app.core.exceptions import DocumentParseError

logger = logging.getLogger(__name__)


class PdfParser:
    """쪽 단위로 텍스트를 추출한다.

    쪽을 세그먼트 경계로 유지하는 이유는 출처 표기다. 문자열 하나로 합쳐 돌려주면
    페이지 정보가 그 시점에 소실되고 되살릴 방법이 없다. 경계가 남아 있어야 청커가
    "청크는 페이지를 넘지 않는다"를 지킬 수 있고, 그래야 "이 청크는 몇 쪽인가"에
    답이 하나다.
    """

    formats = frozenset({DocumentFormat.PDF})

    def parse(self, data: bytes) -> ExtractedDocument:
        # 0 바이트를 파서로 판정하지 않는다. PyMuPDF 는 빈 스트림에 대해 "파일이 깨졌다"
        # 계열의 예외를 던지는데, 그대로 옮기면 빈 파일이 422 `document_parse_error` 가
        # 되어 `empty_document` 와 뭉개진다. 쪽도 텍스트도 없는 상태로 돌려주면 상위
        # 계층의 판정 규칙이 포맷과 무관하게 하나로 유지된다.
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
            # 라이브러리 예외를 여기서 끊는다. 라우터까지 `pymupdf.FileDataError` 가
            # 새면 계층 경계가 무의미해지고, 내부 예외 메시지가 응답에 노출된다.
            # 진단에 필요한 원문은 로그로만 남긴다.
            logger.warning("PDF 파싱 실패", exc_info=exc)
            raise DocumentParseError("PDF 를 읽을 수 없습니다 (내용이 PDF 가 아닙니다)") from exc

        segments = tuple(
            TextSegment(text=normalize_newlines(text), page=number)
            for number, text in enumerate(texts, start=1)
            if text.strip()
        )
        return ExtractedDocument(segments=segments, page_count=page_count)
