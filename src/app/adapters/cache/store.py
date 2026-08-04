"""Redis 응답 캐시 — 페이로드·벡터·순서 인덱스·태그를 나눠 든다.

키 구조와 그것을 나눈 근거는 `ARCHITECTURE.md` 「응답 캐시」에 있다. 실패는 도메인 예외로
바꿔 올리고 로그를 남기지 않는다 — 요청마다 경고를 찍으면 진짜 신호가 묻힌다.
"""

import asyncio
import functools
import logging
from collections.abc import Iterable, Sequence

from redis.exceptions import RedisError

from app.adapters.cache.codec import dumps_entry, loads_entry, pack_vector, unpack_vector
from app.adapters.cache.redis import RedisConnection
from app.core.cache import CachedAnswer, CacheLookup, best_match
from app.core.exceptions import StorageUnavailable

logger = logging.getLogger(__name__)

#: 세대 표지. 페이로드 구조를 바꿔야 하면 여기를 올리고 옛 키는 TTL 로 보낸다.
KEY_PREFIX = "qa:v1"

NEGATIVE_KEY = f"{KEY_PREFIX}:negative"
SEQUENCE_KEY = f"{KEY_PREFIX}:seq"
#: 살아 있는 후보 집합의 이름들. 무효화가 어느 순서 인덱스에서 fp 를 걷어야 하는지는
#: 지문만 봐서는 알 수 없어, 이 집합이 그 목록을 든다.
SCOPES_KEY = f"{KEY_PREFIX}:scopes"


def entry_key(fingerprint: str) -> str:
    return f"{KEY_PREFIX}:entry:{fingerprint}"


def vector_key(fingerprint: str) -> str:
    return f"{KEY_PREFIX}:vec:{fingerprint}"


def index_key(scope: str) -> str:
    """후보 집합 하나의 순서 인덱스. 집합마다 나뉜 이유는 design 결정 2 에 있다."""
    return f"{KEY_PREFIX}:index:{scope}"


def document_key(document_id: str) -> str:
    return f"{KEY_PREFIX}:doc:{document_id}"


def _storage_errors(method):
    """저장소 예외를 도메인 예외로 바꾼다 — 라이브러리 예외가 서비스로 새지 않게."""

    @functools.wraps(method)
    async def guarded(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except (RedisError, OSError, TimeoutError) as exc:
            raise StorageUnavailable("캐시 저장소에 접근할 수 없습니다") from exc

    return guarded


class RedisResponseCache:
    """`ResponseCache` 의 Redis 구현."""

    def __init__(
        self,
        connection: RedisConnection,
        *,
        ttl_seconds: int,
        max_entries: int,
    ) -> None:
        self._connection = connection
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries

    # ── 조회 ────────────────────────────────────────────────────────────

    @_storage_errors
    async def lookup_exact(self, fingerprint: str) -> CacheLookup:
        raw = await self._client.get(entry_key(fingerprint))
        entry = self._decode(fingerprint, raw)
        if entry is None:
            return CacheLookup.miss()
        return CacheLookup.exact(fingerprint, entry)

    @_storage_errors
    async def count_candidates(self, scope: str) -> int:
        """그 후보 집합의 항목 수. 0 이면 호출부가 질의 임베딩을 만들지 않는다.

        캐시가 빈 상태(평가자의 첫 실행, 대부분의 테스트)에서 임베딩이 한 번만 돈다."""
        return int(await self._client.zcard(index_key(scope)))

    @_storage_errors
    async def lookup_semantic(
        self,
        embedding: Sequence[float],
        *,
        scope: str,
        threshold: float,
        candidates: int,
    ) -> CacheLookup:
        # 최신순 상한개. 오래된 항목은 L2 후보에서 빠지지만 L1 에서는 여전히 찾힌다.
        fingerprints = await self._client.zrange(index_key(scope), 0, candidates - 1, desc=True)
        if not fingerprints:
            return CacheLookup.miss()

        fingerprints = tuple(_text(value) for value in fingerprints)
        vectors = await self._client.mget([vector_key(item) for item in fingerprints])
        await self._sweep(scope, fingerprints, vectors)

        match = await asyncio.to_thread(
            _closest, embedding, fingerprints, vectors, threshold=threshold
        )
        if match is None:
            return CacheLookup.miss()

        # 이긴 하나만 페이로드를 읽는다. 그 사이 만료됐으면 미스다.
        raw = await self._client.get(entry_key(match.fingerprint))
        entry = self._decode(match.fingerprint, raw)
        if entry is None:
            return CacheLookup.miss()
        return CacheLookup.semantic(match.fingerprint, entry, match.similarity)

    # ── 저장 ────────────────────────────────────────────────────────────

    @_storage_errors
    async def store(
        self,
        fingerprint: str,
        entry: CachedAnswer,
        *,
        scope: str,
        embedding: Sequence[float],
        negative: bool,
    ) -> None:
        # 시계가 아니라 카운터로 나이를 매긴다 — 같은 밀리초의 두 항목 순서가 정해지지
        # 않으면 "가장 최근 N개"가 흔들린다 (design 결정 2).
        sequence = await self._client.incr(SEQUENCE_KEY)

        pipeline = self._client.pipeline(transaction=True)
        pipeline.set(entry_key(fingerprint), dumps_entry(entry), ex=self._ttl_seconds)
        pipeline.set(vector_key(fingerprint), pack_vector(embedding), ex=self._ttl_seconds)
        pipeline.zadd(index_key(scope), {fingerprint: sequence})
        self._touch_set(pipeline, SCOPES_KEY, scope)
        for document_id in entry.document_ids:
            self._touch_set(pipeline, document_key(document_id), fingerprint)
        if negative:
            self._touch_set(pipeline, NEGATIVE_KEY, fingerprint)
        pipeline.zcard(index_key(scope))
        results = await pipeline.execute()

        await self._enforce_capacity(scope, int(results[-1]))

    # ── 무효화 ──────────────────────────────────────────────────────────

    @_storage_errors
    async def invalidate_document(self, document_id: str) -> int:
        key = document_key(document_id)
        tagged = await self._client.smembers(key)
        removed = await self._forget(tuple(_text(value) for value in tagged))
        await self._client.delete(key)
        return removed

    @_storage_errors
    async def invalidate_negative(self) -> int:
        tagged = await self._client.smembers(NEGATIVE_KEY)
        removed = await self._forget(tuple(_text(value) for value in tagged))
        await self._client.delete(NEGATIVE_KEY)
        return removed

    @_storage_errors
    async def discard(self, fingerprint: str) -> None:
        await self._forget((fingerprint,))

    # ── 내부 ────────────────────────────────────────────────────────────

    @property
    def _client(self):
        return self._connection.client()

    def _touch_set(self, pipeline, key: str, member: str) -> None:
        """태그 SET 에 지문을 넣고 수명을 갱신한다 — 언젠가 사라지게 둔다."""
        pipeline.sadd(key, member)
        pipeline.expire(key, self._ttl_seconds)

    def _decode(self, fingerprint: str, raw: bytes | None) -> CachedAnswer | None:
        """페이로드를 항목으로. 읽을 수 없으면 미스로 떨어뜨린다.

        여기 경고는 요청마다 나지 않는다 — 정상 경로에서는 도달하지 않는 자리다."""
        if raw is None:
            return None
        try:
            return loads_entry(raw)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "캐시 페이로드를 읽지 못해 미스로 처리합니다",
                extra={"cache_fingerprint": fingerprint[:12]},
                exc_info=exc,
            )
            return None

    async def _sweep(
        self, scope: str, fingerprints: Sequence[str], vectors: Sequence[bytes | None]
    ) -> None:
        """벡터가 만료된 fp 를 순서 인덱스에서 걷어낸다.

        만료 정리를 백그라운드 작업이 아니라 다음 조회가 조금씩 나눠 문다 (지연 정리)."""
        dead = [
            fingerprint
            for fingerprint, vector in zip(fingerprints, vectors, strict=True)
            if vector is None
        ]
        if dead:
            await self._client.zrem(index_key(scope), *dead)

    async def _enforce_capacity(self, scope: str, count: int) -> None:
        """상한을 넘은 만큼 오래된 항목을 자른다. 저장이 거부되는 경로는 없다."""
        excess = count - self._max_entries
        if excess <= 0:
            return

        stale = await self._client.zrange(index_key(scope), 0, excess - 1)
        pipeline = self._client.pipeline(transaction=True)
        pipeline.zremrangebyrank(index_key(scope), 0, excess - 1)
        keys = _payload_keys(_text(value) for value in stale)
        if keys:
            pipeline.delete(*keys)
        await pipeline.execute()

    async def _forget(self, fingerprints: Sequence[str]) -> int:
        """항목들을 지우고 실제로 사라진 개수를 돌려준다.

        태그 SET 에 남는 죽은 지문은 무해하다 — 없는 키를 지우는 것은 아무 일도 아니다."""
        if not fingerprints:
            return 0

        scopes = await self._client.smembers(SCOPES_KEY)
        pipeline = self._client.pipeline(transaction=True)
        pipeline.delete(*[entry_key(item) for item in fingerprints])
        pipeline.delete(*[vector_key(item) for item in fingerprints])
        for scope in scopes:
            pipeline.zrem(index_key(_text(scope)), *fingerprints)
        results = await pipeline.execute()
        return int(results[0])


def _closest(
    embedding: Sequence[float],
    fingerprints: Sequence[str],
    vectors: Sequence[bytes | None],
    *,
    threshold: float,
):
    """후보 벡터를 풀어 가장 가까운 하나를 고른다. CPU 바운드라 스레드에서 돈다."""
    candidates = [
        (fingerprint, unpack_vector(vector))
        for fingerprint, vector in zip(fingerprints, vectors, strict=True)
        if vector is not None
    ]
    return best_match(embedding, candidates, threshold=threshold)


def _payload_keys(fingerprints: Iterable[str]) -> list[str]:
    return [key for item in fingerprints for key in (entry_key(item), vector_key(item))]


def _text(value: bytes | str) -> str:
    """Redis 응답은 바이트다 — 벡터를 바이너리로 받으려고 디코딩을 껐기 때문이다."""
    return value.decode("utf-8") if isinstance(value, bytes) else value
