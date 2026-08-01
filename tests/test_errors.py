"""오류 응답 형식.

네 경로(경로 없음 · 메서드 불허 · 요청 검증 실패 · 미처리 예외)가 **같은 봉투**를 쓰고,
어느 것도 내부 정보를 흘리지 않는지 확인한다. 형식이 두 가지가 되면 소비자가 파서를 둘
들고 있어야 하므로, 그 회귀를 여기서 막는다.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.exceptions import AppError, ErrorCode
from app.main import create_app

from .stubs import StubProbe

SECRET = "redis://user:hunter2@cache:6379"


class Payload(BaseModel):
    name: str
    count: int


class CustomDomainError(AppError):
    code = ErrorCode.CONFIGURATION_ERROR


@pytest.fixture
def app_with_error_routes(settings):
    """오류를 낼 수 있는 라우트를 붙인 앱.

    운영 코드에 테스트용 라우트를 두지 않기 위해 여기서만 추가한다.
    """
    app = create_app(settings=settings, probes=(StubProbe("cache"), StubProbe("vector_store")))

    @app.post("/echo")
    async def echo(payload: Payload) -> dict:  # 요청 검증 실패를 만들기 위한 라우트
        return {"ok": payload.name}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"failed to connect to {SECRET}")

    @app.get("/domain-error")
    async def domain_error() -> None:
        raise CustomDomainError("설정을 읽을 수 없습니다")

    return app


@pytest.fixture
async def client(app_with_error_routes):
    transport = ASGITransport(app=app_with_error_routes, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def envelope(payload: dict) -> dict:
    """봉투 구조를 강제로 확인하며 꺼낸다."""
    assert set(payload) == {"error"}, "최상위는 error 하나여야 한다"
    error = payload["error"]
    assert "code" in error and "message" in error
    assert isinstance(error["code"], str)
    assert isinstance(error["message"], str)
    return error


class TestEnvelopeIsUniform:
    async def test_unknown_path(self, client):
        response = await client.get("/nope")

        assert response.status_code == 404
        assert envelope(response.json())["code"] == ErrorCode.NOT_FOUND

    async def test_method_not_allowed(self, client):
        response = await client.delete("/health")

        assert response.status_code == 405
        assert envelope(response.json())["code"] == ErrorCode.METHOD_NOT_ALLOWED

    async def test_request_validation_failure(self, client):
        response = await client.post("/echo", json={"name": "a", "count": "not-a-number"})

        assert response.status_code == 422
        error = envelope(response.json())
        assert error["code"] == ErrorCode.VALIDATION_ERROR

    async def test_unhandled_internal_error(self, client):
        response = await client.get("/boom")

        assert response.status_code == 500
        assert envelope(response.json())["code"] == ErrorCode.INTERNAL_ERROR

    async def test_domain_error_uses_its_own_code(self, client):
        response = await client.get("/domain-error")

        assert response.status_code == 500
        error = envelope(response.json())
        assert error["code"] == ErrorCode.CONFIGURATION_ERROR
        assert "설정을 읽을 수 없습니다" in error["message"]


class TestValidationErrorIsActionable:
    async def test_identifies_which_input_is_wrong(self, client):
        response = await client.post("/echo", json={"name": "a", "count": "not-a-number"})

        fields = response.json()["error"]["fields"]
        assert any("count" in f["location"] for f in fields), (
            "어떤 입력이 문제인지 알 수 있어야 한다"
        )

    async def test_does_not_echo_the_submitted_value(self, client):
        """입력 값 자체는 되비치지 않는다 — 자격증명이 본문에 실려 오는 경우가 있다."""
        response = await client.post("/echo", json={"name": "a", "count": SECRET})

        assert "hunter2" not in response.text
        assert SECRET not in response.text


class TestNoInternalDetailsLeak:
    async def test_unhandled_error_hides_the_exception_message(self, client):
        response = await client.get("/boom")

        assert "hunter2" not in response.text
        assert SECRET not in response.text
        assert "Traceback" not in response.text
        assert "RuntimeError" not in response.text

    async def test_unhandled_error_is_logged_with_the_cause(self, client, caplog):
        """응답에서 지운 정보는 로그에는 남아야 진단이 가능하다."""
        with caplog.at_level("ERROR"):
            await client.get("/boom")

        assert any(
            "RuntimeError" in record.exc_text for record in caplog.records if record.exc_text
        )


class TestHealthIsNotConverted:
    async def test_health_unavailable_keeps_its_status_report_shape(self, settings):
        """503 이어도 오류 봉투로 바뀌지 않는다. 상태 보고와 오류 통지는 다른 일이다."""
        app = create_app(
            settings=settings,
            probes=(StubProbe("cache", raises=RuntimeError("down")), StubProbe("vector_store")),
        )
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert "error" not in body
        assert body["status"] == "unavailable"
        assert set(body["dependencies"]) == {"cache", "vector_store"}


class TestRequestLogging:
    async def test_response_carries_a_request_id(self, client):
        response = await client.get("/health")

        assert response.headers.get("x-request-id")

    async def test_incoming_request_id_is_preserved(self, client):
        response = await client.get("/health", headers={"x-request-id": "abc-123"})

        assert response.headers["x-request-id"] == "abc-123"

    async def test_access_log_records_method_path_and_status(self, client, caplog):
        with caplog.at_level("INFO", logger="app.access"):
            await client.get("/health")

        record = next(r for r in caplog.records if r.name == "app.access")
        assert record.method == "GET"
        assert record.path == "/health"
        assert record.status_code == 200
        assert record.duration_ms >= 0
