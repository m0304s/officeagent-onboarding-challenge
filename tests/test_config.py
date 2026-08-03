"""설정 로딩.

두 가지를 고정한다. 아무것도 주지 않아도 기본값으로 기동되고, 무효한 값이면 조용히
기동되는 대신 기동에 실패한다.
"""

import os

import pytest

from app.config import Settings, get_settings, llm_environment
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

    # 검색 설정도 마찬가지다 — 새 항목이 기동 조건을 늘리지 않아야 한다.
    assert 0 < settings.retrieval_top_k <= settings.retrieval_max_top_k
    assert 0 <= settings.retrieval_min_score <= 1
    assert settings.retrieval_max_query_chars > 0

    # 답변 생성 설정도 마찬가지다. `qa_llm_model` 만 빈 문자열이 기본값인데, 그건 "CLI
    # 기본 모델을 쓴다"는 뜻이라 값이 아니라 부재가 의미다.
    assert settings.qa_llm_timeout_seconds > 0
    assert settings.qa_llm_max_attempts >= 1
    assert settings.qa_llm_retry_backoff_seconds > 0
    assert settings.qa_sse_heartbeat_seconds > 0
    assert settings.qa_concurrency > 0
    assert settings.qa_llm_interrupt_grace_seconds > 0
    assert settings.qa_llm_session_startup_timeout_seconds > 0
    assert settings.qa_llm_model == ""
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


@pytest.mark.parametrize(
    ("variable", "value", "expected_field"),
    [
        ("APP_RETRIEVAL_TOP_K", "0", "retrieval_top_k"),
        ("APP_RETRIEVAL_MAX_TOP_K", "0", "retrieval_max_top_k"),
        # 하한은 어댑터가 내보내는 유사도의 정의역 `[0, 1]` 밖으로 나갈 수 없다.
        # 음수면 아무것도 거르지 못하고, 1 초과면 무엇도 통과하지 못한다.
        ("APP_RETRIEVAL_MIN_SCORE", "-0.1", "retrieval_min_score"),
        ("APP_RETRIEVAL_MIN_SCORE", "1.5", "retrieval_min_score"),
        ("APP_RETRIEVAL_MAX_QUERY_CHARS", "0", "retrieval_max_query_chars"),
    ],
)
def test_invalid_retrieval_values_fail_startup(
    monkeypatch, tmp_path, variable, value, expected_field
):
    """검색 설정도 무효값이면 조용히 기본값으로 흘러가지 않고 기동을 멈춘다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(variable, value)

    get_settings.cache_clear()
    with pytest.raises(ConfigurationError) as exc_info:
        get_settings()
    get_settings.cache_clear()

    assert expected_field in str(exc_info.value)


def test_default_top_k_above_the_ceiling_fails_startup(monkeypatch, tmp_path):
    """기본 K 가 상한보다 크면 어떤 요청도 통과할 수 없다 — 기동 단계에서 막는다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_RETRIEVAL_TOP_K", "10")
    monkeypatch.setenv("APP_RETRIEVAL_MAX_TOP_K", "5")

    get_settings.cache_clear()
    with pytest.raises(ConfigurationError) as exc_info:
        get_settings()
    get_settings.cache_clear()

    message = str(exc_info.value)
    assert "retrieval_top_k" in message and "retrieval_max_top_k" in message


# ── 답변 생성 설정 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("variable", "value", "field", "expected"),
    [
        ("APP_QA_LLM_TIMEOUT_SECONDS", "12.5", "qa_llm_timeout_seconds", 12.5),
        ("APP_QA_LLM_MAX_ATTEMPTS", "1", "qa_llm_max_attempts", 1),
        ("APP_QA_LLM_RETRY_BACKOFF_SECONDS", "0.25", "qa_llm_retry_backoff_seconds", 0.25),
        ("APP_QA_SSE_HEARTBEAT_SECONDS", "5", "qa_sse_heartbeat_seconds", 5.0),
        ("APP_QA_CONCURRENCY", "4", "qa_concurrency", 4),
        ("APP_QA_LLM_MODEL", "gpt-5-codex", "qa_llm_model", "gpt-5-codex"),
        ("APP_QA_LLM_INTERRUPT_GRACE_SECONDS", "0.5", "qa_llm_interrupt_grace_seconds", 0.5),
        (
            "APP_QA_LLM_SESSION_STARTUP_TIMEOUT_SECONDS",
            "45",
            "qa_llm_session_startup_timeout_seconds",
            45.0,
        ),
    ],
)
def test_answer_generation_settings_can_be_overridden_by_environment(
    monkeypatch, tmp_path, variable, value, field, expected
):
    """상한을 조정하는 데 재배포가 들지 않아야 한다 — 전부 환경변수로 덮인다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(variable, value)

    get_settings.cache_clear()
    settings = get_settings()
    get_settings.cache_clear()

    assert getattr(settings, field) == expected


@pytest.mark.parametrize(
    ("variable", "value", "expected_field"),
    [
        ("APP_QA_LLM_TIMEOUT_SECONDS", "0", "qa_llm_timeout_seconds"),
        # 0 이면 어떤 시도도 하지 않아 생성기가 영영 불리지 않는다.
        ("APP_QA_LLM_MAX_ATTEMPTS", "0", "qa_llm_max_attempts"),
        ("APP_QA_LLM_RETRY_BACKOFF_SECONDS", "0", "qa_llm_retry_backoff_seconds"),
        ("APP_QA_SSE_HEARTBEAT_SECONDS", "0", "qa_sse_heartbeat_seconds"),
        ("APP_QA_CONCURRENCY", "0", "qa_concurrency"),
        ("APP_QA_LLM_INTERRUPT_GRACE_SECONDS", "0", "qa_llm_interrupt_grace_seconds"),
        (
            "APP_QA_LLM_SESSION_STARTUP_TIMEOUT_SECONDS",
            "0",
            "qa_llm_session_startup_timeout_seconds",
        ),
    ],
)
def test_invalid_answer_generation_values_fail_startup(
    monkeypatch, tmp_path, variable, value, expected_field
):
    """생성 설정도 무효값이면 조용히 기본값으로 흘러가지 않고 기동을 멈춘다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(variable, value)

    get_settings.cache_clear()
    with pytest.raises(ConfigurationError) as exc_info:
        get_settings()
    get_settings.cache_clear()

    assert expected_field in str(exc_info.value)


def test_the_generator_environment_carries_only_the_three_names(monkeypatch):
    """생성기에게 넘길 환경은 상속이 아니라 목록이다.

    컨테이너 환경변수에 저장소 접속 정보가 들어 있어, 도구를 쓸 수 있는 에이전트에게
    통째로 넘길 이유가 없다. 어댑터가 아니라 여기서 정해진다.
    """
    monkeypatch.setenv("APP_CACHE_URL", "redis://secret-host:6379/0")
    monkeypatch.setenv("HOME", "/home/app")
    monkeypatch.setenv("CODEX_HOME", "/home/app/.codex")

    environment = llm_environment()

    assert set(environment) <= {"HOME", "PATH", "CODEX_HOME"}
    assert environment["HOME"] == "/home/app"
    assert "APP_CACHE_URL" not in environment


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
