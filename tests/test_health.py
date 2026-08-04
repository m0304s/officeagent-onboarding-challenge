"""헬스 엔드포인트 시나리오.

spec 의 시나리오와 1:1로 맞춘다. 상태 코드만 보는 테스트가 되지 않도록, 불능일 때
어느 의존성이 문제인지 본문에서 식별되는지를 함께 단언한다.
"""

import asyncio

from app.core.models import Status

from .stubs import StubProbe


def deps(payload: dict) -> dict:
    return payload["dependencies"]


class TestAllHealthy:
    async def test_returns_ok_with_every_dependency_reported(self, client):
        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert set(deps(body)) == {"cache", "vector_store"}
        assert all(d["status"] == "ok" for d in deps(body).values())


class TestSingleDependencyDown:
    async def test_cache_down_marks_only_the_cache(self, make_client):
        probes = (
            StubProbe("cache", Status.UNAVAILABLE, detail="연결 실패"),
            StubProbe("vector_store"),
        )
        async with make_client(probes) as client:
            response = await client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert deps(body)["cache"]["status"] == "unavailable"
        assert deps(body)["vector_store"]["status"] == "ok"

    async def test_vector_store_down_marks_only_the_vector_store(self, make_client):
        probes = (
            StubProbe("cache"),
            StubProbe("vector_store", Status.UNAVAILABLE, detail="연결 실패 (ConnectionError)"),
        )
        async with make_client(probes) as client:
            response = await client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert deps(body)["vector_store"]["status"] == "unavailable"
        assert deps(body)["cache"]["status"] == "ok"

    async def test_the_reason_reaches_the_response(self, make_client):
        """어느 의존성이 왜 불능인지가 응답에 남아야 한다 — 상태 코드만으로는 진단할 수 없다."""
        probes = (
            StubProbe("cache"),
            StubProbe("vector_store", Status.UNAVAILABLE, detail="연결 실패 (ConnectionError)"),
        )
        async with make_client(probes) as client:
            body = (await client.get("/health")).json()

        assert "연결 실패" in deps(body)["vector_store"]["detail"]


class TestBothDependenciesDown:
    async def test_endpoint_still_responds_and_marks_both(self, make_client):
        probes = (
            StubProbe("cache", Status.UNAVAILABLE, detail="연결 실패"),
            StubProbe("vector_store", Status.UNAVAILABLE, detail="연결 실패 (ConnectionError)"),
        )
        async with make_client(probes) as client:
            response = await client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert set(deps(body)) == {"cache", "vector_store"}
        assert all(d["status"] == "unavailable" for d in deps(body).values())


class TestUnresponsiveDependency:
    async def test_bounded_response_time_when_a_dependency_hangs(self, make_client):
        probes = (StubProbe("cache", delay=10.0), StubProbe("vector_store"))
        async with make_client(probes) as client:
            start = asyncio.get_running_loop().time()
            response = await client.get("/health")
            elapsed = asyncio.get_running_loop().time() - start

        assert response.status_code == 503
        # 픽스처의 전체 상한이 0.5초다. 무한정 매달리지 않는다.
        assert elapsed < 3.0
        assert deps(response.json())["cache"]["status"] == "unavailable"

    async def test_a_hanging_dependency_does_not_hide_the_healthy_one(self, make_client):
        probes = (StubProbe("cache", delay=10.0), StubProbe("vector_store"))
        async with make_client(probes) as client:
            body = (await client.get("/health")).json()

        assert deps(body)["cache"]["status"] == "unavailable"
        assert deps(body)["vector_store"]["status"] == "ok", (
            "무응답 하나가 나머지 결과를 가리면 안 된다"
        )


class TestProbeRaises:
    async def test_unexpected_exception_marks_only_that_dependency(self, make_client):
        probes = (
            StubProbe("cache", raises=RuntimeError("boom")),
            StubProbe("vector_store"),
        )
        async with make_client(probes) as client:
            response = await client.get("/health")

        assert response.status_code == 503, "프로브 하나의 버그가 500 을 내면 안 된다"
        body = response.json()
        assert deps(body)["cache"]["status"] == "unavailable"
        assert deps(body)["vector_store"]["status"] == "ok"

    async def test_exception_details_are_not_exposed(self, make_client):
        secret = "redis://user:hunter2@cache:6379"
        probes = (
            StubProbe("cache", raises=RuntimeError(f"failed to connect to {secret}")),
            StubProbe("vector_store"),
        )
        async with make_client(probes) as client:
            raw = (await client.get("/health")).text

        assert "hunter2" not in raw
        assert secret not in raw


class TestResponseShape:
    async def test_unavailable_body_is_not_the_common_error_envelope(self, make_client):
        """503 이어도 오류 봉투가 아니라 상태 보고 본문이다. 두 형식이 섞이는 회귀를 막는다."""
        probes = (StubProbe("cache", Status.UNAVAILABLE), StubProbe("vector_store"))
        async with make_client(probes) as client:
            body = (await client.get("/health")).json()

        assert "error" not in body
        assert body["status"] == "unavailable"
        assert "dependencies" in body

    async def test_dependency_key_set_is_identical_whether_healthy_or_not(self, make_client):
        healthy = (StubProbe("cache"), StubProbe("vector_store"))
        broken = (StubProbe("cache", Status.UNAVAILABLE), StubProbe("vector_store"))

        async with make_client(healthy) as client:
            ok_body = (await client.get("/health")).json()
        async with make_client(broken) as client:
            bad_body = (await client.get("/health")).json()

        assert set(deps(ok_body)) == set(deps(bad_body))
