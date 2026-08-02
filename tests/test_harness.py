"""하네스 자체 검증.

픽스처가 실제로 동작하는지 확인한다. 이게 깨지면 다른 테스트의 실패는 해석할 수 없다.
"""

from pathlib import Path


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
