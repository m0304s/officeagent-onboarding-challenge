"""kiwipiepy 형태소 토크나이저 — 어휘 색인용 내용어 토큰을 낸다.

내용어 태그만 남기고 조사·어미·기호는 버린다. 규칙 기반에서 넘어온 근거는
`docs/superpowers/specs/2026-08-14-kiwipiepy-tokenizer-design.md` 에 있다.
"""

import asyncio
import json
import logging
import threading
from typing import Any

import kiwipiepy

from app.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

KIWI_TOKENIZER_VERSION = 1

#: 남기는 형태소 태그 — 명사·동사·형용사·어근·외국어·숫자·한자. 조사·어미·기호는 버린다.
CONTENT_TAGS = frozenset({"NNG", "NNP", "VV", "VA", "XR", "SL", "SN", "SH"})

_WARM_UP_TEXT = "워밍업"


class KiwiTokenizer:
    """kiwipiepy 로 형태소를 뽑아 내용어만 토큰으로 낸다."""

    def __init__(self, *, content_tags: frozenset[str] = CONTENT_TAGS) -> None:
        self._content_tags = content_tags
        self._kiwi: Any = None
        # 잠그지 않으면 서로 다른 워커의 첫 호출이 모델을 두 벌 올린다.
        self._load_lock = threading.Lock()

    def tokenize(self, text: str) -> tuple[str, ...]:
        """내용어 형태소만 casefold 해 낸다. 중복은 그대로 둔다 — BM25 의 빈도 항이 읽는다."""
        kiwi = self._ensure_kiwi()
        return tuple(
            token.form.casefold()
            for token in kiwi.tokenize(text)
            if token.tag in self._content_tags
        )

    @property
    def signature_material(self) -> str:
        """`derive_index_signature` 재료. 분석기·판·태그 집합이 바뀌면 값이 달라진다."""
        return json.dumps(
            {
                "tokenizer": "kiwipiepy",
                "version": KIWI_TOKENIZER_VERSION,
                "engine_version": kiwipiepy.__version__,
                "content_tags": sorted(self._content_tags),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    async def warm_up(self) -> None:
        """모델을 미리 올린다. 임베더·리랭커와 같은 자리, 같은 처분이다."""
        await asyncio.to_thread(self.tokenize, _WARM_UP_TEXT)

    def _ensure_kiwi(self) -> Any:
        """Kiwi 인스턴스를 한 번만 만든다. 블로킹이라 스레드풀 안에서 불린다."""
        if self._kiwi is not None:
            return self._kiwi
        with self._load_lock:
            if self._kiwi is None:
                self._kiwi = self._load()
        return self._kiwi

    def _load(self) -> Any:
        """가중치를 올린다. 실패는 도메인 예외로 세운다 — 빈 결과로 위장하지 않는다."""
        try:
            from kiwipiepy import Kiwi
        except ImportError as exc:  # pragma: no cover - 의존성이 빠진 배포에서만 발생
            raise ConfigurationError(
                "어휘 토크나이저 런타임(kiwipiepy)이 설치되어 있지 않습니다"
            ) from exc
        logger.info("kiwipiepy 모델을 로딩합니다")
        return Kiwi()
