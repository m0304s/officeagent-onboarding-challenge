"""테스트 하네스.

세 가지를 제공한다.

1. 앱 테스트 클라이언트 — 실제 네트워크 없이 ASGI 앱에 직접 요청한다.
2. 격리된 임시 데이터 디렉터리 — 테스트 간 상태가 새지 않고 실행 순서에 의존하지 않게 한다.
3. 어댑터 대역 — 프로브·임베더·벡터 스토어·문서 레지스트리.

**임베더를 대역으로 두는 것은 선택이 아니다.** 실제 모델을 쓰면 `pytest` 한 줄이 수백 MB
다운로드에 묶여, "외부 서비스·구독 없이 실행된다"는 요구를 깬다. 실제 모델과의 계약은
`test_embedding.py` 가 가중치가 있을 때만 도는 테스트로 따로 확인한다.

저장소 대역은 속도 때문만이 아니라 **테스트가 저장 결과를 직접 들여다봐야** 하기
때문이다. spec 이 요구하는 것은 "응답의 `chunk_count` 만큼 청크가 실제로 저장되었는가"
같은 성질이라, API 응답만으로는 확인되지 않는다. 실물 어댑터와의 계약은
`test_vector_store.py`·`test_registry.py` 가 덮는다.
"""

import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.protocols import HealthProbe
from app.adapters.vector_store.client import parse_url
from app.config import Settings
from app.main import create_app

from .stubs import FakeEmbedder, StubDocumentRegistry, StubProbe, StubVectorStore


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """격리된 임시 데이터 디렉터리."""
    d = tmp_path / "data"
    d.mkdir()
    return d


#: 테스트가 실물 Chroma 를 볼 때 쓰는 주소. `docker compose up vector-store` 가 여는
#: 포트이자 `Settings` 의 기본값이다. 환경변수로 읽지 않는 이유는 픽스처가 환경에 따라
#: 다른 값을 주면 실패가 재현되지 않기 때문이다 — 서버가 그 자리에 없으면 **건너뛴다**.
VECTOR_STORE_URL = "http://localhost:8001"


def vector_store_is_reachable(url: str = VECTOR_STORE_URL) -> bool:
    """TCP 연결만 해 본다. chromadb import 는 비싸고, 여기서 알고 싶은 것은 존재 여부다."""
    endpoint = parse_url(url)
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((endpoint.host, endpoint.port)) == 0


needs_vector_store = pytest.mark.skipif(
    not vector_store_is_reachable(),
    reason=f"Chroma 서버({VECTOR_STORE_URL})가 떠 있지 않습니다 — `make vector-store` 로 띄웁니다",
)


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """환경과 무관하게 결정론적인 설정. 상한은 테스트가 오래 걸리지 않게 짧게 잡는다."""
    return Settings(
        cache_url="redis://unused:6379/0",
        vector_store_url=VECTOR_STORE_URL,
        registry_path=data_dir / "registry.sqlite3",
        probe_timeout_seconds=0.2,
        health_total_timeout_seconds=0.5,
    )


@pytest.fixture
def healthy_probes() -> tuple[HealthProbe, ...]:
    return (StubProbe("cache"), StubProbe("vector_store"))


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def vector_store() -> StubVectorStore:
    """앱에 배선된 벡터 스토어. 테스트가 저장 결과를 여기서 직접 확인한다."""
    return StubVectorStore()


@pytest.fixture
def registry() -> StubDocumentRegistry:
    return StubDocumentRegistry()


@pytest.fixture
def make_app(settings, healthy_probes, embedder, vector_store, registry):
    """앱 하나를 만든다. 지정하지 않은 것은 기본 대역이 채운다.

    **같은 대역을 공유한 채 설정만 바꾼 앱**을 만들 수 있다는 점이 중요하다. 색인 구성을
    바꾼 뒤의 재기동은 그렇게만 재현된다 — 저장소는 그대로인데 서명이 달라지는 상황.
    """
    defaults = {
        "settings": settings,
        "probes": healthy_probes,
        "embedder": embedder,
        "vector_store": vector_store,
        "registry": registry,
    }

    def _make(probes: Sequence[HealthProbe] | None = None, **overrides) -> FastAPI:
        wiring = {
            **defaults,
            **{key: value for key, value in overrides.items() if value is not None},
        }
        if probes is not None:
            wiring["probes"] = probes
        return create_app(**wiring)

    return _make


@pytest.fixture
def make_client(make_app):
    """테스트 클라이언트를 만든다. `make_app` 과 인자가 같다."""

    def _make(probes: Sequence[HealthProbe] | None = None, **overrides) -> AsyncClient:
        app = make_app(probes, **overrides)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return _make


@pytest.fixture
async def client(make_client):
    """전체 정상 상태의 기본 클라이언트."""
    async with make_client() as c:
        yield c


@asynccontextmanager
async def booted(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """기동 훅까지 태워 앱을 띄운다.

    `ASGITransport` 는 lifespan 을 돌리지 않는다. 기동 정리(`reconcile_storage`)가 앱에
    실제로 배선되어 있는지는 그 훅이 돌아야만 확인되므로, 재기동을 다루는 테스트는 이
    도우미로 앱을 띄운다.
    """
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
