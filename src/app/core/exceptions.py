"""도메인 예외.

API 계층만 이것을 HTTP 응답으로 옮긴다. 어댑터는 자기 경계에서 라이브러리 예외를
이것으로 바꿔 던진다 — 라우터까지 새면 내부 메시지가 응답에 노출된다.
"""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """오류 응답에 실리는 안정적인 식별자.

    값은 한 번 정하면 바꾸지 않는다 — 소비자가 메시지를 파싱하지 않고 분기한다."""

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

    # 검색 (add-qa-retrieval)
    EMPTY_QUERY = "empty_query"
    QUERY_TOO_LONG = "query_too_long"
    INVALID_TOP_K = "invalid_top_k"

    # 답변 생성 — 둘을 가르는 이유는 소비자가 할 일이 다르기 때문이다(재시도 / 자격증명
    # 주입 경로 확인). 타임아웃은 스트림을 끝내지 않아 자기 코드를 갖지 않는다.
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_UNAUTHENTICATED = "llm_unauthenticated"


class AppError(Exception):
    """모든 도메인 예외의 기반.

    `extra` 로 구조화된 사실을 응답까지 나른다. 상태는 `api/errors.py` 의 표가 정한다."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra: dict[str, Any] = extra


class ConfigurationError(AppError):
    """설정이 없거나 형식이 틀려 기동할 수 없다. 조용히 기동되는 것보다 낫다."""

    code = ErrorCode.CONFIGURATION_ERROR


class UnsupportedDocumentFormat(AppError):
    """지원 목록에 없는 포맷이다. `extra` 에 지원 포맷 목록을 싣는다."""

    code = ErrorCode.UNSUPPORTED_DOCUMENT_FORMAT


class DocumentTooLarge(AppError):
    """업로드 크기 상한을 넘었다. `extra` 에 적용된 상한을 싣는다.

    막는 것은 메모리가 아니라 시간이다 — 수집이 요청 안에서 동기적으로 끝난다."""

    code = ErrorCode.DOCUMENT_TOO_LARGE


class EmptyDocument(AppError):
    """바이트가 없거나 추출 결과가 공백뿐이다."""

    code = ErrorCode.EMPTY_DOCUMENT


class NoExtractableText(AppError):
    """쪽은 있으나 텍스트 레이어가 없다 (스캔본 등).

    `EmptyDocument` 와 가른다 — 뭉개면 파일 손상과 OCR 필요가 같은 사건이 된다."""

    code = ErrorCode.NO_EXTRACTABLE_TEXT


class DocumentParseError(AppError):
    """파서가 내용을 읽지 못했다. 어댑터가 라이브러리 예외를 이것으로 바꿔 던진다."""

    code = ErrorCode.DOCUMENT_PARSE_ERROR


class DocumentNotFound(AppError):
    """수집된 문서가 없다. `NOT_FOUND` 를 재사용한다 — 소비자에게는 같은 사건이다."""

    code = ErrorCode.NOT_FOUND


class EmptyQuery(AppError):
    """질의가 비었거나 공백 문자뿐이다.

    프레임워크 검증에 안 맡긴다 — 공백 질의는 길이가 0 이 아니고, 수집도 도메인 코드로 알린다."""

    code = ErrorCode.EMPTY_QUERY


class QueryTooLong(AppError):
    """질의가 문자 수 상한이나 임베딩 입력 창을 넘었다.

    두 상한을 함께 싣고 자르지 않는다 — 조용한 절단은 사용자가 눈치채지 못한다."""

    code = ErrorCode.QUERY_TOO_LONG


class InvalidTopK(AppError):
    """`top_k` 가 설정된 상한을 넘었다. `extra` 에 적용된 상한을 싣는다.

    상한 초과에만 쓴다 — `>= 1` 은 배포와 무관한 사실이라 요청 스키마가 판정한다."""

    code = ErrorCode.INVALID_TOP_K


class LlmTimeout(AppError):
    """생성 한 시도가 시간 상한을 넘겼다.

    이것이 스트림의 끝은 아니라 `LlmGenerationFailed` 와 코드가 같다 — 구분은 사유가 한다."""

    code = ErrorCode.LLM_UNAVAILABLE


class LlmGenerationFailed(AppError):
    """시간 초과와 인증 부재를 뺀 생성 실패 전부.

    더 잘게 가르지 않는 이유는 서비스의 처분이 같기 때문이다 — 전부 재시도 대상이다."""

    code = ErrorCode.LLM_UNAVAILABLE


class LlmUnauthenticated(AppError):
    """생성기가 인증되지 않았다 — 자격증명이 없거나 만료됐다.

    재시도 대상이 아니다 — 백오프를 돌아도 자격증명이 생기지 않는다. 기동 조건도 아니다."""

    code = ErrorCode.LLM_UNAUTHENTICATED


class StorageUnavailable(AppError):
    """벡터 스토어나 레지스트리 쓰기가 실패했다.

    교체 도중이면 새 리비전 청크를 되돌리고 이전 리비전은 그대로 남는다."""

    code = ErrorCode.STORAGE_UNAVAILABLE
