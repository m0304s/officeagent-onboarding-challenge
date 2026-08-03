"""하네스 자체 검증.

픽스처가 실제로 동작하는지 확인한다. 이게 깨지면 다른 테스트의 실패는 해석할 수 없다.
"""

from pathlib import Path

from app.adapters.protocols import DocumentRegistry, Embedder, VectorStore


def test_the_stub_adapters_satisfy_their_protocols(embedder, vector_store, registry):
    """대역이 프로토콜을 벗어나면 대역으로 통과한 테스트가 실물에서 깨진다.

    임베더 쪽은 `test_embedding.py` 가 페이크와 실물을 같은 단언에 나란히 세워 이미
    확인한다(질의 경로 — `embed_query`·`count_query_tokens` — 를 포함해서다). 저장소는
    사정이 다르다: 실물 쪽 같은 단언(`test_vector_store.py`)이 Chroma 서버가 없으면
    파일째 건너뛰므로, **기본 실행에서 `query` 계약을 확인하는 자리가 여기뿐**이다.
    """
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
    # 환경변수에 무엇이 있든 픽스처가 준 값이어야 한다.
    assert settings.vector_store_url == "http://localhost:8001"
    assert settings.probe_timeout_seconds == 0.2
