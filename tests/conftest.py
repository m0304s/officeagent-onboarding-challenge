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

import os
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


#: 테스트가 실물 Chroma 를 볼 때 쓰는 주소. 기본값은 `docker compose up -d vector-store` 가
#: 호스트에 여는 포트이자 `Settings` 의 기본값이다.
#:
#: **환경변수를 보는 이유**: 테스트가 `docker compose run --build --rm test` 로 컨테이너 안에서도
#: 돈다. 거기서는 `localhost:8001` 이 벡터 스토어가 아니라 자기 자신이라, 주소를 고정하면
#: 실물 Chroma 층이 컨테이너 안에서 영원히 건너뛰어진다 — 그 층을 실제로 돌리려고 만든
#: 실행 경로에서 정작 안 도는 셈이 된다. 값을 주지 않으면 예전과 똑같이 동작하고,
#: 서버가 그 자리에 없으면 **건너뛴다**.
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

    픽스처 밖에서도 같은 객체를 만들 수 있어야 한다 — "환경변수가 이 값을 뚫지 못한다"를
    확인하는 테스트가 환경을 조작한 뒤 같은 구성으로 다시 지어 봐야 하기 때문이다.
    """
    return Settings(
        cache_url="redis://unused:6379/0",
        vector_store_url=VECTOR_STORE_URL,
        registry_path=data_dir / "registry.sqlite3",
        probe_timeout_seconds=0.2,
        health_total_timeout_seconds=0.5,
    )


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """환경과 무관하게 결정론적인 설정. 상한은 테스트가 오래 걸리지 않게 짧게 잡는다.

    `vector_store_url` 만은 예외로 환경을 따른다 — 실물 Chroma 를 보는 테스트가 이 값으로
    서버에 붙는데, 컨테이너 안에서는 그 주소가 `localhost:8001` 이 아니기 때문이다.
    값을 고르는 것은 `VECTOR_STORE_URL` 이고, 여기서는 그것을 **명시적으로** 넘긴다.
    """
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

    **같은 대역을 공유한 채 설정만 바꾼 앱**을 만들 수 있다는 점이 중요하다. 색인 구성을
    바꾼 뒤의 재기동은 그렇게만 재현된다 — 저장소는 그대로인데 서명이 달라지는 상황.
    """
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

    `ASGITransport` 는 lifespan 을 돌리지 않는다. 기동 정리(`reconcile_storage`)가 앱에
    실제로 배선되어 있는지는 그 훅이 돌아야만 확인되므로, 재기동을 다루는 테스트는 이
    도우미로 앱을 띄운다.
    """
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
