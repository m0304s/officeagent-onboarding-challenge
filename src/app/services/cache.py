"""캐시 오케스트레이션 — 조회 순서, 현재성 재검증, 저장 정책, 축소 동작.

정책이 전부 여기 있다. 저장소는 맡아 두고 걷어내는 일만 하고, "지금 이 항목을 써도
되는가"는 이 층이 판정한다 (design 결정 7·13).
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from app.adapters.protocols import DocumentRegistry, Embedder, ResponseCache
from app.core.answers import FinishReason
from app.core.cache import (
    CachedAnswer,
    CacheLookup,
    derive_cache_key,
    derive_cache_scope,
    negation_polarity,
    normalize_query,
)
from app.core.exceptions import StorageUnavailable

logger = logging.getLogger(__name__)

#: "코퍼스 어디에도 답이 없다"고 주장하며 끝난 종료. 자기가 본 문서만으로는 반증되지
#: 않아 부정 집합이라는 축을 하나 더 받는다 (design 결정 4).
NEGATIVE_FINISH_REASONS = frozenset({FinishReason.NO_EVIDENCE, FinishReason.INSUFFICIENT_EVIDENCE})


@dataclass(frozen=True)
class CacheSlot:
    """이 요청이 캐시에서 차지하는 자리 — 지문·후보 집합·조회 결과·질의 벡터.

    저장이 조회의 재료를 다시 만들지 않도록 한 벌로 들고 다닌다."""

    fingerprint: str
    scope: str
    #: 질의의 부정 극성. 저장과 조회가 같은 값을 봐야 게이트가 성립한다.
    polarity: bool = False
    lookup: CacheLookup = field(default_factory=CacheLookup.miss)
    embedding: tuple[float, ...] | None = None
    #: 저장소에 닿지 못해 미스로 강등된 요청. 히트 여부와 구분되어야 관측이 성립한다.
    degraded: bool = False

    @property
    def hit(self) -> bool:
        return self.lookup.hit

    @property
    def entry(self) -> CachedAnswer | None:
        return self.lookup.entry

    def with_embedding(self, embedding: Sequence[float] | None) -> "CacheSlot":
        """검색이 만든 벡터를 받아 둔다. 이미 들고 있으면 그대로다 — 둘은 같은 벡터다."""
        if self.embedding is not None or embedding is None:
            return self
        return replace(self, embedding=tuple(embedding))


class CircuitBreaker:
    """연속 실패가 쌓이면 쿨다운 동안 저장소를 부르지 않는다.

    상태는 프로세스 메모리다 — 공유하려면 저장소가 필요한데 그것이 지금 죽은 캐시다."""

    def __init__(
        self,
        *,
        failures: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failures
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allows(self) -> bool:
        """지금 저장소를 불러도 되는가. 쿨다운이 지나면 요청 하나가 탐침이 된다."""
        if self._opened_at is None:
            return True
        return self._clock() - self._opened_at >= self._cooldown_seconds

    def record_success(self) -> bool:
        """성공을 기록한다. 이번 호출로 차단이 풀렸으면 참."""
        was_open = self._opened_at is not None
        self._failures = 0
        self._opened_at = None
        return was_open

    def record_failure(self) -> bool:
        """실패를 기록한다. 이번 호출로 차단이 열렸으면 참."""
        self._failures += 1
        if self._opened_at is not None:
            # 열린 상태의 실패는 탐침이 실패했다는 뜻이라 쿨다운을 다시 건다.
            self._opened_at = self._clock()
            return False
        if self._failures < self._threshold:
            return False
        self._opened_at = self._clock()
        return True


class CacheService:
    """질의 하나에 대해 캐시를 조회하고, 답변 하나를 맡긴다.

    캐시 예외가 이 밖으로 나가지 않는다 — 조회 실패는 미스, 저장 실패는 로그다."""

    def __init__(
        self,
        cache: ResponseCache,
        registry: DocumentRegistry,
        embedder: Embedder,
        *,
        prompt_version: str,
        model: str,
        semantic_threshold: float,
        semantic_candidates: int,
        operation_timeout_seconds: float,
        breaker_failures: int,
        breaker_cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache = cache
        self._registry = registry
        self._embedder = embedder
        self._prompt_version = prompt_version
        self._model = model
        self._semantic_threshold = semantic_threshold
        self._semantic_candidates = semantic_candidates
        self._timeout_seconds = operation_timeout_seconds
        self._breaker = CircuitBreaker(
            failures=breaker_failures,
            cooldown_seconds=breaker_cooldown_seconds,
            clock=clock,
        )

    # ── 조회 ────────────────────────────────────────────────────────────

    async def lookup(self, query: str, *, top_k: int, index_signature: str) -> CacheSlot:
        """L1 정확 매치 → (미스면) 질의 임베딩 → L2 유사 매치 → 현재성 재검증.

        정확 매치가 성공하면 임베딩을 만들지 않는다 — 앞 층이 더 싸다."""
        materials = {
            "prompt_version": self._prompt_version,
            "index_signature": index_signature,
            "model": self._model,
        }
        fingerprint = derive_cache_key(query=query, top_k=top_k, **materials)
        scope = derive_cache_scope(**materials)
        # L1 은 정규화 결과가 같은 질의만 맞히므로 극성도 자동으로 같다. 이 값을 보는 것은
        # L2 뿐이지만, 저장이 같은 슬롯에서 꺼내 쓰도록 여기서 한 번만 유도한다.
        polarity = negation_polarity(normalize_query(query))
        slot = CacheSlot(fingerprint=fingerprint, scope=scope, polarity=polarity)

        if not self._breaker.allows():
            return replace(slot, degraded=True)

        try:
            probed = await self._probe(query, slot, index_signature=index_signature)
        except Exception as exc:
            # 캐시는 지연을 줄이는 층이지 답변의 성립 조건이 아니다. 어떤 실패도
            # 미스로 강등된다.
            self._on_failure(exc, action="조회")
            return replace(slot, degraded=True)

        self._on_success()
        self._log_lookup(probed)
        return probed

    async def _probe(self, query: str, slot: CacheSlot, *, index_signature: str) -> CacheSlot:
        exact = await self._call(self._cache.lookup_exact(slot.fingerprint))
        if exact.hit:
            # 재검증에서 버렸어도 L2 로 내려가지 않는다 — 문서가 방금 바뀌었다는 신호라
            # 같은 집합의 이웃도 낡았을 가능성이 높고, 그 확인에 임베딩이 한 번 더 든다.
            verified = await self._verify(exact, index_signature=index_signature)
            return replace(slot, lookup=verified)

        if not await self._call(self._cache.count_candidates(slot.scope, polarity=slot.polarity)):
            return slot

        embedding = await self._embed(query)
        semantic = await self._call(
            self._cache.lookup_semantic(
                embedding,
                scope=slot.scope,
                polarity=slot.polarity,
                threshold=self._semantic_threshold,
                candidates=self._semantic_candidates,
            )
        )
        if semantic.hit:
            semantic = await self._verify(semantic, index_signature=index_signature)
        return replace(slot, lookup=semantic, embedding=embedding)

    async def _verify(self, lookup: CacheLookup, *, index_signature: str) -> CacheLookup:
        """인용 문서가 지금도 현재 리비전인가. 하나라도 어긋나면 항목을 지우고 미스.

        판정은 `Document.is_up_to_date` 가 한다 — 검색과 다른 규칙을 쓰면 조용히 갈린다."""
        if lookup.entry is None or lookup.fingerprint is None:
            return CacheLookup.miss()

        try:
            for version in lookup.entry.source_versions:
                document = await self._registry.get(version.document_id)
                if document is not None and document.is_up_to_date(
                    revision=version.revision, index_signature=index_signature
                ):
                    continue
                await self._discard(lookup.fingerprint)
                return CacheLookup.miss()
        except Exception as exc:
            # 레지스트리 장애는 캐시 저장소의 상태가 아니라 차단기에 세지 않는다.
            logger.warning("캐시 항목의 현재성을 확인하지 못했습니다", exc_info=exc)
            return CacheLookup.miss()
        return lookup

    async def _embed(self, query: str) -> tuple[float, ...]:
        """L1 이 키로 삼은 것과 같은 문자열을 벡터로 만든다.

        두 층이 다른 질의를 보면 "같은 질문"의 뜻이 층마다 갈린다."""
        return tuple(await self._embedder.embed_query(normalize_query(query)))

    async def _discard(self, fingerprint: str) -> None:
        """재검증에서 버린 항목을 지운다. 남기면 요청마다 같은 비용을 다시 문다."""
        try:
            await self._call(self._cache.discard(fingerprint))
        except Exception as exc:
            logger.warning("낡은 캐시 항목을 지우지 못했습니다", exc_info=exc)
            return
        logger.info(
            "인용 문서가 낡아 캐시 항목을 버렸습니다",
            extra={"cache_fingerprint": fingerprint[:12]},
        )

    # ── 저장 ────────────────────────────────────────────────────────────

    async def store(self, slot: CacheSlot, entry: CachedAnswer, *, query: str) -> None:
        """답변 하나를 맡긴다. 실패는 삼키고 로그로 남긴다.

        `error` 로 끝난 스트림에는 `Answer` 가 없어 항목 자체가 만들어지지 않는다."""
        if slot.degraded or not self._breaker.allows():
            return

        try:
            # 후보 집합이 비어 있어 조회가 임베딩을 건너뛴 경우다. 그 최적화가 아끼는
            # 것은 첫 바이트까지의 시간이라, 생성이 끝난 뒤의 이 비용과 바꿀 만하다.
            embedding = slot.embedding or await self._embed(query)
            await self._call(
                self._cache.store(
                    slot.fingerprint,
                    entry,
                    scope=slot.scope,
                    polarity=slot.polarity,
                    embedding=embedding,
                    negative=entry.answer.finish_reason in NEGATIVE_FINISH_REASONS,
                )
            )
        except Exception as exc:
            self._on_failure(exc, action="저장")
            return
        self._on_success()

    # ── 무효화 ──────────────────────────────────────────────────────────

    async def invalidate_document(self, document_id: str) -> int:
        """그 문서를 근거로 삼은 항목을 걷어낸다. 실패는 수집을 실패시키지 않는다."""
        return await self._invalidate(
            self._cache.invalidate_document(document_id),
            reason="document",
            document_id=document_id,
        )

    async def invalidate_negative(self) -> int:
        """근거 없음·답변 불가 판정을 걷어낸다 — 코퍼스 내용이 바뀌면 그 주장이 깨진다."""
        return await self._invalidate(self._cache.invalidate_negative(), reason="negative")

    async def _invalidate(self, awaitable, *, reason: str, document_id: str | None = None) -> int:
        try:
            removed = await self._call(awaitable)
        except Exception as exc:
            logger.warning(
                "캐시 무효화에 실패했습니다",
                extra={"cache_invalidation": reason, "document_id": document_id},
                exc_info=exc,
            )
            return 0
        if removed:
            logger.info(
                "캐시 항목을 무효화했습니다",
                extra={
                    "cache_invalidation": reason,
                    "document_id": document_id,
                    "cache_invalidated": removed,
                },
            )
        return removed

    # ── 상한과 관측 ─────────────────────────────────────────────────────

    async def _call(self, awaitable):
        """작업마다 시간 상한을 건다.

        상한이 없으면 죽은 저장소가 "미스"가 아니라 "느린 미스"가 된다 (design 결정 13)."""
        return await asyncio.wait_for(awaitable, self._timeout_seconds)

    def _on_success(self) -> None:
        if self._breaker.record_success():
            logger.info("캐시 저장소가 응답해 조회를 재개합니다")

    def _on_failure(self, exc: BaseException, *, action: str) -> None:
        """저장소 실패만 차단기에 센다. 전이 시점에만 경고를 남긴다."""
        if isinstance(exc, StorageUnavailable | TimeoutError) and self._breaker.record_failure():
            logger.warning(
                "캐시 저장소가 연속으로 실패해 한동안 건너뜁니다",
                extra={"cache_action": action},
                exc_info=exc,
            )
            return
        logger.debug("캐시 접근에 실패해 축소 동작으로 처리합니다", exc_info=exc)

    def _log_lookup(self, slot: CacheSlot) -> None:
        """질문 원문·답변 본문·청크 본문은 남기지 않는다 — 지문은 되돌릴 수 없는 값이다."""
        logger.debug(
            "캐시를 조회했습니다",
            extra={
                "cache_hit": slot.hit,
                "cache_layer": slot.lookup.layer.value if slot.lookup.layer else None,
                "cache_similarity": slot.lookup.similarity,
                "cache_degraded": slot.degraded,
                "cache_fingerprint": slot.fingerprint[:12],
            },
        )
