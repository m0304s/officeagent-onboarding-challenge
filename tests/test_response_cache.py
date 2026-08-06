"""캐시 저장소 구현 셋 — 무엇이 맡아 두고, 무엇이 밀려나고, 무엇이 무동작인가.

인메모리 구현이 전체 의미를 내는 덕에 기본 실행이 캐시 히트를 실제로 검증한다. 세 묶음의
내용과 배선에 인메모리 분기가 없어야 하는 이유는 `tests/README.md` 에 있다.
"""

import ast
from pathlib import Path

import pytest

from app.adapters.cache.memory import InMemoryResponseCache
from app.adapters.cache.null import NullResponseCache
from app.adapters.protocols import ResponseCache
from app.core.answers import Answer, FinishReason
from app.core.cache import CachedAnswer, CacheLayer
from app.core.documents import ChunkLocation, DocumentFormat
from app.core.retrieval import ScoredChunk

SCOPE = "scope-1"
NEAR = [1.0, 0.0]
FAR = [0.0, 1.0]


class FakeClock:
    """테스트가 시간을 진행시킨다 — TTL 검증이 실제로 기다리면 스위트가 느려진다."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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


def negative_entry() -> CachedAnswer:
    """근거 0건으로 끝난 항목 — 태그가 없어 부정 집합이 유일한 방어다."""
    return CachedAnswer(answer=Answer.no_evidence(), top_k=5, target_documents=0, sources=())


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cache(clock) -> InMemoryResponseCache:
    return InMemoryResponseCache(ttl_seconds=60.0, max_entries=10, clock=clock)


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


# ── 계약 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "implementation",
    [NullResponseCache(), InMemoryResponseCache(ttl_seconds=1, max_entries=1)],
)
def test_both_implementations_satisfy_the_protocol(implementation):
    """배선이 둘을 같은 자리에 꽂으므로 계약이 갈리면 교체가 런타임에 터진다."""
    assert isinstance(implementation, ResponseCache)


# ── 맡아 두기와 되돌려 주기 ──────────────────────────────────────────────


async def test_stored_entry_comes_back_by_its_fingerprint(cache):
    await store(cache, "fp-1")

    lookup = await cache.lookup_exact("fp-1")

    assert lookup.hit and lookup.layer is CacheLayer.EXACT
    assert lookup.fingerprint == "fp-1"
    assert lookup.entry.answer.text == "연 200만원입니다. [1]"


async def test_unknown_fingerprint_is_a_miss(cache):
    assert not (await cache.lookup_exact("fp-없음")).hit


async def test_semantic_lookup_finds_a_near_enough_entry(cache):
    await store(cache, "fp-1", embedding=[1.0, 0.0])

    lookup = await cache.lookup_semantic(
        [0.99,
        0.05],
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )

    assert lookup.hit and lookup.layer is CacheLayer.SEMANTIC
    assert lookup.fingerprint == "fp-1" and lookup.similarity >= 0.93


async def test_semantic_lookup_below_the_threshold_is_a_miss(cache):
    """임계값 미달이 히트가 되면 이 값이 "얼마나 틀려도 되는가"를 정하지 못한다."""
    await store(cache, "fp-1", embedding=NEAR)

    lookup = await cache.lookup_semantic(
        FAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )

    assert not lookup.hit


async def test_semantic_lookup_on_an_empty_cache_is_a_miss(cache):
    """평가자의 첫 실행이 오류가 되지 않는다."""
    assert not (await cache.lookup_semantic(
        NEAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )).hit


async def test_semantic_lookup_ignores_other_candidate_scopes(cache):
    """프롬프트·색인 세대·모델이 다른 항목이 유사도만으로 히트가 되면 그 셋이 뜻을 잃는다."""
    await store(cache, "fp-other", embedding=NEAR, scope="scope-other")

    lookup = await cache.lookup_semantic(
        NEAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )

    assert not lookup.hit
    assert (await cache.lookup_semantic(
        NEAR,
        scope="scope-other",
        polarity=False,
        threshold=0.93,
        candidates=10,
    )).hit, "자기 집합에서는 여전히 찾힌다"


# ── 수명과 총량 ──────────────────────────────────────────────────────────


async def test_entry_expires_after_its_ttl(cache, clock):
    """TTL 이 지난 항목이 히트로 남으면 무효화가 새는 경로가 하나 더 생긴다."""
    await store(cache, "fp-1")
    clock.advance(61.0)

    assert not (await cache.lookup_exact("fp-1")).hit


async def test_expired_entry_is_not_a_semantic_candidate(cache, clock):
    """만료가 정확 매치에만 적용되면 같은 항목이 유사 매치로 되살아난다."""
    await store(cache, "fp-1", embedding=NEAR)
    clock.advance(61.0)

    lookup = await cache.lookup_semantic(
        NEAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )

    assert not lookup.hit


async def test_capacity_evicts_the_oldest_and_never_rejects_a_store(clock):
    """가득 찼다고 새 답변을 못 남기면 캐시가 시간이 갈수록 쓸모없어진다."""
    cache = InMemoryResponseCache(ttl_seconds=60.0, max_entries=3, clock=clock)

    for index in range(5):
        await store(cache, f"fp-{index}")

    assert not (await cache.lookup_exact("fp-0")).hit
    assert not (await cache.lookup_exact("fp-1")).hit
    for index in (2, 3, 4):
        assert (await cache.lookup_exact(f"fp-{index}")).hit


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


async def test_the_candidate_ceiling_still_bounds_what_is_compared(cache):
    """상한이 스캔 비용을 고정한다 — 가까운 것부터 고르되 개수는 여전히 묶여 있다."""
    for index in range(5):
        await store(cache, f"fp-{index}", embedding=NEAR)

    assert await cache.count_candidates(SCOPE, polarity=False) == 5
    lookup = await cache.lookup_semantic(
        NEAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=1,
    )

    assert lookup.hit, "상한 안의 후보 하나는 여전히 비교된다"


async def test_an_entry_of_the_other_polarity_is_not_a_candidate(cache):
    """부정 한 글자 차이는 코사인이 갈라 주지 못한다 — 축이 하나 더 필요하다."""
    await store(cache, "fp-negated", embedding=NEAR, polarity=True)

    lookup = await cache.lookup_semantic(
        NEAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.93,
        candidates=10,
    )

    assert not lookup.hit
    assert await cache.count_candidates(SCOPE, polarity=False) == 0
    assert await cache.count_candidates(SCOPE, polarity=True) == 1


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


# ── 무효화 ───────────────────────────────────────────────────────────────


async def test_document_invalidation_removes_only_entries_that_cited_it(cache):
    """관계없는 문서를 인용한 항목이 함께 지워지면 업로드 한 번이 캐시를 비운다."""
    await store(cache, "fp-a", sources=(chunk(document_id="doc-a"),))
    await store(cache, "fp-b", sources=(chunk(document_id="doc-b"),))

    removed = await cache.invalidate_document("doc-b")

    assert removed == 1
    assert (await cache.lookup_exact("fp-a")).hit
    assert not (await cache.lookup_exact("fp-b")).hit


async def test_document_invalidation_covers_uncited_sources(cache):
    """인용되지 않은 근거가 낡으면 화면에는 지금 문서에 없는 문장이 출처로 뜬다."""
    await store(cache, "fp-1", sources=(chunk(document_id="doc-a"), chunk(document_id="doc-b")))

    assert await cache.invalidate_document("doc-b") == 1
    assert not (await cache.lookup_exact("fp-1")).hit


async def test_invalidating_an_unknown_document_is_harmless(cache):
    await store(cache, "fp-1")

    assert await cache.invalidate_document("doc-없음") == 0
    assert (await cache.lookup_exact("fp-1")).hit


async def test_negative_invalidation_removes_only_negative_entries(cache):
    """긍정까지 코퍼스 변경으로 지우면 업로드 한 번이 캐시 전체 비우기가 된다."""
    await store(cache, "fp-positive")
    await store(cache, "fp-negative", negative=True, answer=Answer.no_evidence(), sources=())

    removed = await cache.invalidate_negative()

    assert removed == 1
    assert (await cache.lookup_exact("fp-positive")).hit
    assert not (await cache.lookup_exact("fp-negative")).hit


async def test_negative_entry_without_sources_is_still_reachable_by_the_set(cache):
    """근거 0건 항목에는 태그가 없어 부정 집합이 유일한 방어다."""
    await cache.store(
        "fp-1", negative_entry(), scope=SCOPE, polarity=False, embedding=NEAR, negative=True
    )

    assert await cache.invalidate_negative() == 1
    assert not (await cache.lookup_exact("fp-1")).hit


async def test_discard_removes_a_single_entry(cache):
    """재검증에서 버린 항목을 남기면 요청마다 같은 비용을 다시 문다."""
    await store(cache, "fp-1")
    await store(cache, "fp-2")

    await cache.discard("fp-1")

    assert not (await cache.lookup_exact("fp-1")).hit
    assert (await cache.lookup_exact("fp-2")).hit


async def test_discarding_an_unknown_fingerprint_is_harmless(cache):
    await cache.discard("fp-없음")


async def test_reinvalidation_does_not_recount_removed_entries(cache):
    """무효화 개수가 관측값이라, 이미 없는 항목을 세면 로그가 사실과 어긋난다."""
    await store(cache, "fp-1", sources=(chunk(document_id="doc-a"),))

    assert await cache.invalidate_document("doc-a") == 1
    assert await cache.invalidate_document("doc-a") == 0


# ── 꺼진 캐시 ────────────────────────────────────────────────────────────


async def test_disabled_cache_never_returns_what_it_was_given():
    """같은 질문을 두 번 보내도 둘 다 미스여야 한다 (`response-cache`).

    캐시를 끄는 이유가 대개 "배제하고 원인을 보겠다"라, 히트가 나면 배제가 성립하지 않는다."""
    cache = NullResponseCache()

    await cache.store("fp-1", entry(), scope=SCOPE, polarity=False, embedding=NEAR, negative=False)

    assert not (await cache.lookup_exact("fp-1")).hit
    assert not (await cache.lookup_exact("fp-1")).hit
    assert not (await cache.lookup_semantic(
        NEAR,
        scope=SCOPE,
        polarity=False,
        threshold=0.0,
        candidates=10,
    )).hit


async def test_disabled_cache_has_nothing_to_invalidate():
    """무효화가 실패가 아니라 무동작이어야 수집 경로가 캐시 유무를 몰라도 된다."""
    cache = NullResponseCache()

    assert await cache.invalidate_document("doc-a") == 0
    assert await cache.invalidate_negative() == 0
    await cache.discard("fp-1")


def test_disabled_cache_announces_itself_once_at_startup(caplog):
    """꺼짐은 의도된 상태라 요청마다 찍으면 진짜 신호가 묻힌다 (design 결정 12)."""
    with caplog.at_level("INFO", logger="app.adapters.cache.null"):
        NullResponseCache()

    assert len(caplog.records) == 1


# ── 배선의 경계 ──────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SOURCES = sorted((REPO_ROOT / "src" / "app").rglob("*.py"))


def test_production_wiring_has_no_path_to_the_in_memory_cache():
    """프로덕션 경로가 인메모리로 새면 결정 12 가 막으려던 실패가 그대로 돌아온다.

    워커가 둘이면 캐시가 둘이 되고, 무효화가 프로세스 경계를 넘지 못한다."""
    assert PRODUCTION_SOURCES, "소스를 못 찾았다면 이 테스트는 아무것도 지키지 않는다"

    offenders = [
        f"{path.relative_to(REPO_ROOT)}"
        for path in PRODUCTION_SOURCES
        if path.name != "memory.py" and "InMemoryResponseCache" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_in_memory_cache_is_not_imported_by_any_production_module():
    """import 만 남아도 다음 사람이 그것을 배선의 선택지로 읽는다."""
    imported_by = []
    for path in PRODUCTION_SOURCES:
        if path.name == "memory.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = getattr(node, "module", None) if isinstance(node, ast.ImportFrom) else None
            if module and module.endswith("adapters.cache.memory"):
                imported_by.append(str(path.relative_to(REPO_ROOT)))

    assert imported_by == []
