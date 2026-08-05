"""캐시 오케스트레이션 — 조회 순서, 현재성 재검증, 저장 정책, 축소 동작.

이 층이 정책 전부를 들어, 깨지는 것은 "언제 무엇을 써도 되는가"이지 "무엇이 저장되었는가"가
아니다. 네 묶음의 내용은 `tests/README.md` 에 있다.
"""

from datetime import UTC, datetime

import pytest

from app.core.answers import Answer, FinishReason
from app.core.cache import CachedAnswer, CacheLayer, derive_cache_key
from app.core.documents import ChunkLocation, Document, DocumentFormat, IndexStatus
from app.core.retrieval import ScoredChunk
from app.services.cache import CacheService, CircuitBreaker
from tests.stubs import FakeEmbedder, StubDocumentRegistry, StubResponseCache, SynonymEmbedder

SIGNATURE = "sig-1"
PROMPT_VERSION = "qa-ko-1"
MODEL = "gpt-5-codex"
QUESTION = "교육비 지원 한도가 얼마인가요?"

#: 리랭커를 끈 구성의 서명. 기존 묶음 전부가 이 값 위에서 돌아 리랭킹이 없던 때와
#: 같은 것을 잰다 — 켠 구성은 자기 이름의 서명을 쓴다.
RERANKER_OFF = ""
RERANKER_ON = "fake-reranker/none-v1"

#: 뜻이 같은 두 질의를 한 벡터로 묶는다 — 유사 매치 층을 만드는 유일한 수단이다.
SYNONYM_GROUP = {
    "교육비 지원 한도가 얼마인가요?": "교육비 한도",
    "교육비 얼마까지 지원되나요?": "교육비 한도",
}


def synonym_embedder() -> SynonymEmbedder:
    return SynonymEmbedder(SYNONYM_GROUP)


class FakeClock:
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
        "index_signature": SIGNATURE,
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
        "target_documents": 1,
        "sources": (chunk(),),
    }
    return CachedAnswer(**{**fields, **overrides})


def document(**overrides) -> Document:
    fields = {
        "document_id": "doc-1",
        "filename": "company-policy.txt",
        "format": DocumentFormat.TXT,
        "revision": "rev-1",
        "index_signature": SIGNATURE,
        "chunk_count": 3,
        "byte_size": 100,
        "ingested_at": datetime(2026, 8, 1, tzinfo=UTC),
    }
    return Document(**{**fields, **overrides})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def registry() -> StubDocumentRegistry:
    stub = StubDocumentRegistry()
    stub.documents["doc-1"] = document()
    return stub


@pytest.fixture
def store(clock) -> StubResponseCache:
    return StubResponseCache(clock=clock)


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


def make_service(store, registry, embedder, *, clock=None, **overrides) -> CacheService:
    settings = {
        "prompt_version": PROMPT_VERSION,
        "model": MODEL,
        "semantic_threshold": 0.93,
        "semantic_candidates": 200,
        "operation_timeout_seconds": 0.2,
        "breaker_failures": 3,
        "breaker_cooldown_seconds": 30.0,
    }
    if clock:
        settings["clock"] = clock
    return CacheService(store, registry, embedder, **{**settings, **overrides})


@pytest.fixture
def service(store, registry, embedder, clock) -> CacheService:
    return make_service(store, registry, embedder, clock=clock)


async def lookup(service, question=QUESTION, *, top_k=5, rerank_signature=RERANKER_OFF):
    return await service.lookup(
        question,
        top_k=top_k,
        index_signature=SIGNATURE,
        rerank_signature=rerank_signature,
    )


async def remember(
    service, question=QUESTION, *, top_k=5, item=None, rerank_signature=RERANKER_OFF
):
    """조회 → 저장. 실제 요청이 지나는 순서 그대로 캐시에 항목 하나를 남긴다."""
    slot = await lookup(service, question, top_k=top_k, rerank_signature=rerank_signature)
    await service.store(slot, item or entry(), query=question)
    return slot


# ── 조회 순서 ────────────────────────────────────────────────────────────


async def test_first_lookup_is_a_miss(service):
    slot = await lookup(service)

    assert not slot.hit and not slot.degraded
    assert slot.lookup.layer is None


async def test_second_lookup_of_the_same_question_is_an_exact_hit(service):
    await remember(service)

    slot = await lookup(service)

    assert slot.hit and slot.lookup.layer is CacheLayer.EXACT
    assert slot.entry.answer.text == "연 200만원입니다. [1]"


async def test_exact_hit_does_not_embed_the_query(service, embedder):
    """정확 매치가 유사 매치보다 앞이며 더 싸다 (`response-cache`).

    임베딩을 만들었는지는 응답 어디에도 드러나지 않아 호출 기록으로만 관측된다."""
    await remember(service)
    embedder.queries.clear()

    slot = await lookup(service)

    assert slot.hit and embedder.queries == []


async def test_empty_candidate_set_skips_the_embedding(service, embedder, store):
    """캐시가 빈 상태(평가자의 첫 실행)에서 임베딩이 한 번만 돌아야 한다 (design 결정 10)."""
    await lookup(service)

    assert embedder.queries == []
    assert "lookup_semantic" not in store.calls


async def test_similar_question_hits_the_semantic_layer(store, registry, clock):
    """정확 매치가 실패해도 뜻이 충분히 가까우면 캐시로 답한다 (`response-cache`)."""
    service = make_service(store, registry, synonym_embedder(), clock=clock)
    await remember(service, "교육비 지원 한도가 얼마인가요?")

    slot = await lookup(service, "교육비 얼마까지 지원되나요?")

    assert slot.hit and slot.lookup.layer is CacheLayer.SEMANTIC
    assert slot.lookup.similarity == pytest.approx(1.0)


async def test_semantic_hit_is_revalidated_like_an_exact_hit(store, registry, clock):
    """두 층 중 하나만 재검증하면 낡은 답변이 다른 문으로 나간다."""
    service = make_service(store, registry, synonym_embedder(), clock=clock)
    await remember(service, "교육비 지원 한도가 얼마인가요?")
    registry.documents["doc-1"] = document(revision="rev-2")

    slot = await lookup(service, "교육비 얼마까지 지원되나요?")

    assert not slot.hit


async def test_different_question_below_the_threshold_is_a_miss(service):
    """묻는 바가 다른 질문에 캐시된 답변이 나가면 임계값이 뜻을 잃는다."""
    await remember(service, "교육비 지원 한도가 얼마인가요?")

    slot = await lookup(service, "재택근무는 주 며칠까지 가능한가요?")

    assert not slot.hit


async def test_a_different_top_k_does_not_hit(service):
    """K 가 다르면 근거 개수가 달라 답변이 달라진다 — 다른 항목이어야 한다."""
    await remember(service, top_k=3)

    assert not (await lookup(service, top_k=5)).hit


async def test_a_different_top_k_is_unreachable_by_similarity(store, registry, clock):
    """L1 에서 갈린 것이 L2 에서 도로 합쳐지면 후보 집합의 경계가 없는 것이다."""
    service = make_service(store, registry, synonym_embedder(), clock=clock)
    await remember(service, "교육비 지원 한도가 얼마인가요?", top_k=3)

    slot = await lookup(service, "교육비 얼마까지 지원되나요?")

    assert not slot.hit


async def test_a_different_index_signature_does_not_hit(service):
    """청킹·임베딩 구성이 달라지면 근거가 달라진다."""
    await remember(service)

    reindexed = await service.lookup(
        QUESTION, top_k=5, index_signature="sig-2", rerank_signature=RERANKER_OFF
    )

    assert not reindexed.hit


async def test_a_different_prompt_version_does_not_hit(store, registry, embedder):
    """프롬프트를 고쳤는데 옛 프롬프트의 답이 나오면 개선이 캐시에 가려 관측되지 않는다."""
    await remember(make_service(store, registry, embedder))

    revised = make_service(store, registry, embedder, prompt_version="qa-ko-2")

    assert not (await lookup(revised)).hit


async def test_a_different_model_does_not_hit(store, registry, embedder):
    await remember(make_service(store, registry, embedder))

    revised = make_service(store, registry, embedder, model="gpt-5")

    assert not (await lookup(revised)).hit


async def test_turning_the_reranker_on_does_not_hit_the_entry_made_without_it(service):
    """리랭킹은 상위 K 에 들어오는 청크를 바꾼다 — 켠 배포가 끈 배포의 답을 쓰면
    응답의 `sources` 가 그 답변이 실제로 본 근거가 아니게 된다."""
    await remember(service, rerank_signature=RERANKER_OFF)

    assert not (await lookup(service, rerank_signature=RERANKER_ON)).hit


async def test_turning_the_reranker_off_does_not_hit_the_entry_made_with_it(service):
    """되돌리기가 설정 한 줄이라 이 방향이 실제로 일어난다 (design Migration Plan)."""
    await remember(service, rerank_signature=RERANKER_ON)

    assert not (await lookup(service, rerank_signature=RERANKER_OFF)).hit


async def test_a_different_reranker_model_does_not_hit(service):
    """서명에 모델 이름이 들어 있어야 갈린다 — 켜짐/꺼짐 두 상태만으로는 부족하다."""
    await remember(service, rerank_signature=RERANKER_ON)

    assert not (await lookup(service, rerank_signature="other/reranker@abc/sigmoid-v1")).hit


async def test_the_reranker_also_splits_the_semantic_candidate_set(store, registry, clock):
    """L1 에서 갈린 것이 L2 에서 도로 합쳐지면 리랭커 몫이 키에만 있고 뜻은 없다."""
    service = make_service(store, registry, synonym_embedder(), clock=clock)
    await remember(service, "교육비 지원 한도가 얼마인가요?", rerank_signature=RERANKER_OFF)

    slot = await lookup(service, "교육비 얼마까지 지원되나요?", rerank_signature=RERANKER_ON)

    assert not slot.hit


# ── 현재성 재검증 ────────────────────────────────────────────────────────


async def test_entry_citing_an_old_revision_is_not_used(service, registry):
    """무효화가 새도 낡은 답변이 나가지 않는다 (`response-cache`)."""
    await remember(service)
    registry.documents["doc-1"] = document(revision="rev-2")

    slot = await lookup(service)

    assert not slot.hit


async def test_entry_citing_a_deleted_document_is_not_used(service, registry):
    await remember(service)
    del registry.documents["doc-1"]

    assert not (await lookup(service)).hit


async def test_entry_citing_a_stale_document_is_not_used(service, registry):
    """`stale` 은 리비전과 서명을 그대로 두므로 그 둘만 봐서는 걸러지지 않는다."""
    await remember(service)
    registry.documents["doc-1"] = document(index_status=IndexStatus.STALE)

    assert not (await lookup(service)).hit


async def test_a_rejected_entry_is_removed_from_the_cache(service, registry, store):
    """남기면 같은 항목이 요청마다 재검증 비용을 다시 물린다 (design 결정 7)."""
    await remember(service)
    registry.documents["doc-1"] = document(revision="rev-2")

    await lookup(service)

    assert "discard" in store.calls
    fingerprint = derive_cache_key(
        query=QUESTION,
        top_k=5,
        prompt_version=PROMPT_VERSION,
        index_signature=SIGNATURE,
        model=MODEL,
        rerank_signature=RERANKER_OFF,
    )
    assert not (await store.lookup_exact(fingerprint)).hit


async def test_revalidation_reads_only_the_documents_that_contributed(service, registry):
    """비용이 문서 수십 건이 아니라 인용 문서 몇 건이어야 재검증이 히트보다 싸다."""
    registry.documents["doc-2"] = document(document_id="doc-2", revision="rev-9")
    await remember(service)
    registry.before_get = None

    assert (await lookup(service)).hit, "관계없는 문서의 리비전이 히트를 막으면 안 된다"


async def test_a_no_evidence_entry_has_nothing_to_revalidate(service, registry):
    """근거 0건 항목에는 참조한 문서가 없다 — 재검증이 아니라 부정 집합이 방어선이다."""
    negative = CachedAnswer(answer=Answer.no_evidence(), top_k=5, target_documents=0)
    await remember(service, item=negative)
    del registry.documents["doc-1"]

    slot = await lookup(service)

    assert slot.hit and slot.entry.answer.finish_reason is FinishReason.NO_EVIDENCE


# ── 저장 정책 ────────────────────────────────────────────────────────────


async def test_stored_entry_is_tagged_with_every_source_document(service, store):
    """인용되지 않은 근거가 낡아도 화면에는 출처로 뜬다 — 태그는 근거 전부다."""
    item = entry(sources=(chunk(document_id="doc-1"), chunk(document_id="doc-2")))
    await remember(service, item=item)

    assert await store.invalidate_document("doc-2") == 1


async def test_no_evidence_joins_the_negative_set(service, store):
    """근거 0건 판정은 문서 태그가 닿지 않아 부정 집합이 유일한 방어다 (결정 4)."""
    negative = CachedAnswer(answer=Answer.no_evidence(), top_k=5, target_documents=0)
    await remember(service, item=negative)

    assert await store.invalidate_negative() == 1


async def test_insufficient_evidence_gets_both_defences(service, store):
    """자기가 본 청크가 바뀌면 태그가, 코퍼스에 내용이 더해지면 집합이 잡는다.

    두 기제가 다른 사건을 덮으므로 중복이 아니다 (design 결정 4)."""
    item = CachedAnswer(
        answer=Answer(
            text="문서에 답이 없습니다", finish_reason=FinishReason.INSUFFICIENT_EVIDENCE
        ),
        top_k=5,
        target_documents=1,
        sources=(chunk(),),
    )
    await remember(service, item=item)

    assert await store.invalidate_document("doc-1") == 1

    await remember(service, item=item)

    assert await store.invalidate_negative() == 1


async def test_a_positive_answer_stays_out_of_the_negative_set(service, store):
    """긍정까지 코퍼스 변경으로 지우면 업로드 한 번이 캐시 전체 비우기가 된다."""
    await remember(service)

    assert await store.invalidate_negative() == 0
    assert (await lookup(service)).hit


async def test_storing_after_an_empty_candidate_scan_still_embeds(service, store, embedder):
    """조회가 임베딩을 건너뛴 경우다 — 벡터 없이 저장하면 그 항목은 L2 에 영영 안 보인다."""
    slot = await lookup(service)
    assert slot.embedding is None

    await service.store(slot, entry(), query=QUESTION)

    assert embedder.queries == [QUESTION]
    assert "store" in store.calls


# ── 장애 아래에서 ────────────────────────────────────────────────────────


async def test_a_dead_store_degrades_to_a_miss(registry, embedder, clock):
    """캐시에 닿지 못해도 질의응답은 성립한다 (`response-cache`)."""
    service = make_service(StubResponseCache(fail=True), registry, embedder, clock=clock)

    slot = await lookup(service)

    assert not slot.hit and slot.degraded


async def test_a_dead_store_does_not_fail_the_store_call(registry, embedder, clock):
    """저장 실패가 응답을 깨면 캐시가 답변의 성립 조건이 된다."""
    service = make_service(StubResponseCache(fail=True), registry, embedder, clock=clock)
    slot = await lookup(service)

    await service.store(slot, entry(), query=QUESTION)


async def test_a_hanging_store_misses_within_the_timeout(registry, embedder, clock):
    """상한이 없으면 죽은 저장소가 "미스"가 아니라 "느린 미스"가 된다 (design 결정 13)."""
    store = StubResponseCache(delay=5.0)
    service = make_service(store, registry, embedder, clock=clock, operation_timeout_seconds=0.05)

    slot = await lookup(service)

    assert not slot.hit and slot.degraded


async def test_the_breaker_stops_calling_a_dead_store(registry, embedder, clock):
    """타임아웃만 두면 죽은 저장소에 매 요청 상한만큼을 계속 잃는다."""
    store = StubResponseCache(fail=True)
    service = make_service(store, registry, embedder, clock=clock, breaker_failures=3)

    for _ in range(3):
        await lookup(service)
    store.calls.clear()

    slot = await lookup(service)

    assert not slot.hit and slot.degraded
    assert store.calls == [], "차단이 열린 뒤에는 저장소를 아예 부르지 않아야 한다"


async def test_the_breaker_reopens_after_the_cooldown_without_a_restart(registry, embedder, clock):
    """회복에 재기동이나 수동 개입이 필요하지 않다 (`response-cache`)."""
    store = StubResponseCache(fail=True, clock=clock)
    service = make_service(store, registry, embedder, clock=clock, breaker_cooldown_seconds=30.0)
    for _ in range(3):
        await lookup(service)

    store.fail = False
    clock.advance(31.0)
    await remember(service)

    slot = await lookup(service)

    assert slot.hit, "쿨다운 뒤 첫 요청이 탐침이 되어 차단이 풀려야 한다"


async def test_the_breaker_stays_open_while_the_store_is_still_dead(registry, embedder, clock):
    """쿨다운이 지나 보낸 탐침이 실패하면 다시 닫혀야 한다 — 아니면 매 요청이 탐침이다."""
    store = StubResponseCache(fail=True)
    service = make_service(store, registry, embedder, clock=clock)
    for _ in range(3):
        await lookup(service)

    clock.advance(31.0)
    await lookup(service)  # 탐침 — 실패한다
    store.calls.clear()

    await lookup(service)

    assert store.calls == []


async def test_a_broken_registry_does_not_serve_a_stale_hit(service, registry):
    """재검증을 못 했으면 히트로 쓰지 않는다 — 확인하지 못한 것과 확인된 것은 다르다."""
    await remember(service)

    async def explode() -> None:
        raise RuntimeError("주입된 레지스트리 장애")

    registry.before_get = explode

    assert not (await lookup(service)).hit


# ── 차단기 자체 ──────────────────────────────────────────────────────────


def test_breaker_opens_only_after_consecutive_failures(clock):
    breaker = CircuitBreaker(failures=3, cooldown_seconds=30.0, clock=clock)

    assert breaker.record_failure() is False
    assert breaker.record_failure() is False
    assert breaker.record_failure() is True
    assert breaker.is_open and not breaker.allows()


def test_breaker_forgets_failures_after_a_success(clock):
    """연속이 아니면 열리지 않는다 — 간헐적 실패로 캐시가 30초씩 꺼지면 손해다."""
    breaker = CircuitBreaker(failures=3, cooldown_seconds=30.0, clock=clock)
    breaker.record_failure()
    breaker.record_failure()

    breaker.record_success()

    assert breaker.record_failure() is False
    assert not breaker.is_open


def test_breaker_reports_only_transitions(clock):
    """전이 시점에만 로그를 남기려면 전이 자체가 반환값이어야 한다 (design 결정 12)."""
    breaker = CircuitBreaker(failures=1, cooldown_seconds=30.0, clock=clock)

    assert breaker.record_failure() is True
    assert breaker.record_failure() is False
    assert breaker.record_success() is True
    assert breaker.record_success() is False


def test_breaker_lets_one_probe_through_after_the_cooldown(clock):
    breaker = CircuitBreaker(failures=1, cooldown_seconds=30.0, clock=clock)
    breaker.record_failure()

    clock.advance(29.0)
    assert not breaker.allows()

    clock.advance(2.0)
    assert breaker.allows()
