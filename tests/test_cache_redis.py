"""Redis 어댑터 계약 — 키 구조와 정리 규칙이 실물 저장소에서 성립하는가.

기본 실행에서 빠진다(`pytest -m redis`). 여기서만 확인되는 것은 Redis 에서만 존재하는
것들이다 — TTL, 지연 정리, 용량 상한이 페이로드 키까지 지우는가.
"""

import pytest

from app.adapters.cache.redis import RedisConnection, create_client
from app.adapters.cache.store import (
    NEGATIVE_KEY,
    SCOPES_KEY,
    RedisResponseCache,
    candidate_set,
    document_key,
    entry_key,
    index_key,
    vector_key,
    vector_set_key,
)
from app.core.answers import Answer, FinishReason
from app.core.cache import CachedAnswer
from app.core.documents import ChunkLocation, DocumentFormat
from app.core.retrieval import ScoredChunk
from tests.conftest import CACHE_URL, needs_cache

pytestmark = [pytest.mark.redis, needs_cache]

SCOPE = "scope-1"
NEAR = [1.0, 0.0]
FAR = [0.0, 1.0]

#: 극성까지 붙인 후보 집합 이름. 두 색인의 키가 여기서 갈린다.
AFFIRMATIVE = candidate_set(SCOPE, False)
NEGATED = candidate_set(SCOPE, True)


def chunk(**overrides) -> ScoredChunk:
    fields = {
        "document_id": "doc-1",
        "revision": "rev-1",
        "index_signature": "sig-1",
        "chunk_index": 0,
        "text": "교육비는 연 200만원까지 지원됩니다.",
        "location": ChunkLocation(char_start=0, char_end=40),
        "filename": "company-policy.txt",
        "format": DocumentFormat.TXT,
        "score": 0.8,
    }
    return ScoredChunk(**{**fields, **overrides})


def entry(**overrides) -> CachedAnswer:
    fields = {
        "answer": Answer(text="연 200만원입니다. [1]", finish_reason=FinishReason.STOP),
        "top_k": 5,
        "target_documents": 2,
        "sources": (chunk(),),
    }
    return CachedAnswer(**{**fields, **overrides})


@pytest.fixture
async def client():
    """계약 테스트 전용 db 를 비우고 시작한다 — 앞 테스트의 키가 남으면 개수가 흔들린다."""
    connection = create_client(CACHE_URL, timeout_seconds=2.0)
    await connection.flushdb()
    yield connection
    await connection.flushdb()
    await connection.aclose()


@pytest.fixture
async def cache(client):
    connection = RedisConnection(CACHE_URL, timeout_seconds=2.0)
    yield RedisResponseCache(connection, ttl_seconds=60, max_entries=10)
    await connection.aclose()


async def store(
    cache,
    fingerprint,
    *,
    embedding=NEAR,
    scope=SCOPE,
    polarity=False,
    negative=False,
    **overrides,
):
    await cache.store(
        fingerprint,
        entry(**overrides),
        scope=scope,
        polarity=polarity,
        embedding=embedding,
        negative=negative,
    )


# ── 연결 ─────────────────────────────────────────────────────────────────


async def test_connection_reuses_one_client():
    """요청마다 새로 연결하면 핸드셰이크 비용이 캐시가 아끼는 시간을 갉아먹는다."""
    connection = RedisConnection(CACHE_URL, timeout_seconds=2.0)

    assert connection.client() is connection.client()
    await connection.aclose()


async def test_closing_twice_is_harmless():
    """종료 경로가 두 번 불릴 수 있다 — 두 번째가 터지면 재배포가 걸린다."""
    connection = RedisConnection(CACHE_URL, timeout_seconds=2.0)
    connection.client()

    await connection.aclose()
    await connection.aclose()


# ── 키 구조 ──────────────────────────────────────────────────────────────


async def test_store_writes_every_key_of_the_structure(cache, client):
    """페이로드·벡터·순서 인덱스·태그가 한 번의 저장으로 모두 나가야 한다."""
    await store(cache, "fp-1", negative=False)

    assert await client.exists(entry_key("fp-1"))
    assert await client.exists(vector_key("fp-1"))
    assert await client.zscore(index_key(AFFIRMATIVE), "fp-1") is not None
    assert await client.sismember(document_key("doc-1"), "fp-1")
    assert await client.sismember(SCOPES_KEY, AFFIRMATIVE)


async def test_payload_and_vector_carry_a_ttl(cache, client):
    """수명이 없으면 방치된 항목이 영원히 남는다 (`response-cache`)."""
    await store(cache, "fp-1")

    assert 0 < await client.ttl(entry_key("fp-1")) <= 60
    assert 0 < await client.ttl(vector_key("fp-1")) <= 60
    assert 0 < await client.ttl(document_key("doc-1")) <= 60


async def test_negative_entry_joins_the_negative_set(cache, client):
    await store(cache, "fp-1", negative=True, answer=Answer.no_evidence(), sources=())

    assert await client.sismember(NEGATIVE_KEY, "fp-1")
    assert 0 < await client.ttl(NEGATIVE_KEY) <= 60


async def test_order_index_is_a_counter_not_a_clock(cache, client):
    """같은 밀리초의 두 항목 순서가 정해지지 않으면 "가장 최근 N개"가 흔들린다."""
    await store(cache, "fp-1")
    await store(cache, "fp-2")

    first = await client.zscore(index_key(AFFIRMATIVE), "fp-1")
    second = await client.zscore(index_key(AFFIRMATIVE), "fp-2")

    assert second > first


# ── 조회 ─────────────────────────────────────────────────────────────────


async def test_stored_entry_survives_the_storage_roundtrip(cache):
    """저장소를 한 바퀴 돌고 온 항목이 원래 값과 같아야 히트가 미스와 같은 응답을 낸다."""
    original = entry()
    await cache.store(
        "fp-1", original, scope=SCOPE, polarity=False, embedding=NEAR, negative=False
    )

    lookup = await cache.lookup_exact("fp-1")

    assert lookup.hit and lookup.entry == original


async def test_unknown_fingerprint_is_a_miss(cache):
    assert not (await cache.lookup_exact("fp-없음")).hit


async def test_candidate_count_is_zero_before_anything_is_stored(cache):
    """0 이면 호출부가 질의 임베딩을 만들지 않는다 (design 결정 10)."""
    assert await cache.count_candidates(SCOPE, polarity=False) == 0

    await store(cache, "fp-1")

    assert await cache.count_candidates(SCOPE, polarity=False) == 1
    assert await cache.count_candidates("scope-없음", polarity=False) == 0


async def test_semantic_lookup_finds_the_closest_candidate(cache):
    await store(cache, "fp-far", embedding=FAR)
    await store(cache, "fp-near", embedding=NEAR)

    lookup = await cache.lookup_semantic(
        [0.99,
        0.05],
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )

    assert lookup.hit and lookup.fingerprint == "fp-near"


async def test_semantic_lookup_ignores_other_scopes(cache):
    """순서 인덱스가 후보 집합마다 나뉜 것이 L2 의 경계다 (design 결정 2)."""
    await store(cache, "fp-k3", embedding=NEAR, scope="scope-k3")

    assert not (
        await cache.lookup_semantic(
            NEAR,
            scope="scope-k5",
            polarity=False,
            threshold=0.93,
            candidates=10,
        )
    ).hit


async def test_semantic_lookup_below_the_threshold_is_a_miss(cache):
    await store(cache, "fp-1", embedding=NEAR)

    assert not (await cache.lookup_semantic(
        FAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )).hit


async def test_an_old_entry_is_still_reachable_under_the_candidate_ceiling(cache):
    """후보를 저장 순서로 고르면 상한이 곧 히트율의 천장이 된다 — 캐시가 클수록 못 맞힌다."""
    await store(cache, "fp-oldest", embedding=NEAR)
    for index in range(3):
        await store(cache, f"fp-{index}", embedding=FAR)

    lookup = await cache.lookup_semantic(
        NEAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=2,
    )

    assert lookup.hit and lookup.fingerprint == "fp-oldest"


async def test_a_candidate_of_the_other_polarity_is_not_even_a_candidate(cache, client):
    """게이트가 저장 구조로 강제되는지 — 조회 경로가 필터를 잊어도 후보에 없어야 한다."""
    await store(cache, "fp-negated", embedding=NEAR, polarity=True)

    lookup = await cache.lookup_semantic(
        NEAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )

    assert not lookup.hit
    assert await client.vset().vcard(vector_set_key(NEGATED)) == 1
    assert await client.vset().vcard(vector_set_key(AFFIRMATIVE)) == 0


async def test_the_same_polarity_is_still_found(cache):
    """게이트가 자기 극성까지 막으면 유사 매치 층이 통째로 죽는다."""
    await store(cache, "fp-negated", embedding=NEAR, polarity=True)

    lookup = await cache.lookup_semantic(
        [0.99, 0.05],
        scope=SCOPE,
        polarity=True,
        threshold=0.93,
        candidates=10,
    )

    assert lookup.hit and lookup.fingerprint == "fp-negated"


# ── 지연 정리와 용량 ─────────────────────────────────────────────────────


async def test_expired_vector_is_swept_from_the_order_index(cache, client):
    """백그라운드 스위퍼가 없다 — 만료 정리를 다음 조회가 조금씩 나눠 문다 (design 결정 2)."""
    await store(cache, "fp-1", embedding=NEAR)
    await client.delete(vector_key("fp-1"))  # TTL 만료와 같은 상태

    lookup = await cache.lookup_semantic(
        NEAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )

    assert not lookup.hit
    assert await client.zcard(index_key(AFFIRMATIVE)) == 0
    assert await client.vset().vcard(vector_set_key(AFFIRMATIVE)) == 0


async def test_capacity_trims_the_oldest_entries_and_their_keys(client):
    """자르기가 인덱스만 건드리면 페이로드가 상한과 무관하게 쌓인다."""
    connection = RedisConnection(CACHE_URL, timeout_seconds=2.0)
    cache = RedisResponseCache(connection, ttl_seconds=60, max_entries=3)

    for index in range(5):
        await store(cache, f"fp-{index}")

    assert await client.zcard(index_key(AFFIRMATIVE)) == 3
    assert await client.vset().vcard(vector_set_key(AFFIRMATIVE)) == 3
    assert not await client.exists(entry_key("fp-0"))
    assert not await client.exists(vector_key("fp-0"))
    assert await client.exists(entry_key("fp-4"))
    await connection.aclose()


async def test_store_is_never_rejected_when_full(client):
    """가득 찼다고 새 답변을 못 남기면 캐시가 시간이 갈수록 쓸모없어진다."""
    connection = RedisConnection(CACHE_URL, timeout_seconds=2.0)
    cache = RedisResponseCache(connection, ttl_seconds=60, max_entries=2)

    for index in range(4):
        await store(cache, f"fp-{index}")

    assert (await cache.lookup_exact("fp-3")).hit
    await connection.aclose()


# ── 무효화 ───────────────────────────────────────────────────────────────


async def test_document_invalidation_removes_the_entry_and_its_index_slot(cache, client):
    """지문만 지우고 인덱스를 두면 죽은 fp 가 후보 자리를 계속 차지한다."""
    await store(cache, "fp-1", sources=(chunk(document_id="doc-a"),))

    removed = await cache.invalidate_document("doc-a")

    assert removed == 1
    assert not await client.exists(entry_key("fp-1"))
    assert not await client.exists(vector_key("fp-1"))
    assert await client.zcard(index_key(AFFIRMATIVE)) == 0
    assert await client.vset().vcard(vector_set_key(AFFIRMATIVE)) == 0
    assert not await client.exists(document_key("doc-a"))


async def test_document_invalidation_leaves_unrelated_entries(cache):
    """관계없는 문서를 인용한 항목이 함께 지워지면 업로드 한 번이 캐시를 비운다."""
    await store(cache, "fp-a", sources=(chunk(document_id="doc-a"),))
    await store(cache, "fp-b", sources=(chunk(document_id="doc-b"),))

    await cache.invalidate_document("doc-b")

    assert (await cache.lookup_exact("fp-a")).hit
    assert not (await cache.lookup_exact("fp-b")).hit


async def test_negative_invalidation_clears_only_the_negative_set(cache, client):
    await store(cache, "fp-positive")
    await store(cache, "fp-negative", negative=True, answer=Answer.no_evidence(), sources=())

    removed = await cache.invalidate_negative()

    assert removed == 1
    assert (await cache.lookup_exact("fp-positive")).hit
    assert not await client.exists(NEGATIVE_KEY)


async def test_discard_removes_one_entry(cache, client):
    await store(cache, "fp-1")
    await store(cache, "fp-2")

    await cache.discard("fp-1")

    assert not (await cache.lookup_exact("fp-1")).hit
    assert (await cache.lookup_exact("fp-2")).hit
    assert await client.zcard(index_key(AFFIRMATIVE)) == 1
    assert await client.vset().vcard(vector_set_key(AFFIRMATIVE)) == 1


async def test_invalidating_nothing_is_harmless(cache):
    """수집 경로가 캐시에 무엇이 있는지 모른 채 부른다 — 없는 것을 지우는 것이 정상 경로다."""
    assert await cache.invalidate_document("doc-없음") == 0
    assert await cache.invalidate_negative() == 0
    await cache.discard("fp-없음")
