"""환경변수 → 설정 객체.

환경을 읽는 곳이 여기뿐이다. 흩어지면 무엇을 읽는지 문서화할 수 없어 린트(`TID251`)로
막아 둔다.
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.adapters.retrievers import RETRIEVER_NAMES
from app.core.chunking import ChunkStrategy
from app.core.exceptions import ConfigurationError
from app.core.fusion import DEFAULT_RRF_K


class RetrieverSettings(BaseModel):
    """활성 retriever 하나의 구성 — 이름·가중치·후보 깊이·필수 여부."""

    name: str
    # 0 이나 음수는 그 목록이 순위에 아무것도 기여하지 않거나 순서를 뒤집는다는 뜻이다.
    weight: float = Field(default=1.0, gt=0)
    candidate_depth: int = Field(default=100, gt=0)
    required: bool = False

    @field_validator("name")
    @classmethod
    def _must_be_registered(cls, value: str) -> str:
        """등록되지 않은 이름은 기동을 막는다 — 런타임에 발견되면 오타가 조용히 도는 배포다."""
        if value not in RETRIEVER_NAMES:
            raise ValueError(
                f"알 수 없는 retriever 이름입니다 — {value!r} "
                f"(등록된 이름: {sorted(RETRIEVER_NAMES)})"
            )
        return value


def _default_retrievers() -> list[RetrieverSettings]:
    """밀집 필수 + 어휘 선택. 가중치를 같게 두는 근거는 `ARCHITECTURE.md` 에 있다."""
    return [
        RetrieverSettings(name="dense", required=True),
        RetrieverSettings(name="lexical", required=False),
    ]


class Settings(BaseSettings):
    """서비스 설정. 전 항목에 기본값이 있어 환경변수 없이도 기동된다."""

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    app_name: str = "document-qa-api"
    log_level: str = "INFO"

    cache_url: str = "redis://localhost:6379/0"
    vector_store_url: str = "http://localhost:8001"

    probe_timeout_seconds: float = Field(default=2.0, gt=0)
    health_total_timeout_seconds: float = Field(default=5.0, gt=0)

    registry_path: Path = Path("./data/registry.sqlite3")

    # 어휘 색인(FTS5)도 파일 하나다. 레지스트리와 나누는 이유는 검색용 테이블이 섞이면
    # 백업·삭제 절차가 두 관심사를 함께 다루게 되기 때문이다.
    lexical_index_path: Path = Path("./data/lexical.sqlite3")

    # 질의 토큰이 "드물다"고 인정받는 하한. 실측 근거는 `ARCHITECTURE.md` 「어휘 색인」에
    # 있다 — 0.4 로 올리면 샘플 문서에서 코드리뷰 질의가 빈 목록이 된다.
    lexical_min_token_rarity: float = Field(default=0.3, ge=0, le=1)

    # 샘플 문서가 한국어라 영어 전용 모델은 검색이 동작하지 않고, 입력 창이 좁은
    # 모델(128 토큰)은 청크 뒷부분이 조용히 잘린다.
    embedding_model: str = "intfloat/multilingual-e5-small"

    # 크기가 문자 기준인 이유는 토큰 기준이면 `core/` 가 임베딩 라이브러리를 알게 되어서다.
    chunk_strategy: ChunkStrategy = ChunkStrategy.RECURSIVE
    chunk_size: int = Field(default=600, gt=0)
    chunk_overlap: int = Field(default=100, gt=0)

    embedding_batch_size: int = Field(default=64, gt=0)
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)

    # 상한에 걸린 요청은 실패하지 않고 대기한다. 같은 문서의 직렬화는 별도 잠금이 맡는다.
    ingestion_concurrency: int = Field(default=2, gt=0)

    # ── 검색 ──────────────────────────────────────────────────────────
    # 요청이 덮어쓸 수 있게 연 이유는 K 조정에 재배포가 들지 않게 하려는 것이다.
    retrieval_top_k: int = Field(default=5, gt=0)
    # 그 열어 둔 문 때문에 상한이 필요하다 — 없으면 `top_k=1000000` 이 저장소를 다 긁는다.
    # 20 인 이유는 응답 크기가 아니라 융합 여지다 — 상한이 후보 깊이에 가까워질수록
    # 한 retriever 가 자리를 다 채워 다른 쪽 발견이 들어올 자리가 없어진다 (`candidate_depth`).
    retrieval_max_top_k: int = Field(default=20, gt=0)
    # 0.82 는 계측값이다 — 관련 1위 최솟값 0.8511 / 무관 1위 최댓값 0.8134 사이.
    # 어휘 쪽 하한은 `lexical_min_token_rarity` 라 이 값은 밀집 retriever 만 본다.
    retrieval_min_score: float = Field(default=0.82, ge=0, le=1)

    # 활성 retriever 구성. JSON 목록이라 조합·가중치·깊이·필수 여부가 재배포 없이 바뀐다.
    retrievers: list[RetrieverSettings] = Field(default_factory=_default_retrievers)
    # RRF 원 논문(Cormack et al., 2009)이 실험으로 고른 값이자 업계 관례다.
    retrieval_rrf_k: int = Field(default=DEFAULT_RRF_K, gt=0)
    # 막는 것은 절단이 아니라 임의 길이 입력이 토크나이저에 들어가는 것이다. 절단은
    # 임베더가 선언한 입력 창이 막는다 — 문자 수로 막으면 상한이 101자가 된다.
    retrieval_max_query_chars: int = Field(default=1000, gt=0)

    # ── 답변 생성 ──────────────────────────────────────────────────────
    # 한 시도의 상한. 실측 최악값(18.9초)이 이 값의 1/3 이라 정상 경로가 닿지 않는다.
    qa_llm_timeout_seconds: float = Field(default=60.0, gt=0)
    # 1 은 재시도가 아니고, 4 이상이면 최악 지연이 상한×4 + 백오프라 인내를 넘는다.
    qa_llm_max_attempts: int = Field(default=3, ge=1)
    # 대기가 1초·2초. 상한 대비 무시할 만하면서 즉시 재시도는 아닌 값이다.
    qa_llm_retry_backoff_seconds: float = Field(default=1.0, gt=0)
    # 흔한 프록시 유휴 상한(60초)의 1/4.
    qa_sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    # 생성 하나가 프로세스 하나다 — 없으면 메모리가 동시 요청 수에 비례한다. 세션 풀의
    # 상한이 같은 값이라 두 곳이 같은 수를 두 번 세지 않는다.
    qa_concurrency: int = Field(default=2, gt=0)
    # 비면 CLI 기본값을 쓴다. 다음 change 의 캐시 키에 들어갈 값이라 설정으로 노출해 둔다.
    qa_llm_model: str = ""
    # `turn/interrupt` 뒤 `turn/completed` 를 기다리는 유예. 넘기면 세션을 폐기한다.
    qa_llm_interrupt_grace_seconds: float = Field(default=2.0, gt=0)
    # 프로세스 기동 + 핸드셰이크 상한. 실측 8.5초의 세 배 이상이다.
    qa_llm_session_startup_timeout_seconds: float = Field(default=30.0, gt=0)

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
        """기본 K 가 상한보다 크면 어떤 요청도 통과하지 못한다 — 기동에서 막는다."""
        if self.retrieval_top_k > self.retrieval_max_top_k:
            raise ValueError(
                f"retrieval_top_k({self.retrieval_top_k}) 는 "
                f"retrieval_max_top_k({self.retrieval_max_top_k}) 이하여야 합니다"
            )
        return self

    @model_validator(mode="after")
    def _at_least_one_retriever_must_be_active(self) -> "Settings":
        """빈 목록을 허용하면 검색이 아무것도 못 찾는 배포가 오류 없이 존재하게 된다."""
        if not self.retrievers:
            raise ValueError("retrievers 는 하나 이상이어야 합니다")
        return self

    @model_validator(mode="after")
    def _candidate_depth_must_cover_the_k_ceiling(self) -> "Settings":
        """깊이를 K 상한과 비교한다 — 기본값으로만 보면 큰 `top_k` 요청 하나가 곧 넘어선다."""
        shallow = [
            item.name for item in self.retrievers if item.candidate_depth < self.retrieval_max_top_k
        ]
        if shallow:
            raise ValueError(
                f"retriever {shallow} 의 candidate_depth 가 "
                f"retrieval_max_top_k({self.retrieval_max_top_k}) 보다 작습니다"
            )
        return self


#: 생성기 프로세스에 넘기는 환경변수. 상속하지 않는 것이 결정이라 목록이 명시적이고,
#: 환경을 읽는 곳이 이 파일뿐이라는 규약(`TID251`) 때문에 목록도 여기 있다.
LLM_ENV_PASSTHROUGH = ("HOME", "PATH", "CODEX_HOME")


def llm_environment() -> dict[str, str]:
    """생성기에게 넘길 최소 환경.

    컨테이너 환경변수에 저장소 접속 정보가 있어 통째로 넘기지 않는다."""
    return {name: os.environ[name] for name in LLM_ENV_PASSTHROUGH if name in os.environ}


@lru_cache
def get_settings() -> Settings:
    """설정을 로딩한다. 무효한 값이면 기동을 멈춘다.

    pydantic 오류를 도메인 예외로 바꾼다 — 프레임워크 예외가 상위로 새지 않게."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigurationError(f"설정 값이 유효하지 않습니다 — {problems}") from exc
