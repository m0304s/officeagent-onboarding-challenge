"""환경변수 → 설정 객체.

이 모듈이 환경을 읽는 **유일한** 곳이다. 다른 모듈이 `os.environ`을 직접 조회하면
어디서 무엇을 읽는지 흩어져 문서화가 불가능해지므로, 린트 규칙(`TID251`)으로 막아둔다.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.chunking import ChunkStrategy
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

    # 벡터 스토어 (별도 서비스 — Chroma 서버). 기본 포트가 8001 인 이유는 Chroma 의
    # 기본값(8000)이 이 API 의 포트와 겹치기 때문이다. compose 가 호스트 8001 을 컨테이너
    # 8000 에 연결하므로, 도커 안에서 돌리든 밖에서 돌리든 같은 주소가 쓰인다.
    vector_store_url: str = "http://localhost:8001"

    # 헬스 점검 상한. 의존성별 상한과 전체 상한을 따로 둔다 — 하나가 매달려도
    # 나머지 결과는 나와야 어느 의존성이 문제인지 식별할 수 있다.
    probe_timeout_seconds: float = Field(default=2.0, gt=0)
    health_total_timeout_seconds: float = Field(default=5.0, gt=0)

    # ── 문서 수집 ──────────────────────────────────────────────────────
    # 문서 레지스트리 ("지금 유효한 리비전이 무엇인가"의 단일 답). 표준 라이브러리
    # SQLite 파일이라 컨테이너가 늘지 않는다. 벡터 스토어와 같은 볼륨에 둔다.
    registry_path: Path = Path("./data/registry.sqlite3")

    # 다국어 모델을 쓴다. 샘플 문서가 한국어라 영어 전용 모델은 검색이 동작하지 않고,
    # 입력 창이 좁은 모델(128 토큰)은 청크 뒷부분이 조용히 잘린다.
    embedding_model: str = "intfloat/multilingual-e5-small"

    # 청킹. 크기는 문자 기준이다 — 토큰 기준으로 하면 core/ 가 토크나이저를,
    # 즉 임베딩 라이브러리를 알아야 해서 계층 규칙을 어기게 된다.
    chunk_strategy: ChunkStrategy = ChunkStrategy.RECURSIVE
    chunk_size: int = Field(default=600, gt=0)
    # 겹침은 0 을 허용하지 않는다. 인접 청크가 겹치지 않으면 문단 경계에 걸친 문장이
    # 양쪽 청크 어디에서도 온전히 읽히지 않는다 — 스펙이 요구하는 성질이 설정 하나로
    # 깨지는 자리라, 설정 단계에서 막는다.
    chunk_overlap: int = Field(default=100, gt=0)

    # 임베딩·저장을 이 크기로 끊어 배치 사이에 이벤트 루프에 양보한다. 메모리가
    # 문서 크기가 아니라 배치 크기에 비례하게 만드는 값이다.
    embedding_batch_size: int = Field(default=64, gt=0)

    # 업로드 크기 상한. 본문을 읽으면서 누적 바이트로 강제한다.
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)

    # 동시 수집 상한. 상한에 걸린 요청은 실패하지 않고 대기한다.
    # 같은 문서의 직렬화는 이것이 아니라 document_id 단위 잠금이 담당한다.
    ingestion_concurrency: int = Field(default=2, gt=0)

    # ── 검색 ──────────────────────────────────────────────────────────
    # 상위 K. 기본값 5 는 청크 겹침 설계(관련 절 1~2 + 인접 문맥 + 여유)에서 나온 값이고,
    # 요청이 `top_k` 로 덮어쓸 수 있다 — 답변 품질을 보며 K 를 조정할 때 재배포가
    # 필요 없게 열어 둔다.
    retrieval_top_k: int = Field(default=5, gt=0)
    # 그 열어 둔 문 때문에 상한이 필요하다. 상한이 없으면 `top_k=1000000` 한 줄이
    # 저장소에서 전부를 긁어 와 직렬화된다. 50 은 기본값의 열 배이자 최악 응답 크기
    # 600자 × 50 ≈ 30 KB 다. 상한도 설정인 이유는 K 실험에 재배포가 들지 않게 하기 위함.
    retrieval_max_top_k: int = Field(default=50, gt=0)
    # 유사도 하한. 어댑터가 거리를 `[0, 1]` 유사도로 바꿔 내보내므로 정의역이 그 범위다.
    # **0.82 는 계측값이다** — `sample-docs` 로 관련 질의 4개·무관 질의 3개의 점수 분포를
    # 실제로 재서 정했다(관련 1위 최솟값 0.8511 / 무관 1위 최댓값 0.8134). 초안이던 0.75 는
    # 아무것도 거르지 못한다: e5 정규화 코사인은 무관한 한국어 문장 사이에서도 0.75~0.81 이
    # 흔하다. 표본이 문서 2개·질의 7개뿐이라는 사실까지 ARCHITECTURE.md 에 함께 적었다.
    retrieval_min_score: float = Field(default=0.82, ge=0, le=1)
    # 질의 문자 수 상한. 이 상한이 막는 것은 조용한 절단이 아니라 **임의 길이 입력이
    # 토크나이저에 들어가는 것**이다. 절단은 임베더가 선언한 입력 창(설정 아님)으로 막는다 —
    # 문자 수만으로 절단을 막으려면 상한이 101자가 되어 정상 질문까지 거부된다.
    retrieval_max_query_chars: int = Field(default=1000, gt=0)

    @model_validator(mode="after")
    def _overlap_must_be_smaller_than_chunk(self) -> "Settings":
        """겹침이 청크 크기 이상이면 분할이 진행되지 않는다 — 무한 루프가 된다."""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"chunk_overlap({self.chunk_overlap}) 은 "
                f"chunk_size({self.chunk_size}) 보다 작아야 합니다"
            )
        return self

    @model_validator(mode="after")
    def _default_top_k_must_fit_under_its_ceiling(self) -> "Settings":
        """기본 K 가 상한보다 크면 어떤 요청도 통과할 수 없다 — 조용히 기동하면 안 된다.

        설정 둘이 서로 모순될 수 있는 자리라, 겹침·청크 크기와 같은 방식으로 막는다.
        """
        if self.retrieval_top_k > self.retrieval_max_top_k:
            raise ValueError(
                f"retrieval_top_k({self.retrieval_top_k}) 는 "
                f"retrieval_max_top_k({self.retrieval_max_top_k}) 이하여야 합니다"
            )
        return self


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
