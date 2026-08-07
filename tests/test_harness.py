"""하네스 자체 검증.

픽스처가 실제로 동작하는지 확인한다. 이게 깨지면 다른 테스트의 실패는 해석할 수 없다.
"""

from pathlib import Path

from app.adapters.protocols import DocumentRegistry, Embedder, VectorStore
from tests.conftest import VECTOR_STORE_URL, make_settings


def test_the_stub_adapters_satisfy_their_protocols(embedder, vector_store, registry):
    """대역이 프로토콜을 벗어나면 대역으로 통과한 테스트가 실물에서 깨진다.

    실물 쪽 단언이 서버 없이는 건너뛰어져, 기본 실행에서 `query` 계약을 보는 곳은 여기뿐이다."""
    assert isinstance(embedder, Embedder)
    assert isinstance(vector_store, VectorStore)
    assert isinstance(registry, DocumentRegistry)


async def test_app_client_reaches_the_app(client):
    response = await client.get("/health")
    assert response.status_code == 200


def test_data_dir_is_isolated_and_empty(data_dir: Path):
    assert data_dir.is_dir()
    assert list(data_dir.iterdir()) == []


def test_settings_fixture_does_not_read_the_environment(settings, data_dir):
    # 환경변수에 무엇이 있든 픽스처가 명시적으로 준 값이어야 한다.
    assert settings.cache_url == "redis://127.0.0.1:1/0"
    assert settings.probe_timeout_seconds == 0.2
    # 벡터 스토어 주소만은 하네스가 환경을 보고 고른다 — 컨테이너 안에서는 실물 서버가
    # `localhost:8001` 이 아니다. 그래도 픽스처는 그 고른 값을 그대로 넘겨야 한다.
    assert settings.vector_store_url == VECTOR_STORE_URL


def test_환경변수는_픽스처가_정한_값을_뚫지_못한다(data_dir: Path, monkeypatch):
    """`APP_*` 가 떠 있는 채로 테스트가 돌 수 있다.

    환경이 명시 인자를 이기면 구조 층이 환경에 따라 다른 값을 보고 실패가 재현되지 않는다."""
    monkeypatch.setenv("APP_CACHE_URL", "redis://환경에서-샜다:6379/0")
    monkeypatch.setenv("APP_PROBE_TIMEOUT_SECONDS", "99")

    built = make_settings(data_dir)

    assert built.cache_url == "redis://127.0.0.1:1/0"
    assert built.probe_timeout_seconds == 0.2
