"""테스트 하네스 — 앱 클라이언트, 격리된 데이터 디렉터리, 어댑터 대역.

임베더까지 대역인 이유는 실제 모델을 쓰면 `pytest` 한 줄이 수백 MB 다운로드에 묶여
"외부 서비스·구독 없이 실행된다"는 요구가 깨지기 때문이다 (`tests/README.md`).
"""

import os
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.protocols import HealthProbe
from app.adapters.vector_store.client import parse_url
from app.config import Settings
from app.main import create_app

from .stubs import (
    FakeEmbedder,
    StubDocumentRegistry,
    StubLexicalIndex,
    StubProbe,
    StubVectorStore,
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """격리된 임시 데이터 디렉터리."""
    d = tmp_path / "data"
    d.mkdir()
    return d


#: 테스트가 실물 Chroma 를 볼 때 쓰는 주소. 컨테이너 안에서는 `localhost:8001` 이 자기
#: 자신이라 환경변수를 따른다 — 고정하면 그 층이 컨테이너에서 영영 건너뛰어진다.
VECTOR_STORE_URL = os.environ.get("APP_VECTOR_STORE_URL") or "http://localhost:8001"


def vector_store_is_reachable(url: str = VECTOR_STORE_URL) -> bool:
    """TCP 연결만 해 본다. chromadb import 는 비싸고, 여기서 알고 싶은 것은 존재 여부다."""
    endpoint = parse_url(url)
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((endpoint.host, endpoint.port)) == 0


needs_vector_store = pytest.mark.skipif(
    not vector_store_is_reachable(),
    reason=(
        f"Chroma 서버({VECTOR_STORE_URL})가 떠 있지 않습니다 — "
        "`docker compose run --build --rm test` 로 돌리면 함께 뜹니다"
    ),
)


#: 계약 테스트 전용 데이터베이스 번호. 0 이 아니면 실행 환경의 캐시와 섞이지 않는다.
CACHE_TEST_DB = 15


def _isolated_db(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{CACHE_TEST_DB}"))


#: 실물 Redis 를 보는 테스트가 쓰는 주소. db 번호를 갈아 끼우는 것은 계약 테스트가 키를
#: 지우고 세는데 그것이 개발자가 띄워 둔 캐시면 상태가 한 번에 사라지기 때문이다.
CACHE_URL = _isolated_db(os.environ.get("APP_CACHE_URL") or "redis://localhost:6379/0")


def cache_is_reachable(url: str = CACHE_URL) -> bool:
    """TCP 연결만 해 본다. 여기서 알고 싶은 것은 존재 여부뿐이다."""
    parsed = urlparse(url)
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((parsed.hostname or "localhost", parsed.port or 6379)) == 0


#: Redis 계약 테스트의 공통 스킵. 마커가 기본 실행에서 이 층을 빼고, 이 조건은
#: `-m redis` 로 명시적으로 부른 실행이 저장소 없이 실패하지 않게 한다.
needs_cache = pytest.mark.skipif(
    not cache_is_reachable(),
    reason=(
        f"Redis({CACHE_URL})가 떠 있지 않습니다 — "
        "`docker compose run --build --rm test pytest -m redis` 로 돌리면 함께 뜹니다"
    ),
)


#: 실물 임베딩을 쓰는 테스트가 요구하는 모델. `Settings.embedding_model` 의 기본값과 같아야
#: 한다 — 다른 모델로 품질을 재면 그 숫자는 배포되는 구성의 것이 아니다.
EMBEDDING_MODEL = "intfloat/multilingual-e5-small"


def weights_are_cached(model_name: str = EMBEDDING_MODEL) -> bool:
    """네트워크를 건드리지 않고 캐시만 확인한다."""
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(model_name, local_files_only=True)
    except Exception:
        return False
    return True


#: 실물 가중치가 필요한 테스트의 공통 스킵. `needs_vector_store` 와 같은 자리에 두는
#: 이유도 같다 — 조건이 두 벌이 되면 한쪽만 고쳐진 채 다른 쪽이 조용히 안 돌 수 있다.
needs_weights = pytest.mark.skipif(
    not weights_are_cached(),
    reason=f"{EMBEDDING_MODEL} 가중치가 로컬에 없습니다 (컨테이너 이미지에는 구워져 있습니다)",
)


def make_settings(data_dir: Path) -> Settings:
    """`settings` 픽스처가 만드는 것과 같은 설정.

    환경을 조작한 뒤 같은 구성으로 다시 지어 보는 테스트가 있어 픽스처 밖에도 있어야 한다."""
    return Settings(
        cache_url="redis://unused:6379/0",
        vector_store_url=VECTOR_STORE_URL,
        registry_path=data_dir / "registry.sqlite3",
        probe_timeout_seconds=0.2,
        health_total_timeout_seconds=0.5,
    )


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """환경과 무관하게 결정론적인 설정. 상한은 짧게 잡는다.

    `vector_store_url` 만 환경을 따른다 — 컨테이너 안에서는 그 주소가 다르기 때문이다."""
    return make_settings(data_dir)


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
def lexical_index() -> StubLexicalIndex:
    """앱에 배선된 어휘 색인. 수집이 두 색인에 같은 청크를 넣었는지 여기서 확인한다."""
    return StubLexicalIndex()


@pytest.fixture
def registry() -> StubDocumentRegistry:
    return StubDocumentRegistry()


@pytest.fixture
def make_app(settings, healthy_probes, embedder, vector_store, lexical_index, registry):
    """앱 하나를 만든다. 지정하지 않은 것은 기본 대역이 채운다.

    같은 대역을 공유한 채 설정만 바꾼 앱을 만들 수 있어야 재기동 상황이 재현된다."""
    defaults = {
        "settings": settings,
        "probes": healthy_probes,
        "embedder": embedder,
        "vector_store": vector_store,
        "lexical_index": lexical_index,
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

    `ASGITransport` 는 lifespan 을 돌리지 않아, 기동 정리의 배선은 이것으로만 확인된다."""
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
