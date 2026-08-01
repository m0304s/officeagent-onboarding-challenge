"""프로브 단위 테스트.

벡터 스토어 프로브의 핵심은 "저장 경로에 실제로 쓸 수 있는가"다. 클라이언트 객체가
만들어졌다는 사실만으로 정상 판정하면 볼륨이 안 붙은 채로 정상이 나오므로, 그 실패 모드를
직접 겨냥한다.

캐시 프로브는 실제 서버 없이 검증한다 — 스위트가 외부 서비스 없이 돌아야 한다.
"""

import asyncio
import os
import stat
import sys

import pytest

from app.adapters.cache.probe import CacheProbe
from app.adapters.vector_store.probe import VectorStoreProbe
from app.core.models import Status


class TestVectorStoreProbe:
    async def test_reports_ok_when_the_path_is_writable(self, data_dir):
        probe = VectorStoreProbe(path=data_dir / "chroma", timeout_seconds=10.0)

        result = await probe.check()

        assert result.status is Status.OK
        assert result.name == "vector_store"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root 는 권한 검사를 우회한다")
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 권한 모델 전제")
    async def test_reports_unavailable_when_the_path_is_not_writable(self, data_dir):
        """저장 경로에 쓸 수 없으면 비정상. 이 프로브가 막으려는 실패 모드의 본체다."""
        locked = data_dir / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)  # 읽기·탐색만, 쓰기 불가
        try:
            probe = VectorStoreProbe(path=locked / "chroma", timeout_seconds=10.0)

            result = await probe.check()

            assert result.status is Status.UNAVAILABLE
            assert "쓸 수 없음" in (result.detail or "")
        finally:
            locked.chmod(stat.S_IRWXU)

    async def test_reports_unavailable_when_the_check_exceeds_its_timeout(
        self, data_dir, monkeypatch
    ):
        probe = VectorStoreProbe(path=data_dir / "chroma", timeout_seconds=0.01)
        monkeypatch.setattr(probe, "_probe", lambda: __import__("time").sleep(1))

        result = await probe.check()

        assert result.status is Status.UNAVAILABLE
        assert "시간 초과" in (result.detail or "")

    async def test_reports_unavailable_when_the_check_raises_unexpectedly(
        self, data_dir, monkeypatch
    ):
        probe = VectorStoreProbe(path=data_dir / "chroma", timeout_seconds=1.0)

        def boom() -> None:
            raise RuntimeError("자격증명 secret-token-1234 로 접속 실패")

        monkeypatch.setattr(probe, "_probe", boom)

        result = await probe.check()

        assert result.status is Status.UNAVAILABLE
        assert "secret-token-1234" not in (result.detail or ""), "내부 정보가 새면 안 된다"


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
