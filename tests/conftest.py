"""테스트 하네스.

두 가지를 제공한다.

1. 앱 테스트 클라이언트 — 실제 네트워크 없이 ASGI 앱에 직접 요청한다.
2. 격리된 임시 데이터 디렉터리 — 테스트 간 상태가 새지 않고 실행 순서에 의존하지 않게 한다.

의존성 상태는 프로브 대역(`StubProbe`)으로 만든다. 실제 컨테이너를 죽여가며 상태를 만들면
느리고 불안정하며, 무엇보다 외부 서비스 없이 스위트가 돌아야 한다.
"""

from collections.abc import Sequence
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.protocols import HealthProbe
from app.config import Settings
from app.main import create_app

from .stubs import StubProbe


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """격리된 임시 데이터 디렉터리."""
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """환경과 무관하게 결정론적인 설정. 상한은 테스트가 오래 걸리지 않게 짧게 잡는다."""
    return Settings(
        cache_url="redis://unused:6379/0",
        vector_store_path=data_dir / "chroma",
        probe_timeout_seconds=0.2,
        health_total_timeout_seconds=0.5,
    )


@pytest.fixture
def make_client(settings: Settings):
    """프로브 구성을 받아 테스트 클라이언트를 만든다."""

    def _make(probes: Sequence[HealthProbe]) -> AsyncClient:
        app = create_app(settings=settings, probes=probes)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return _make


@pytest.fixture
def healthy_probes() -> tuple[HealthProbe, ...]:
    return (StubProbe("cache"), StubProbe("vector_store"))


@pytest.fixture
async def client(make_client, healthy_probes):
    """전체 정상 상태의 기본 클라이언트."""
    async with make_client(healthy_probes) as c:
        yield c
