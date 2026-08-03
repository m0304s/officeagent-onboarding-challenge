"""오류 응답 형식과 예외 → HTTP 변환.

`{"error": {"code", "message", ...부가 항목}}` 하나로 통일한다. 부가 항목이 평평한 것도
프레임워크 기본 응답을 덮어쓰는 것도 같은 이유다 — 소비자가 파서를 둘 들지 않게.
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
    # 프레임워크 검증 오류와 같은 코드를 써야 소비자가 한 갈래로 다룬다.
    HTTP_UNPROCESSABLE_ENTITY: ErrorCode.VALIDATION_ERROR,
}

# 표가 여기 있는 이유는 도메인이 HTTP 를 알면 같은 예외를 CLI 나 배치에서 재사용할 때
# 의미 없는 숫자를 들고 다니기 때문이다. 표에 없는 코드는 500 이다.
_CODE_TO_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNSUPPORTED_DOCUMENT_FORMAT: HTTP_UNSUPPORTED_MEDIA_TYPE,
    ErrorCode.DOCUMENT_TOO_LARGE: HTTP_PAYLOAD_TOO_LARGE,
    ErrorCode.EMPTY_DOCUMENT: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.NO_EXTRACTABLE_TEXT: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.DOCUMENT_PARSE_ERROR: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.NOT_FOUND: HTTP_NOT_FOUND,
    ErrorCode.STORAGE_UNAVAILABLE: HTTP_SERVICE_UNAVAILABLE,
    # 셋 다 422 — 문법은 맞고 값이 처리 불가라 프레임워크 검증 실패와 같은 상태다.
    ErrorCode.EMPTY_QUERY: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.QUERY_TOO_LONG: HTTP_UNPROCESSABLE_ENTITY,
    ErrorCode.INVALID_TOP_K: HTTP_UNPROCESSABLE_ENTITY,
    # 오늘 이 매핑을 타는 경로는 없다(둘 다 SSE `error` 로 나간다). 그래도 등록하는 것은
    # 비스트리밍 경로가 생겼을 때 표에 없는 코드가 조용히 500 이 되는 것을 막기 위해서다.
    ErrorCode.LLM_UNAVAILABLE: HTTP_SERVICE_UNAVAILABLE,
    ErrorCode.LLM_UNAUTHENTICATED: HTTP_SERVICE_UNAVAILABLE,
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
    """도메인 예외 → HTTP. 메시지는 도메인이 정한 것이라 그대로 내보낸다.

    `exc.extra` 를 평평하게 싣는다 — 검증 오류의 `fields` 가 이미 평면이라 형식을 맞춘다."""
    assert isinstance(exc, AppError)
    message = exc.message or "요청을 처리할 수 없습니다"
    status = _CODE_TO_STATUS.get(exc.code, HTTP_INTERNAL_SERVER_ERROR)
    if status >= HTTP_INTERNAL_SERVER_ERROR:
        # 5xx 는 우리 잘못이라, 응답에 안 남기는 원인을 로그에는 남겨야 진단이 된다.
        logger.warning("도메인 오류를 %d 로 변환했습니다: %s", status, exc.code, exc_info=exc)
    return error_response(status, exc.code, message, **exc.extra)


async def handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    """프레임워크의 기본 오류 응답을 공통 봉투로 덮어쓴다."""
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    return error_response(exc.status_code, code, str(exc.detail))


async def handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    """요청 검증 실패 — 위치와 사유를 싣는다.

    입력 값 자체는 싣지 않는다 — 자격증명이 본문에 실려 왔으면 그대로 되비친다."""
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

    내부 정보를 응답에서 지우고 로그에만 남긴다 — 진단은 되고 밖으로는 안 샌다."""
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
