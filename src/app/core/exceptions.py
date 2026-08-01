"""도메인 예외.

API 계층만이 이 예외들을 HTTP 응답으로 변환한다. 어댑터가 던지는 라이브러리 예외가
라우터까지 그대로 새지 않도록, 각 어댑터는 자기 경계에서 이 예외로 바꿔 던진다.
"""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """오류 응답에 실리는 안정적인 식별자.

    소비자가 문자열 메시지를 파싱하지 않고 분기할 수 있게 하는 것이 목적이므로, 값은
    한 번 정하면 바꾸지 않는다. 이후 change 는 여기에 항목을 **추가**하는 식으로 확장한다.
    """

    INTERNAL_ERROR = "internal_error"
    CONFIGURATION_ERROR = "configuration_error"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    VALIDATION_ERROR = "validation_error"

    # 문서 수집 (add-document-ingestion)
    UNSUPPORTED_DOCUMENT_FORMAT = "unsupported_document_format"
    DOCUMENT_TOO_LARGE = "document_too_large"
    EMPTY_DOCUMENT = "empty_document"
    NO_EXTRACTABLE_TEXT = "no_extractable_text"
    DOCUMENT_PARSE_ERROR = "document_parse_error"
    STORAGE_UNAVAILABLE = "storage_unavailable"


class AppError(Exception):
    """모든 도메인 예외의 기반.

    `code`는 응답 본문에 실리는 안정적인 식별자다. 이후 change가 하위 클래스를 추가하며
    확장한다. 메시지에 내부 구현 세부 정보를 담지 않는다.

    `extra`는 구조화된 사실을 오류 응답까지 나르기 위한 통로다 — 지원 포맷 목록,
    적용된 크기 상한처럼 소비자가 메시지 문자열을 파싱하지 않고 읽어야 하는 값.
    담기는 값은 도메인 사실뿐이라 `core/`가 HTTP 를 알게 되는 것은 아니다.

    **아직 응답까지 이어지지 않았다.** 현재 `api/errors.py`의 `handle_app_error`는
    `extra`를 읽지 않고 모든 `AppError`를 500으로 변환한다. 이 값을 봉투에 평평하게
    싣는 배선과 `ErrorCode → HTTP 상태` 표는 API 계층 태스크(7.5·7.6)에서 함께
    들어간다 — 둘 중 하나만 하면 415·413 이어야 할 응답이 500 인 채로 부가 정보만
    붙는 어중간한 상태가 된다.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra: dict[str, Any] = extra


class ConfigurationError(AppError):
    """설정 값이 없거나 형식이 맞지 않아 서비스를 기동할 수 없다.

    잘못된 설정으로 조용히 기동되는 것보다 기동에 실패하는 편이 낫다.
    """

    code = ErrorCode.CONFIGURATION_ERROR


class UnsupportedDocumentFormat(AppError):
    """지원 목록에 없는 포맷이다. `extra`에 지원 포맷 목록을 싣는다."""

    code = ErrorCode.UNSUPPORTED_DOCUMENT_FORMAT


class DocumentTooLarge(AppError):
    """업로드 크기 상한을 넘었다. `extra`에 적용된 상한을 싣는다.

    이 상한이 막는 것은 메모리가 아니라 시간이다 — 메모리는 배치 처리가 이미 문서
    크기와 무관하게 만들었다. 상한이 남는 이유는 수집이 요청 안에서 동기적으로
    끝나기 때문이며, 상한을 없애려면 배치가 아니라 작업 큐가 필요하다.
    """

    code = ErrorCode.DOCUMENT_TOO_LARGE


class EmptyDocument(AppError):
    """바이트가 없거나 추출 결과가 공백뿐이다."""

    code = ErrorCode.EMPTY_DOCUMENT


class NoExtractableText(AppError):
    """쪽은 있으나 텍스트 레이어가 없다 (스캔본 등).

    `EmptyDocument`와 **구분해야 한다.** 뭉개면 클라이언트가 "파일이 잘못됐다"와
    "OCR 이 필요하다"를 구분할 수 없다. 이 서비스는 OCR 을 하지 않는다.
    """

    code = ErrorCode.NO_EXTRACTABLE_TEXT


class DocumentParseError(AppError):
    """파서가 내용을 읽지 못했다.

    각 파서 어댑터가 자기 경계에서 라이브러리 예외를 이것으로 바꿔 던진다. 라우터까지
    라이브러리 예외가 새면 계층 경계가 무의미해지고, 내부 예외 메시지가 응답에 노출된다.
    """

    code = ErrorCode.DOCUMENT_PARSE_ERROR


class DocumentNotFound(AppError):
    """해당 `document_id`로 수집된 문서가 없다.

    새 코드를 만들지 않고 기존 `NOT_FOUND`를 재사용한다 — 소비자에게는 같은 사건이다.
    """

    code = ErrorCode.NOT_FOUND


class StorageUnavailable(AppError):
    """벡터 스토어나 레지스트리 쓰기가 실패했다.

    리비전 교체 도중 발생하면 서비스가 같은 요청 안에서 새 리비전 청크를 되돌리고
    이것으로 끝낸다. 이전 리비전은 그대로 남는다.
    """

    code = ErrorCode.STORAGE_UNAVAILABLE
