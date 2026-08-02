"""로컬 sentence-transformers 임베더.

`intfloat/multilingual-e5-small` 을 기본으로 쓴다. 샘플 문서가 한국어라 영어 전용
모델은 검색이 사실상 동작하지 않고, 입력 창이 좁은 다국어 모델(128 토큰)은 청크
뒷부분이 조용히 잘린다 — 잘린 부분은 벡터에 반영되지 않으면서 청크 본문에는 남아
"저장은 됐는데 검색되지 않는 텍스트"가 생긴다. 후보 비교는 `ARCHITECTURE.md` 참조.

**e5 계열의 역할 접두사는 이 파일 밖으로 나가지 않는다.** 상위 계층은
`embed_documents`/`embed_query` 두 메서드만 보고, 접두사의 존재도 문자열도 모른다.
"""

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

# e5 계열이 요구하는 역할 접두사. 문서와 질의를 서로 다른 공간으로 보내는 규약이라,
# 한쪽만 붙이거나 둘 다 빠뜨리면 검색 품질이 조용히 무너진다.
_PASSAGE_PREFIX = "passage: "
_QUERY_PREFIX = "query: "

#: 접두사 규약의 판 번호. 접두사 문자열이나 적용 범위를 바꾸면 올린다 — 같은 모델이라도
#: 규약이 다르면 다른 벡터가 나오므로, 이 값이 `signature` 를 통해 재색인을 강제한다.
PREFIX_CONVENTION = "e5-prefix-v1"


@dataclass(frozen=True)
class ModelProfile:
    """모델을 로딩하지 않고도 알아야 하는 사실."""

    dimension: int
    max_input_tokens: int


# 이 어댑터가 모양을 아는 모델들.
#
# 표를 두는 이유는 `dimension`·`max_input_tokens` 를 **모델 로딩 없이** 답해야 하기
# 때문이다. 색인 서명은 수집이 시작되기 전에 필요하고, 무엇보다 **선로딩이 실패한
# 뒤에도** 서명은 답할 수 있어야 한다 — 모델을 못 올린 상태에서 차원을 물으면 또
# 실패하는 구조라면 목록·삭제 같은 경로까지 함께 죽는다.
#
# 표에 없는 이름은 기동을 막는다. 차원을 추측해 서명에 넣으면 서명이 거짓이 되고,
# 서명이 거짓이면 재색인 강제라는 목적 자체가 사라진다. `chunk_strategy` 에 미구현
# 전략 이름을 넣지 못하게 한 것과 같은 규율이다.
KNOWN_MODEL_PROFILES: dict[str, ModelProfile] = {
    "intfloat/multilingual-e5-small": ModelProfile(dimension=384, max_input_tokens=512),
    "intfloat/multilingual-e5-base": ModelProfile(dimension=768, max_input_tokens=512),
}


#: 선로딩이 실제로 인코딩까지 해 보는 데 쓰는 텍스트.
#:
#: 짧고 무해한 한 줄이면 충분하다. 목적은 벡터의 내용이 아니라 **경로가 끝까지 도는지**
#: 확인하는 것이다. 한국어를 쓰는 이유는 이 서비스가 실제로 다루는 입력이 그것이라,
#: 토크나이저가 다국어 자산을 함께 초기화하게 하기 위해서다.
_WARM_UP_TEXT = "워밍업"


class SentenceTransformerEmbedder:
    """sentence-transformers 모델로 인코딩한다.

    로딩은 **기동 시 선로딩**(`warm_up`)과 **첫 사용 시 지연 로딩** 두 경로를 모두
    갖는다. 후자를 남겨 두는 이유는 선로딩이 실패했을 때의 백스톱이기 때문이다 —
    선로딩 실패가 곧 영구 실패가 되면, 디스크 경합 같은 일시적 원인으로 서비스가
    재시작 전까지 수집을 못 하게 된다.
    """

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

        # 모델 식별자에 `/` 가 들어가지만 필드 수가 고정(뒤에서 세 칸)이라 값이 섞이지
        # 않는다. 조직 접두사를 떼면 다른 조직의 동명 모델이 같은 서명을 받는다.
        self.signature = (
            f"{model_name}/{self.dimension}/{'l2norm' if normalize else 'raw'}/{PREFIX_CONVENTION}"
        )

        self._model: Any = None
        # 인코딩과 토큰 계산이 서로 다른 워커 스레드에서 동시에 첫 호출될 수 있다.
        # 잠그지 않으면 모델을 두 벌 올려 메모리가 두 배가 된다.
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
        # **여기가 오프로드 지점이다.** 인코딩은 이 서비스에서 가장 무거운 CPU 작업이고
        # 배치마다 반복 호출된다. 이벤트 루프에서 돌리면 수집 한 건이 헬스 응답까지
        # 멈춰 세운다.
        return await asyncio.to_thread(self._encode_blocking, prefixed)

    def _encode_blocking(self, prefixed: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        vectors = model.encode(
            prefixed,
            normalize_embeddings=self._normalize,
            show_progress_bar=False,
        )
        # numpy 배열을 그대로 내보내지 않는다 — 상위 계층이 임베딩 런타임의 타입을
        # 알게 되면 어댑터 교체가 그 계층까지 번진다.
        return [[float(value) for value in row] for row in vectors]

    # ── 선로딩 ──────────────────────────────────────────────────────────

    async def warm_up(self) -> None:
        """가중치를 올리고 **실제로 한 번 인코딩한다.**

        로딩만으로는 부족하다. 첫 `encode` 에는 로딩과 별개의 초기화 비용(연산 그래프
        준비, 커널 워밍)이 남아, "첫 요청 지연을 없앤다"는 목적이 절반만 달성된다.
        한 번 돌려 보면 "이 모델로 벡터가 나오는가"까지 기동 시점에 확인된다.

        **실패를 삼키지 않는다.** 기동을 계속할지는 호출자(앱 팩토리)가 정한다. 여기서
        조용히 넘어가면 무엇이 준비되지 않았는지가 사라진다.
        """
        logger.info("임베딩 모델을 미리 준비합니다", extra={"embedding_model": self._model_name})
        await self._encode([_PASSAGE_PREFIX + _WARM_UP_TEXT])
        logger.info(
            "임베딩 모델 준비 완료",
            extra={"embedding_model": self._model_name, "embedding_dimension": self.dimension},
        )

    # ── 토큰 계산 ───────────────────────────────────────────────────────

    def count_tokens(self, text: str) -> int:
        """청크 하나가 실제로 몇 토큰으로 인코딩되는지.

        **접두사를 포함해 센다.** 실제로 인코딩되는 문자열이 `passage: ` + 본문이라,
        본문만 세면 접두사 몫만큼 과소 계산되어 상한 바로 아래 청크가 조용히 잘린다.

        문서 경로(`passage: `) 기준이다. 이 값을 쓰는 곳이 수집의 토큰 가드뿐이고,
        질의는 청크보다 훨씬 짧아 가드 대상이 아니다.

        **블로킹이다.** 토크나이저 호출이고, 첫 호출은 모델 로딩까지 유발한다.
        `DocumentParser.parse` 와 같은 이유로 동기로 두어 호출부가 오프로드를 의식하게
        한다.
        """
        model = self._ensure_model()
        return len(model.tokenizer.encode(_PASSAGE_PREFIX + text))

    # ── 로딩 ────────────────────────────────────────────────────────────

    def _ensure_model(self) -> Any:
        """블로킹. 스레드풀에서만 호출한다.

        선로딩이 이미 올려 두었으면 그대로 쓰고, 아니면 여기서 올린다 — 선로딩이
        실패했을 때 수집을 살리는 백스톱이다.
        """
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:
                self._model = self._load()
        return self._model

    def _load(self) -> Any:
        """가중치를 올린다. 블로킹이라 항상 스레드풀 안이다.

        기동 경로에서도 불리지만(`warm_up`), 그 실패가 기동을 막지는 않는다 — 앱
        팩토리가 경고만 남기고 계속 뜬다. "설정 없이 기동된다"는 기존 요구사항이
        선로딩이 생겼다고 약해지지 않는다.
        """
        # import 자체가 무겁다(torch 로딩). 모듈 최상단이 아니라 여기서 가져온다.
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

        `dimension`·`max_input_tokens` 를 표에서 선언하는 대가가 이것이다 — 선언이
        틀리면 서명이 거짓이 되고(차원), 토큰 가드가 상한을 넘겨 통과시킨다(입력 창).
        둘 다 조용히 틀리는 종류라, 로딩이 성공한 그 자리에서 확인한다.
        """
        # sentence-transformers 5.x 가 `get_sentence_embedding_dimension` 을
        # `get_embedding_dimension` 으로 개명했다. 옛 이름만 부르면 경고가 뜨고,
        # 새 이름만 부르면 구버전에서 터진다.
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
