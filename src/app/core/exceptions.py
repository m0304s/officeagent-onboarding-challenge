"""도메인 예외.

API 계층만이 이 예외들을 HTTP 응답으로 변환한다. 어댑터가 던지는 라이브러리 예외가
라우터까지 그대로 새지 않도록, 각 어댑터는 자기 경계에서 이 예외로 바꿔 던진다.
"""

from enum import StrEnum


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


class AppError(Exception):
    """모든 도메인 예외의 기반.

    `code`는 응답 본문에 실리는 안정적인 식별자다. 이후 change가 하위 클래스를 추가하며
    확장한다. 메시지에 내부 구현 세부 정보를 담지 않는다.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(AppError):
    """설정 값이 없거나 형식이 맞지 않아 서비스를 기동할 수 없다.

    잘못된 설정으로 조용히 기동되는 것보다 기동에 실패하는 편이 낫다.
    """

    code = ErrorCode.CONFIGURATION_ERROR
