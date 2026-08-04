"""환경변수 → 설정 객체.

환경을 읽는 곳이 여기뿐이다. 흩어지면 무엇을 읽는지 문서화할 수 없어 린트(`TID251`)로
막아 둔다.
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.adapters.reranking import KNOWN_RERANKER_PROFILES
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
    # 없으면 `top_k=1000000` 이 저장소를 다 긁는다. 20 인 이유는 융합 여지다 — 상한이
    # 후보 깊이에 가까워질수록 한 retriever 가 자리를 다 채운다 (`candidate_depth`).
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

    # ── 리랭킹 ────────────────────────────────────────────────────────
    # 끄면 리랭커를 아예 배선하지 않는다 — 되돌리기가 설정 한 줄이어야 한다.
    reranker_enabled: bool = True
    # 학습 언어에 한국어가 있고 원격 코드가 없는 모델. 후보 비교는 `ARCHITECTURE.md` 에 있다.
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # 후보 깊이(100)와 근거가 다르다 — 리랭크 깊이는 비용이 후보 수에 선형이라 곧 지연이다.
    # K 상한 20 을 덮으면서 그 위로 여유를 조금 둔 값이다.
    rerank_candidates: int = Field(default=30, gt=0)
    # 실패보다 느린 성공이 흔한 자리다 — 가중치를 처음 올리는 요청이 여기 걸린다.
    # 후보 30개 실측(1.14초, 웜)의 세 배 이상이라는 규칙은 `qa_llm_timeout_seconds` 와 같다.
    reranker_timeout_seconds: float = Field(default=5.0, gt=0)

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

    # ── 응답 캐시 ──────────────────────────────────────────────────────
    # 끄면 `NullResponseCache` 라 모든 요청이 미스다. 인메모리로 잇지 않는 것이 결정이다.
    cache_enabled: bool = True
    # 정확성은 무효화와 재검증이 맡으므로 TTL 은 방치된 항목의 상한 역할만 한다. 하루면
    # 프롬프트나 색인을 바꾼 뒤 키가 갈려 닿을 수 없게 된 항목도 그 안에 사라진다.
    cache_ttl_seconds: int = Field(default=86_400, gt=0)
    # 항목 하나가 근거 청크 본문까지 담아 3~5KB 라 500건이 2.5MB 규모다. 넘으면 저장을
    # 거부하는 대신 오래된 것부터 밀어낸다.
    cache_max_entries: int = Field(default=500, gt=0)
    # 계측값이다 — 서로 다른 질문 최대 0.8326 위로 여유를 둔 값. 부정문 쌍(0.9066~0.9979)은
    # 이 값으로 걸러지지 않는다: 내리기 전에 `ARCHITECTURE.md` 「임계값 0.93」을 읽을 것.
    cache_semantic_threshold: float = Field(default=0.93, ge=0, le=1)
    # 완전 탐색의 비용을 고정하는 값이다 — 없으면 캐시가 커질수록 히트가 미스보다 느려진다.
    cache_semantic_candidates: int = Field(default=200, gt=0)

    # 캐시는 최적화라 200ms 안에 답하지 못하면 도움이 되지 않는다. `probe_timeout_seconds`
    # 와 값이 다른 것은 프로브가 "살아 있나"를, 조회가 "지금 도움이 되나"를 묻기 때문이다.
    cache_operation_timeout_seconds: float = Field(default=0.2, gt=0)
    # 타임아웃만 두면 죽은 저장소에 매 요청 400ms 를 계속 잃는다. 연속 실패가 이만큼
    # 쌓이면 쿨다운 동안 저장소를 아예 부르지 않는다.
    cache_circuit_breaker_failures: int = Field(default=3, ge=1)
    # 쿨다운 뒤 첫 요청이 탐침이라 회복에 재기동이 들지 않는다.
    cache_circuit_breaker_cooldown_seconds: float = Field(default=30.0, gt=0)

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

    @model_validator(mode="after")
    def _rerank_depth_must_cover_the_k_ceiling(self) -> "Settings":
        """깊이가 K 상한보다 작으면 응답 뒷자리가 리랭킹되지 않은 채로 채워진다.

        검사를 활성 여부 아래 두는 이유는 끄는 것이 롤백 수단이기 때문이다."""
        if self.reranker_enabled and self.rerank_candidates < self.retrieval_max_top_k:
            raise ValueError(
                f"rerank_candidates({self.rerank_candidates}) 가 "
                f"retrieval_max_top_k({self.retrieval_max_top_k}) 보다 작습니다"
            )
        return self

    @model_validator(mode="after")
    def _rerank_window_must_hold_a_query_and_a_chunk(self) -> "Settings":
        """창을 넘으면 토크나이저가 뒤에서부터 조용히 자른다 — 오류 없이 점수만 틀어진다."""
        profile = KNOWN_RERANKER_PROFILES.get(self.reranker_model)
        # 표에 없는 이름은 어댑터가 사용 가능한 값과 함께 거절한다 — 사유를 두 벌로 두지 않는다.
        if not self.reranker_enabled or profile is None:
            return self
        # 창은 토큰, 상한은 문자다. 한국어 서브워드는 문자보다 적게 나오므로 문자 합을
        # 토큰 상한으로 두는 쪽이 안전한 방향이다.
        needed = self.retrieval_max_query_chars + self.chunk_size
        if profile.max_input_tokens < needed:
            raise ValueError(
                f"리랭커 입력 창({profile.max_input_tokens})이 "
                f"retrieval_max_query_chars({self.retrieval_max_query_chars}) 와 "
                f"chunk_size({self.chunk_size}) 의 합을 담지 못합니다"
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
