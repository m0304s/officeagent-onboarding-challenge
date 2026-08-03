"""오류 응답 형식과 예외 → HTTP 변환.

**변환은 이 계층에서만 한다.** 어댑터가 던지는 라이브러리 예외가 라우터까지 그대로 새지
않도록, 각 어댑터는 자기 경계에서 도메인 예외로 바꿔 던지고 여기서 HTTP 로 옮긴다.

모든 오류가 같은 봉투를 쓴다.

    {"error": {"code": "...", "message": "...", ...부가 항목}}

부가 항목은 **평평하게** 들어간다. 중첩 객체를 새로 만들지 않는 이유는 봉투 안에 형식이
둘이면 소비자가 파서를 둘 들어야 하기 때문이다.

프레임워크가 기본 제공하는 오류 응답(경로 없음·메서드 불허·요청 검증 실패)도 이 봉투로
덮어쓴다. 기본값을 그대로 두면 형식이 두 가지가 되어, 소비자가 파서를 둘 들고 있어야 한다.

**헬스 엔드포인트는 이 변환의 대상이 아니다.** 헬스는 라우트가 응답을 직접 반환하므로
예외 핸들러를 타지 않는다. 상태 보고와 오류 통지는 다른 일이다.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError, ErrorCode

logger = logging.getLogger(__name__)

HTTP_NOT_FOUND = 404
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_UNSUPPORTED_MEDIA_TYPE = 415
HTTP_UNPROCESSABLE_ENTITY = 422
HTTP_INTERNAL_SERVER_ERROR = 500
HTTP_SERVICE_UNAVAILABLE = 503

_STATUS_TO_CODE = {
    HTTP_NOT_FOUND: ErrorCode.NOT_FOUND,
    HTTP_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    # 라우터가 `HTTPException(422, ...)` 로 요청 형식 문제를 알릴 때 쓰인다. 프레임워크의
    # 검증 오류(`handle_validation_error`)와 같은 코드를 써야 소비자가 한 갈래로 다룬다.
    HTTP_UNPROCESSABLE_ENTITY: ErrorCode.VALIDATION_ERROR,
}

# 도메인 오류 코드 → HTTP 상태.
#
# **표가 API 계층에 있는 이유**는 "도메인 예외의 HTTP 변환은 이 계층에서만"이라는 규약
# 그대로다. `AppError` 에는 상태 코드 개념이 없고 `code` 만 있다 — 도메인이 HTTP 를 알면
# 같은 예외를 CLI 나 배치에서 재사용할 때 의미 없는 숫자를 들고 다니게 된다.
#
# **표에 없는 코드는 500이다.** 새 도메인 예외가 상태를 정하지 않은 채 들어와도 봉투는
# 그대로이고, 기존 예외의 동작도 바뀌지 않는다.
_CODE_TO_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNSUPPORTED_DOCUMENT_FORMAT: HTTP_UNSUPPORTED_MEDIA_TYPE,
    ErrorCode.DOCUMENT_TOO_LARGE: HTTP_PAYLOAD_TOO_LARGE,
    ErrorCode.EMPTY_DOCUMENT: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.NO_EXTRACTABLE_TEXT: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.DOCUMENT_PARSE_ERROR: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.NOT_FOUND: HTTP_NOT_FOUND,
    ErrorCode.STORAGE_UNAVAILABLE: HTTP_SERVICE_UNAVAILABLE,
    # 검색 요청 거부. 셋 다 422 다 — 요청 자체는 문법상 올바르고 값이 처리할 수 없는
    # 것이라, 프레임워크의 요청 검증 실패(`validation_error`)와 같은 상태를 쓴다.
    ErrorCode.EMPTY_QUERY: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.QUERY_TOO_LONG: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.INVALID_TOP_K: HTTP_UNPROCESSABLE_ENTITY,
}


def error_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    **extra: Any,
) -> JSONResponse:
    """공통 오류 봉투. `extra`는 봉투 구조를 바꾸지 않고 항목만 덧붙인다."""
    payload: dict[str, Any] = {"code": str(code), "message": message}
    payload.update(extra)
    return JSONResponse(status_code=status_code, content={"error": payload})


async def handle_app_error(_: Request, exc: Exception) -> JSONResponse:
    """도메인 예외 → HTTP. 메시지는 도메인이 스스로 정한 것이라 그대로 내보낸다.

    `exc.extra` 는 봉투에 **평평하게** 실린다. 지원 포맷 목록·적용된 크기 상한처럼
    소비자가 메시지 문자열을 파싱하지 않고 읽어야 하는 값이 여기로 온다. 중첩
    `details` 객체를 새로 만들지 않는 이유는 검증 오류의 `fields` 가 이미 평면이기
    때문이다 — 같은 봉투에 형식이 둘이면 소비자가 파서를 둘 들어야 한다.
    """
    assert isinstance(exc, AppError)
    message = exc.message or "요청을 처리할 수 없습니다"
    status = _CODE_TO_STATUS.get(exc.code, HTTP_INTERNAL_SERVER_ERROR)
    if status >= HTTP_INTERNAL_SERVER_ERROR:
        # 5xx 는 우리 잘못이다. 응답에는 남기지 않는 원인을 로그에는 남겨야 진단이 된다.
        logger.warning("도메인 오류를 %d 로 변환했습니다: %s", status, exc.code, exc_info=exc)
    return error_response(status, exc.code, message, **exc.extra)


async def handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    """프레임워크의 기본 오류 응답을 공통 봉투로 덮어쓴다."""
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    return error_response(exc.status_code, code, str(exc.detail))


async def handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    """요청 검증 실패.

    어떤 입력이 문제인지 식별할 수 있어야 하므로 위치와 사유를 함께 싣는다.
    다만 **입력 값 자체는 싣지 않는다** — 자격증명이 본문에 실려 온 경우 그대로 되비친다.
    """
    assert isinstance(exc, RequestValidationError)
    fields = [
        {
            "location": ".".join(str(part) for part in err.get("loc", ())),
            "reason": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    return error_response(
        HTTP_UNPROCESSABLE_ENTITY,
        ErrorCode.VALIDATION_ERROR,
        "요청 형식이 올바르지 않습니다",
        fields=fields,
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """최종 방어선.

    내부 정보(스택 트레이스·내부 식별자·접속 문자열)를 응답에서 지운다. 원인 추적에 필요한
    정보는 로그에만 남긴다 — 그래야 진단은 가능하면서 외부로는 새지 않는다.
    """
    logger.exception("처리되지 않은 오류: %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(
        HTTP_INTERNAL_SERVER_ERROR,
        ErrorCode.INTERNAL_ERROR,
        "요청을 처리하는 중 오류가 발생했습니다",
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
