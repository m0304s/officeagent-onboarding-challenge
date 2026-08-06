"""캐시가 꺼진 상태의 구현 — 조회는 언제나 미스, 저장은 무동작.

`cache_enabled=false` 를 인메모리로 잇지 않는 이유는 그러면 캐시를 껐는데 히트가 계속
나기 때문이다 (design 결정 12).
"""

import logging
from collections.abc import Sequence

from app.core.cache import CachedAnswer, CacheLookup

logger = logging.getLogger(__name__)


class NullResponseCache:
    """`cache_enabled=false` 가 배선되는 자리. 상태를 갖지 않는다."""

    def __init__(self) -> None:
        # 꺼짐은 의도된 상태라 기동 시 한 줄이면 된다 — 요청마다 찍으면 진짜 신호가 묻힌다.
        logger.info("응답 캐시가 비활성화되어 모든 요청이 미스로 처리됩니다")

    async def lookup_exact(self, fingerprint: str) -> CacheLookup:
        return CacheLookup.miss()

    async def count_candidates(self, scope: str, *, polarity: bool) -> int:
        return 0

    async def lookup_semantic(
        self,
        embedding: Sequence[float],
        *,
        scope: str,
        polarity: bool,
        threshold: float,
        candidates: int,
    ) -> CacheLookup:
        return CacheLookup.miss()

    async def store(
        self,
        fingerprint: str,
        entry: CachedAnswer,
        *,
        scope: str,
        polarity: bool,
        embedding: Sequence[float],
        negative: bool,
    ) -> None:
        return None

    async def invalidate_document(self, document_id: str) -> int:
        return 0

    async def invalidate_negative(self) -> int:
        return 0

    async def discard(self, fingerprint: str) -> None:
        return None
