"""오류 응답 형식과 예외 → HTTP 변환.

**변환은 이 계층에서만 한다.** 어댑터가 던지는 라이브러리 예외가 라우터까지 그대로 새지
않도록, 각 어댑터는 자기 경계에서 도메인 예외로 바꿔 던지고 여기서 HTTP 로 옮긴다.

모든 오류가 같은 봉투를 쓴다.

    {"error": {"code": "...", "message": "..."}}

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
HTTP_UNPROCESSABLE_ENTITY = 422
HTTP_INTERNAL_SERVER_ERROR = 500

_STATUS_TO_CODE = {
    HTTP_NOT_FOUND: ErrorCode.NOT_FOUND,
    HTTP_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
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
    """도메인 예외 → HTTP. 메시지는 도메인이 스스로 정한 것이라 그대로 내보낸다."""
    assert isinstance(exc, AppError)
    message = exc.message or "요청을 처리할 수 없습니다"
    return error_response(HTTP_INTERNAL_SERVER_ERROR, exc.code, message)


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
