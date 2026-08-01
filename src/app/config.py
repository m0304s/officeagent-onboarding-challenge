"""환경변수 → 설정 객체.

이 모듈이 환경을 읽는 **유일한** 곳이다. 다른 모듈이 `os.environ`을 직접 조회하면
어디서 무엇을 읽는지 흩어져 문서화가 불가능해지므로, 린트 규칙(`TID251`)으로 막아둔다.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.exceptions import ConfigurationError


class Settings(BaseSettings):
    """서비스 설정.

    모든 항목에 기본값이 있다. 환경변수를 하나도 주지 않아도 로컬에서 기동된다.
    """

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    app_name: str = "document-qa-api"
    log_level: str = "INFO"

    # 캐시 저장소 (별도 컨테이너)
    cache_url: str = "redis://localhost:6379/0"

    # 벡터 스토어 (임베디드 퍼시스턴트 — 별도 서비스가 아니라 마운트된 경로)
    vector_store_path: Path = Path("./data/chroma")

    # 헬스 점검 상한. 의존성별 상한과 전체 상한을 따로 둔다 — 하나가 매달려도
    # 나머지 결과는 나와야 어느 의존성이 문제인지 식별할 수 있다.
    probe_timeout_seconds: float = Field(default=2.0, gt=0)
    health_total_timeout_seconds: float = Field(default=5.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    """설정을 로딩한다. 무효한 값이면 기동을 멈춘다.

    어느 항목이 왜 무효한지 드러나야 한다. pydantic 의 검증 오류를 그대로 흘리면
    프레임워크 예외가 상위로 새므로, 도메인 예외로 바꿔 던진다.
    """
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigurationError(f"설정 값이 유효하지 않습니다 — {problems}") from exc
