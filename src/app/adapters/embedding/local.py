"""로컬 sentence-transformers 임베더.

e5 계열의 역할 접두사가 이 파일 밖으로 나가지 않는다 — 상위 계층은 두 메서드만 본다.
모델 후보 비교는 `ARCHITECTURE.md` 임베딩 절에 있다.
"""

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

# 한쪽만 붙이거나 둘 다 빠뜨리면 검색 품질이 조용히 무너진다.
_PASSAGE_PREFIX = "passage: "
_QUERY_PREFIX = "query: "

#: 접두사 규약의 판 번호. 규약이 다르면 다른 벡터가 나와 재색인이 필요하다.
PREFIX_CONVENTION = "e5-prefix-v1"


@dataclass(frozen=True)
class ModelProfile:
    """모델을 로딩하지 않고도 알아야 하는 사실."""

    dimension: int
    max_input_tokens: int


# 표를 두는 이유는 모양을 모델 로딩 없이 답해야 하기 때문이다 — 선로딩이 실패한 뒤에도
# 서명은 답할 수 있어야 목록·삭제 경로가 함께 죽지 않는다. 표에 없는 이름은 기동을 막는다.
KNOWN_MODEL_PROFILES: dict[str, ModelProfile] = {
    "intfloat/multilingual-e5-small": ModelProfile(dimension=384, max_input_tokens=512),
    "intfloat/multilingual-e5-base": ModelProfile(dimension=768, max_input_tokens=512),
}


#: 선로딩이 인코딩까지 해 보는 데 쓰는 텍스트. 한국어인 이유는 토크나이저가 다국어
#: 자산을 함께 초기화하게 하려는 것이다.
_WARM_UP_TEXT = "워밍업"


class SentenceTransformerEmbedder:
    """sentence-transformers 모델로 인코딩한다.

    로딩 경로가 둘인 것은 지연 로딩이 선로딩 실패의 백스톱이기 때문이다."""

    def __init__(self, model_name: str, *, normalize: bool = True) -> None:
        profile = KNOWN_MODEL_PROFILES.get(model_name)
        if profile is None:
            known = ", ".join(sorted(KNOWN_MODEL_PROFILES))
            raise ConfigurationError(
                f"모양을 알지 못하는 임베딩 모델입니다: {model_name} — 사용 가능한 값: {known}"
            )

        self._model_name = model_name
        self._normalize = normalize
        self.dimension = profile.dimension
        self.max_input_tokens = profile.max_input_tokens

        # 조직 접두사를 떼면 다른 조직의 동명 모델이 같은 서명을 받는다.
        self.signature = (
            f"{model_name}/{self.dimension}/{'l2norm' if normalize else 'raw'}/{PREFIX_CONVENTION}"
        )

        self._model: Any = None
        # 잠그지 않으면 서로 다른 워커의 첫 호출이 모델을 두 벌 올린다.
        self._load_lock = threading.Lock()

    # ── 인코딩 ──────────────────────────────────────────────────────────

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._encode([_PASSAGE_PREFIX + text for text in texts])

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._encode([_QUERY_PREFIX + text])
        return vectors[0]

    async def _encode(self, prefixed: list[str]) -> list[list[float]]:
        if not prefixed:
            return []
        # 여기가 오프로드 지점이다 — 루프에서 돌리면 수집 한 건이 헬스 응답까지 멈춘다.
        return await asyncio.to_thread(self._encode_blocking, prefixed)

    def _encode_blocking(self, prefixed: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        vectors = model.encode(
            prefixed,
            normalize_embeddings=self._normalize,
            show_progress_bar=False,
        )
        # numpy 타입이 새면 어댑터 교체가 상위 계층까지 번진다.
        return [[float(value) for value in row] for row in vectors]

    # ── 선로딩 ──────────────────────────────────────────────────────────

    async def warm_up(self) -> None:
        """가중치를 올리고 실제로 한 번 인코딩한다.

        첫 `encode` 에 별도의 초기화 비용이 남아 로딩만으로는 부족하다."""
        logger.info("임베딩 모델을 미리 준비합니다", extra={"embedding_model": self._model_name})
        await self._encode([_PASSAGE_PREFIX + _WARM_UP_TEXT])
        logger.info(
            "임베딩 모델 준비 완료",
            extra={"embedding_model": self._model_name, "embedding_dimension": self.dimension},
        )

    # ── 토큰 계산 ───────────────────────────────────────────────────────

    def count_document_tokens(self, text: str) -> int:
        """청크 하나가 몇 토큰으로 인코딩되는지 — 문서 경로 기준. 수집 가드가 쓴다."""
        return self._count(_PASSAGE_PREFIX + text)

    def count_query_tokens(self, text: str) -> int:
        """질의 하나가 몇 토큰으로 인코딩되는지 — 질의 경로 기준.

        질의에는 재분할이 성립하지 않아 호출부가 자르지 않고 거부한다."""
        return self._count(_QUERY_PREFIX + text)

    def _count(self, prefixed: str) -> int:
        """블로킹. 스레드풀에서만 호출한다.

        접두사와 특수 토큰까지 센다 — 본문만 세면 상한 바로 아래 입력이 조용히 잘린다."""
        model = self._ensure_model()
        return len(model.tokenizer.encode(prefixed))

    # ── 로딩 ────────────────────────────────────────────────────────────

    def _ensure_model(self) -> Any:
        """블로킹. 선로딩이 실패했을 때 수집을 살리는 백스톱이다."""
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:
                self._model = self._load()
        return self._model

    def _load(self) -> Any:
        """가중치를 올린다. 블로킹이라 항상 스레드풀 안이다.

        기동 경로에서도 불리지만 그 실패가 기동을 막지는 않는다."""
        # import 자체가 무겁다(torch). 모듈 최상단이 아니라 여기서 가져온다.
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - 의존성이 빠진 배포에서만 발생
            raise ConfigurationError(
                "임베딩 런타임(sentence-transformers)이 설치되어 있지 않습니다"
            ) from exc

        logger.info("임베딩 모델을 로딩합니다", extra={"embedding_model": self._model_name})
        model = SentenceTransformer(self._model_name)
        self._assert_matches_declaration(model)
        return model

    def _assert_matches_declaration(self, model: Any) -> None:
        """선언한 값과 실제 모델이 어긋나면 여기서 멈춘다.

        선언이 틀리면 서명이 거짓이 되거나 토큰 가드가 상한을 넘겨 통과시킨다."""
        # 5.x 가 개명했다 — 옛 이름만 부르면 경고, 새 이름만 부르면 구버전에서 터진다.
        read_dimension = getattr(model, "get_embedding_dimension", None) or (
            model.get_sentence_embedding_dimension
        )
        actual_dimension = read_dimension()
        if actual_dimension != self.dimension:
            raise ConfigurationError(
                f"임베딩 차원이 선언과 다릅니다: {self._model_name} "
                f"— 선언 {self.dimension}, 실제 {actual_dimension}"
            )

        actual_window = getattr(model, "max_seq_length", None)
        if actual_window is not None and actual_window < self.max_input_tokens:
            raise ConfigurationError(
                f"모델 입력 창이 선언보다 좁습니다: {self._model_name} "
                f"— 선언 {self.max_input_tokens}, 실제 {actual_window}"
            )
