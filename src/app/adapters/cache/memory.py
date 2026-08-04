"""테스트가 주입하는 인메모리 구현 — TTL·상한·유사 매치·태그 무효화 전체 의미.

프로세스 로컬이라 워커가 둘이면 캐시도 둘이고 무효화가 프로세스 경계를 넘지 못한다.
프로덕션 배선이 여기로 떨어지는 분기를 만들지 않는다 (design 결정 12).
"""

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from app.core.cache import CachedAnswer, CacheLookup, best_match


@dataclass
class _Entry:
    """맡아 둔 항목 하나 — 페이로드·후보 집합·질의 벡터·만료 시각·저장 순번."""

    answer: CachedAnswer
    scope: str
    embedding: tuple[float, ...]
    expires_at: float
    sequence: int


class InMemoryResponseCache:
    """Redis 구현과 같은 의미를 프로세스 메모리로 낸다.

    `clock` 을 받는 것은 TTL 검증이 실제로 기다리지 않게 하려는 것이다."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._documents: dict[str, set[str]] = {}
        self._negative: set[str] = set()
        # 시계가 아니라 카운터로 나이를 매긴다 — 같은 순간의 두 항목 순서가 정해지지
        # 않으면 "가장 최근 N개"가 흔들려 테스트가 확률적이 된다 (design 결정 2).
        self._sequence = 0

    # ── 조회 ────────────────────────────────────────────────────────────

    async def lookup_exact(self, fingerprint: str) -> CacheLookup:
        entry = self._live(fingerprint)
        if entry is None:
            return CacheLookup.miss()
        return CacheLookup.exact(fingerprint, entry.answer)

    async def lookup_semantic(
        self,
        embedding: Sequence[float],
        *,
        scope: str,
        threshold: float,
        candidates: int,
    ) -> CacheLookup:
        match = best_match(embedding, self._candidates(scope, candidates), threshold=threshold)
        if match is None:
            return CacheLookup.miss()
        return CacheLookup.semantic(
            match.fingerprint, self._entries[match.fingerprint].answer, match.similarity
        )

    # ── 저장 ────────────────────────────────────────────────────────────

    async def store(
        self,
        fingerprint: str,
        entry: CachedAnswer,
        *,
        scope: str,
        embedding: Sequence[float],
        negative: bool,
    ) -> None:
        self._sequence += 1
        self._entries[fingerprint] = _Entry(
            answer=entry,
            scope=scope,
            embedding=tuple(embedding),
            expires_at=self._clock() + self._ttl_seconds,
            sequence=self._sequence,
        )
        for document_id in entry.document_ids:
            self._documents.setdefault(document_id, set()).add(fingerprint)
        if negative:
            self._negative.add(fingerprint)
        self._enforce_capacity()

    # ── 무효화 ──────────────────────────────────────────────────────────

    async def invalidate_document(self, document_id: str) -> int:
        return self._remove_all(self._documents.get(document_id, set()))

    async def invalidate_negative(self) -> int:
        return self._remove_all(self._negative)

    async def discard(self, fingerprint: str) -> None:
        self._remove(fingerprint)

    # ── 내부 ────────────────────────────────────────────────────────────

    def _live(self, fingerprint: str) -> _Entry | None:
        """만료되지 않은 항목. 만료된 것은 여기서 걷어낸다 (지연 정리)."""
        entry = self._entries.get(fingerprint)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            self._remove(fingerprint)
            return None
        return entry

    def _candidates(self, scope: str, limit: int) -> Iterator[tuple[str, tuple[float, ...]]]:
        """같은 후보 집합의 최근 항목부터 최대 `limit` 개.

        상한이 스캔 비용을 고정한다 — 없으면 캐시가 클수록 히트가 미스보다 느려진다."""
        # 순회 중에 `_live` 가 만료 항목을 지우므로 지문 목록을 먼저 뜬다.
        live = [
            (fingerprint, entry)
            for fingerprint, entry in ((fp, self._live(fp)) for fp in tuple(self._entries))
            if entry is not None and entry.scope == scope
        ]
        live.sort(key=lambda item: item[1].sequence, reverse=True)
        return ((fingerprint, entry.embedding) for fingerprint, entry in live[:limit])

    def _enforce_capacity(self) -> None:
        """상한을 넘으면 오래된 것부터 밀어낸다. 저장을 거부하는 경로는 없다."""
        while len(self._entries) > self._max_entries:
            oldest = min(self._entries, key=lambda key: self._entries[key].sequence)
            self._remove(oldest)

    def _remove_all(self, fingerprints: set[str]) -> int:
        removed = sum(1 for fingerprint in tuple(fingerprints) if self._remove(fingerprint))
        return removed

    def _remove(self, fingerprint: str) -> bool:
        """항목 하나와 그것을 가리키는 모든 참조를 지운다. 지웠으면 참."""
        entry = self._entries.pop(fingerprint, None)
        self._negative.discard(fingerprint)
        for document_id in tuple(self._documents):
            tagged = self._documents[document_id]
            tagged.discard(fingerprint)
            if not tagged:
                del self._documents[document_id]
        return entry is not None
