"""구조화 로깅과 요청 단위 로그.

로그를 JSON 한 줄로 낸다. 포맷 문자열에 따옴표를 끼워 흉내내면 메시지에 따옴표나 줄바꿈이
섞이는 순간 깨진 JSON 이 나오므로, 포매터에서 직렬화한다.

요청 단위 로그는 이후 change 의 전제다. 스트리밍 응답이나 LLM 호출을 디버깅하려면
"어느 요청이 얼마나 걸렸고 무엇을 반환했는가"가 먼저 있어야 한다.
"""

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

ACCESS_LOGGER = "app.access"

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """로그 레코드를 JSON 한 줄로 직렬화한다.

    `logger.info("...", extra={...})` 로 넘긴 항목은 최상위 키로 합쳐진다.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            # 스택 트레이스는 로그에만 남는다. 응답 본문에는 절대 싣지 않는다.
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # ASGI 서버는 자기 로거에 평문 핸들러를 붙이고 propagate 를 끈다. 그대로 두면 로그
    # 형식이 두 가지가 되어 구조화 로깅의 의미가 없어진다. 핸들러를 걷어내고 루트로 흘린다.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True

    # 액세스 로그는 RequestLoggingMiddleware 가 request_id·duration 과 함께 남긴다.
    # 서버 것까지 두면 같은 요청이 두 줄로 찍힌다.
    logging.getLogger("uvicorn.access").disabled = True


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """요청마다 식별자를 붙이고 처리 결과를 한 줄로 남긴다.

    예외가 나도 로그를 남긴 뒤 다시 던진다. 여기서 삼키면 오류 핸들러가 응답을 만들 수 없다.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        logger = logging.getLogger(ACCESS_LOGGER)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "요청 처리 실패",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "요청 처리 완료",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        response.headers["x-request-id"] = request_id
        return response
