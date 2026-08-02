"""프로브 단위 테스트.

두 프로브 모두 "도달 가능"이 **네트워크 도달**이다 — 캐시도 벡터 스토어도 별도 컨테이너다.
핵심은 클라이언트 객체가 만들어졌다는 사실로 정상 판정하지 않는다는 것이다. 그렇게 하면
서버가 없는 채로 "정상"이 나오고 첫 요청에서 터진다.

**실제 서버 없이 검증한다.** 정상 경로는 대역으로, 불능은 라우팅되지 않는 주소로 만든다 —
스위트가 외부 서비스 없이 돌아야 한다.
"""

import asyncio

import pytest

from app.adapters.cache.probe import CacheProbe
from app.adapters.vector_store import ChromaVectorStore, VectorStoreProbe
from app.core.exceptions import ConfigurationError
from app.core.models import Status

UNREACHABLE = "http://127.0.0.1:1"


class FakeChromaClient:
    """heartbeat 만 있는 클라이언트 대역."""

    def __init__(self, *, raises: Exception | None = None, delay: float = 0.0) -> None:
        self._raises = raises
        self._delay = delay
        self.heartbeats = 0

    def heartbeat(self) -> int:
        self.heartbeats += 1
        if self._delay:
            import time

            time.sleep(self._delay)  # 블로킹. 프로브가 오프로드하지 않으면 루프가 멈춘다
        if self._raises is not None:
            raise self._raises
        return 1


class TestVectorStoreProbe:
    async def test_reports_ok_when_the_heartbeat_succeeds(self, monkeypatch):
        client = FakeChromaClient()
        monkeypatch.setattr(
            "app.adapters.vector_store.probe.create_client", lambda endpoint: client
        )
        probe = VectorStoreProbe(url="http://irrelevant:8001", timeout_seconds=1.0)

        result = await probe.check()

        assert result.status is Status.OK
        assert result.name == "vector_store"
        assert client.heartbeats == 1, "실제로 물어보지 않고 정상이라고 답했다"

    async def test_reports_unavailable_when_the_server_is_unreachable(self):
        """이 프로브가 막으려는 실패 모드의 본체 — 컨테이너가 안 떠 있는 상태."""
        probe = VectorStoreProbe(url=UNREACHABLE, timeout_seconds=0.5)

        result = await probe.check()

        assert result.status is Status.UNAVAILABLE
        assert "연결 실패" in (result.detail or "")

    async def test_reports_unavailable_when_the_check_exceeds_its_timeout(self, monkeypatch):
        monkeypatch.setattr(
            "app.adapters.vector_store.probe.create_client",
            lambda endpoint: FakeChromaClient(delay=1.0),
        )
        probe = VectorStoreProbe(url="http://irrelevant:8001", timeout_seconds=0.01)

        result = await probe.check()

        assert result.status is Status.UNAVAILABLE
        assert "시간 초과" in (result.detail or "")

    async def test_failure_detail_does_not_leak_internals(self, monkeypatch):
        monkeypatch.setattr(
            "app.adapters.vector_store.probe.create_client",
            lambda endpoint: FakeChromaClient(
                raises=RuntimeError("자격증명 secret-token-1234 로 접속 실패")
            ),
        )
        probe = VectorStoreProbe(url="http://irrelevant:8001", timeout_seconds=1.0)

        result = await probe.check()

        assert result.status is Status.UNAVAILABLE
        assert "secret-token-1234" not in (result.detail or ""), "내부 정보가 새면 안 된다"


class TestVectorStoreAddress:
    """주소 해석은 프로브와 어댑터가 같은 함수로 한다 — 둘이 다른 서버를 보면 안 된다."""

    @pytest.mark.parametrize("url", ["localhost:8001", "", "ftp://localhost:8001", "http://"])
    def test_a_malformed_address_stops_the_boot(self, url):
        """오타 난 주소가 기동을 통과하면 첫 업로드에서야 드러난다."""
        with pytest.raises(ConfigurationError):
            VectorStoreProbe(url=url, timeout_seconds=1.0)
        with pytest.raises(ConfigurationError):
            ChromaVectorStore(url)

    def test_the_default_port_comes_from_the_scheme(self):
        from app.adapters.vector_store.client import parse_url

        assert parse_url("http://vector-store") == parse_url("http://vector-store:80")
        assert parse_url("https://example.com").port == 443
        assert parse_url("https://example.com").ssl is True


class TestCacheProbe:
    async def test_reports_unavailable_when_the_server_is_unreachable(self):
        # 라우팅되지 않는 주소로 접속 실패를 만든다. 외부 서비스에 의존하지 않는다.
        probe = CacheProbe(url="redis://127.0.0.1:1/0", timeout_seconds=0.3)

        result = await probe.check()

        assert result.status is Status.UNAVAILABLE
        assert result.name == "cache"

    async def test_failure_detail_does_not_leak_the_connection_string(self):
        probe = CacheProbe(url="redis://user:hunter2@127.0.0.1:1/0", timeout_seconds=0.3)

        result = await probe.check()

        assert "hunter2" not in (result.detail or "")
        assert "127.0.0.1" not in (result.detail or "")

    async def test_reports_ok_when_ping_succeeds(self, monkeypatch):
        """실제 서버 없이 정상 경로를 확인한다."""

        class FakeClient:
            async def ping(self) -> bool:
                return True

            async def aclose(self) -> None:
                return None

        monkeypatch.setattr("app.adapters.cache.probe.redis.from_url", lambda *a, **k: FakeClient())
        probe = CacheProbe(url="redis://irrelevant:6379/0", timeout_seconds=1.0)

        result = await probe.check()

        assert result.status is Status.OK

    async def test_reports_unavailable_when_ping_hangs(self, monkeypatch):
        class HangingClient:
            async def ping(self) -> bool:
                await asyncio.sleep(10)
                return True

            async def aclose(self) -> None:
                return None

        monkeypatch.setattr(
            "app.adapters.cache.probe.redis.from_url", lambda *a, **k: HangingClient()
        )
        probe = CacheProbe(url="redis://irrelevant:6379/0", timeout_seconds=0.05)

        result = await probe.check()

        assert result.status is Status.UNAVAILABLE
        assert "시간 초과" in (result.detail or "")
