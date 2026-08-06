"""로컬 크로스인코더 리랭커.

점수 규약(로짓 → 시그모이드)이 이 파일 밖으로 나가지 않는다 — 상위 계층은 순서만 본다.
모델 후보 비교와 기각 사유는 `ARCHITECTURE.md` 리랭킹 절에 있다.
"""

import asyncio
import logging
import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

#: 점수 규약의 판 번호. 규약이 바뀌면 같은 모델도 다른 값을 내므로 서명에 들어간다.
SCORE_CONVENTION = "sigmoid-v1"


@dataclass(frozen=True)
class RerankerProfile:
    """모델을 로딩하지 않고도 알아야 하는 사실."""

    max_input_tokens: int
    #: 가중치·설정·토크나이저를 한꺼번에 고정한다. 원격 코드를 쓰는 모델이었다면 코드는
    #: 다른 저장소에 있어 이것으로 고정되지 않았다 (design 결정 8).
    revision: str


# 표에 없는 이름은 기동을 막는다. 입력 창을 모르면 질의+청크가 조용히 잘리는 구성을
# 걸러낼 수 없다.
KNOWN_RERANKER_PROFILES: dict[str, RerankerProfile] = {
    "BAAI/bge-reranker-v2-m3": RerankerProfile(
        max_input_tokens=8192,
        revision="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
    ),
}

_WARM_UP_QUERY = "워밍업"
_WARM_UP_DOCUMENT = "워밍업 문서"


def _identity(scores: Any) -> Any:
    """활성화를 우리 손에 둔다 — 라이브러리 기본값은 판마다 다르고, 두 번 씌우면 값이 눌린다."""
    return scores


class CrossEncoderReranker:
    """sentence-transformers 크로스인코더로 후보를 채점한다.

    로딩 경로가 둘인 것은 지연 로딩이 선로딩 실패의 백스톱이기 때문이다."""

    def __init__(self, model_name: str, *, revision: str | None = None) -> None:
        profile = KNOWN_RERANKER_PROFILES.get(model_name)
        if profile is None:
            known = ", ".join(sorted(KNOWN_RERANKER_PROFILES))
            raise ConfigurationError(
                f"모양을 알지 못하는 리랭커 모델입니다: {model_name} — 사용 가능한 값: {known}"
            )

        self.name = model_name
        self.max_input_tokens = profile.max_input_tokens
        self._revision = revision or profile.revision
        # 리비전이 서명에 드는 이유는 같은 이름이 다른 가중치를 가리킬 수 있어서다.
        self.signature = f"{model_name}@{self._revision}/{SCORE_CONVENTION}"

        self._model: Any = None
        # 잠그지 않으면 서로 다른 워커의 첫 호출이 모델을 두 벌 올린다.
        self._load_lock = threading.Lock()

    # ── 채점 ────────────────────────────────────────────────────────────

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        # 여기가 오프로드 지점이다 — 루프에서 돌리면 검색 한 건이 헬스 응답까지 멈춘다.
        return await asyncio.to_thread(self._score_blocking, query, list(documents))

    def _score_blocking(self, query: str, documents: list[str]) -> list[float]:
        model = self._ensure_model()
        logits = model.predict(
            [(query, document) for document in documents],
            show_progress_bar=False,
        )
        # numpy 타입이 새면 어댑터 교체가 상위 계층까지 번진다.
        return [_sigmoid(float(logit)) for logit in logits]

    # ── 선로딩 ──────────────────────────────────────────────────────────

    async def warm_up(self) -> None:
        """가중치를 올리고 실제로 한 번 채점한다.

        첫 `predict` 에 별도의 초기화 비용이 남아 로딩만으로는 부족하다."""
        logger.info("리랭커 모델을 미리 준비합니다", extra={"reranker_model": self.name})
        await self.rerank(_WARM_UP_QUERY, [_WARM_UP_DOCUMENT])
        logger.info("리랭커 모델 준비 완료", extra={"reranker_model": self.name})

    # ── 로딩 ────────────────────────────────────────────────────────────

    def _ensure_model(self) -> Any:
        """블로킹. 선로딩이 실패했을 때 리랭킹을 살리는 백스톱이다."""
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:
                self._model = self._load()
        return self._model

    def _load(self) -> Any:
        """가중치를 올린다. 블로킹이라 항상 스레드풀 안이다.

        실패는 호출부가 융합 순서로 축소해 처리한다 — 검색을 세우지 않는다."""
        # import 자체가 무겁다(torch). 모듈 최상단이 아니라 여기서 가져온다.
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - 의존성이 빠진 배포에서만 발생
            raise ConfigurationError(
                "리랭킹 런타임(sentence-transformers)이 설치되어 있지 않습니다"
            ) from exc

        logger.info("리랭커 모델을 로딩합니다", extra={"reranker_model": self.name})
        # `trust_remote_code` 를 켜지 않는다 — 표의 모델은 아키텍처가 transformers 안에
        # 있어 원격 코드가 필요 없다 (design 결정 1·8).
        model = CrossEncoder(
            self.name,
            revision=self._revision,
            activation_fn=_identity,
        )
        self._assert_matches_declaration(model)
        return model

    def _assert_matches_declaration(self, model: Any) -> None:
        """선언한 값과 실제 모델이 어긋나면 여기서 멈춘다.

        조용히 넘어가면 입력이 잘리거나 값이 눌린 채로 순위가 매겨진다."""
        if model.activation_fn is not _identity:
            raise ConfigurationError(
                f"리랭커의 활성화를 우리 것으로 바꾸지 못했습니다: {self.name}"
            )

        if model.num_labels != 1:
            raise ConfigurationError(
                f"점수를 하나만 내는 모델이 아닙니다: {self.name} — 라벨 {model.num_labels}"
            )

        # 5.x 가 개명했다 — 옛 이름만 부르면 경고, 새 이름만 부르면 구버전에서 터진다.
        window = getattr(model, "max_seq_length", None) or getattr(model, "max_length", None)
        if window is None:
            raise ConfigurationError(f"리랭커의 입력 창을 읽지 못했습니다: {self.name}")
        if window < self.max_input_tokens:
            raise ConfigurationError(
                f"모델 입력 창이 선언보다 좁습니다: {self.name} "
                f"— 선언 {self.max_input_tokens}, 실제 {window}"
            )


def _sigmoid(logit: float) -> float:
    """로짓 하나를 `(0, 1)` 로. 단조라 순서를 바꾸지 않는다 — 읽기 위한 변환이다.

    두 갈래인 것은 큰 음수에서 `exp` 가 넘치기 때문이다. 포화하면 끝점에 정확히 닿는다."""
    if logit >= 0:
        return 1.0 / (1.0 + math.exp(-logit))
    odds = math.exp(logit)
    return odds / (1.0 + odds)
