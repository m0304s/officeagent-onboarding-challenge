"""설정 로딩.

두 가지를 고정한다. 아무것도 주지 않아도 기본값으로 기동되고, 무효한 값이면 조용히
기동되는 대신 기동에 실패한다.
"""

import os

import pytest

from app.config import Settings, get_settings
from app.core.chunking import ChunkStrategy
from app.core.exceptions import ConfigurationError


def test_boots_with_no_configuration_at_all(monkeypatch, tmp_path):
    """설정 항목을 하나도 주지 않아도 문서화된 기본값으로 기동된다."""
    for key in list(os.environ):
        if key.startswith("APP_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # .env 파일의 영향을 받지 않게

    get_settings.cache_clear()
    settings = get_settings()

    assert settings.app_name
    assert settings.cache_url
    assert settings.probe_timeout_seconds > 0
    assert settings.health_total_timeout_seconds > 0

    # 수집 설정도 기본값만으로 성립해야 한다.
    assert settings.chunk_strategy is ChunkStrategy.RECURSIVE
    assert settings.chunk_size > settings.chunk_overlap >= 0
    assert settings.embedding_batch_size > 0
    assert settings.max_upload_bytes > 0
    assert settings.ingestion_concurrency > 0
    assert settings.embedding_model
    get_settings.cache_clear()


def test_invalid_value_fails_startup_with_an_identifiable_field(monkeypatch, tmp_path):
    """무효한 값이면 기동이 실패하고, 어느 항목이 왜 무효한지 드러난다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_PROBE_TIMEOUT_SECONDS", "0")  # gt=0 위반

    get_settings.cache_clear()
    with pytest.raises(ConfigurationError) as exc_info:
        get_settings()
    get_settings.cache_clear()

    message = str(exc_info.value)
    assert "probe_timeout_seconds" in message, "어느 설정 항목이 문제인지 드러나야 한다"


def test_invalid_value_does_not_silently_fall_back_to_the_default(monkeypatch, tmp_path):
    """잘못된 설정으로 조용히 기동되면 안 된다 — 기본값으로 흘러가지 않는다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_PROBE_TIMEOUT_SECONDS", "not-a-number")

    get_settings.cache_clear()
    with pytest.raises(ConfigurationError):
        get_settings()
    get_settings.cache_clear()


def test_settings_can_be_constructed_directly_for_tests():
    """테스트가 환경과 무관하게 설정을 구성할 수 있어야 한다."""
    settings = Settings(probe_timeout_seconds=1.5)
    assert settings.probe_timeout_seconds == 1.5


@pytest.mark.parametrize(
    ("variable", "value", "expected_field"),
    [
        ("APP_CHUNK_SIZE", "-1", "chunk_size"),
        ("APP_CHUNK_OVERLAP", "0", "chunk_overlap"),
        ("APP_EMBEDDING_BATCH_SIZE", "0", "embedding_batch_size"),
        ("APP_MAX_UPLOAD_BYTES", "0", "max_upload_bytes"),
        ("APP_INGESTION_CONCURRENCY", "0", "ingestion_concurrency"),
    ],
)
def test_invalid_ingestion_values_fail_startup(
    monkeypatch, tmp_path, variable, value, expected_field
):
    """수집 설정도 무효값이면 조용히 기본값으로 흘러가지 않고 기동을 멈춘다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(variable, value)

    get_settings.cache_clear()
    with pytest.raises(ConfigurationError) as exc_info:
        get_settings()
    get_settings.cache_clear()

    assert expected_field in str(exc_info.value)


def test_overlap_not_smaller_than_chunk_size_fails_startup(monkeypatch, tmp_path):
    """겹침이 청크 크기 이상이면 분할이 전진하지 않는다 — 기동 단계에서 막는다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_CHUNK_SIZE", "200")
    monkeypatch.setenv("APP_CHUNK_OVERLAP", "200")

    get_settings.cache_clear()
    with pytest.raises(ConfigurationError) as exc_info:
        get_settings()
    get_settings.cache_clear()

    message = str(exc_info.value)
    assert "chunk_overlap" in message and "chunk_size" in message


def test_unimplemented_chunk_strategy_fails_startup(monkeypatch, tmp_path):
    """구현되지 않은 전략을 설정으로 고를 수 없어야 한다.

    고를 수 있게 두면 "설정에 있으니 지원한다"는 허위 기재가 된다. 오류 메시지에는
    실제로 받아들여지는 값 목록이 나와야 무엇을 넣어야 하는지 알 수 있다.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_CHUNK_STRATEGY", "parent-child")  # 아직 구현되지 않았다

    get_settings.cache_clear()
    with pytest.raises(ConfigurationError) as exc_info:
        get_settings()
    get_settings.cache_clear()

    message = str(exc_info.value)
    assert "chunk_strategy" in message
    for implemented in ChunkStrategy:
        assert implemented.value in message, "받아들여지는 값 목록이 드러나야 한다"
