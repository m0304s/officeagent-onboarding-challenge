"""설정 로딩.

두 가지를 고정한다. 아무것도 주지 않아도 기본값으로 기동되고, 무효한 값이면 조용히
기동되는 대신 기동에 실패한다.
"""

import os

import pytest

from app.config import Settings, get_settings
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
