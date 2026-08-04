"""검색 서비스 — 순서와 판정. 무엇을 언제 부르고 무엇을 대상으로 삼는가.

페이크 임베더가 결정론적이라 질의를 청크 본문과 똑같이 두면 그 청크의 1위가 확정된다 —
그 성질로 정렬 방향과 필터를 의미 없이도 결정적으로 잰다.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import pytest

from app.core.documents import IndexStatus
from app.core.exceptions import StorageUnavailable
from tests.retrieval_harness import GUIDE, POLICY, SHORT, make_harness
from tests.stubs import FakeReranker, StubLexicalIndex, StubVectorStore

#: 하한 테스트가 공유하는 질의. 하한을 점수 분포에서 유도하므로 세 테스트가 같은
#: 질의를 봐야 유도한 값이 그대로 쓰인다.
FLOOR_QUERY = "교육비 지원"

#: 양쪽 retriever 를 켠 구성. 이름 목록이 그대로 기여 목록의 기대값이 된다.
HYBRID = ("dense", "lexical")

# ── 임베딩 경로 (5.7) ────────────────────────────────────────────────────


async def test_a_search_uses_the_query_embedding_path_exactly_once():
    """경로를 바꿔 써도 결과 형식은 멀쩡해 호출 기록이 아니면 검출되지 않는다.

    역할에 따라 입력을 다르게 다루는 모델에서는 점수 분포가 통째로 이동한다."""
    harness = make_harness()
    await harness.ingest("policy.txt", POLICY)
    batches_after_ingestion = len(harness.embedder.batches)

    await harness.retrieval.search("교육비 지원 한도")

    assert harness.embedder.queries == ["교육비 지원 한도"]
    assert len(harness.embedder.batches) == batches_after_ingestion, (
        "질의를 문서용 경로로 인코딩했다"
    )


# ── 대상 집합 (5.8) ──────────────────────────────────────────────────────


async def test_no_documents_means_no_store_query_at_all():
    """대상이 없으면 저장소 왕복 비용조차 만들지 않는다."""
    harness = make_harness()

    result = await harness.retrieval.search("교육비")

    assert result.count == 0
    assert result.target_documents == 0
    assert harness.store.queries == [], "대상이 없는데 저장소에 질의가 갔다"


async def test_a_deleted_document_disappears_from_results():
    harness = make_harness()
    policy = await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)
    before = await harness.retrieval.search("승인")
    assert {chunk.document_id for chunk in before.chunks} >= {policy.document_id}

    await harness.ingestion.delete(policy.document_id)
    after = await harness.retrieval.search("승인")

    assert all(chunk.document_id != policy.document_id for chunk in after.chunks)
    assert after.count > 0, "남은 문서까지 사라졌다"


async def test_leftover_chunks_from_a_previous_revision_are_not_searchable():
    """이전 세대 정리가 실패해 저장소에 남아도 검색되지 않는다.

    잔여 청크가 무해하다는 근거가 이 필터 하나뿐이다."""
    # 정리 실패를 주입한다 — 교체 자체는 성립하고 이전 리비전 청크만 저장소에 남는다.
    harness = make_harness(vector_store=_store_that_cannot_delete())
    first = await harness.ingest("policy.txt", POLICY)
    second = await harness.ingest("policy.txt", POLICY + "야근 식대는 1만원까지 지원합니다.\n")
    assert first.revision != second.revision
    stored = {record["chunk"].revision for record in harness.store.records.values()}
    assert first.revision in stored, "정리 실패를 주입했는데 이전 청크가 남지 않았다"

    result = await harness.retrieval.search("교육비")

    assert result.count > 0
    assert all(chunk.revision == second.revision for chunk in result.chunks)


async def test_a_stale_document_is_not_searchable():
    """색인 구성이 바뀌어 `stale` 이 된 문서는 대상에서 빠진다."""
    harness = make_harness()
    policy = await harness.ingest("policy.txt", POLICY)
    await harness.registry.commit(
        replace(harness.registry.documents[policy.document_id], index_status=IndexStatus.STALE)
    )

    result = await harness.retrieval.search("교육비")

    assert result.count == 0
    assert result.target_documents == 0


async def test_a_document_indexed_under_another_signature_is_not_searchable():
    """서명 축이 빠지면 구 구성으로 만든 벡터가 같은 공간에 있는 척한다."""
    harness = make_harness()
    policy = await harness.ingest("policy.txt", POLICY)
    await harness.registry.commit(
        replace(harness.registry.documents[policy.document_id], index_signature="other-signature")
    )

    result = await harness.retrieval.search("교육비")

    assert result.count == 0
    assert harness.store.queries == []


# ── 동시 변경 (5.9) ──────────────────────────────────────────────────────


async def test_a_replacement_committed_after_the_target_set_does_not_leak_the_old_revision():
    """대상 집합 확정과 저장소 질의 사이는 잠겨 있지 않다.

    낡은 `revision` 이 실려 나가면 캐시가 그 값으로 만든 항목이 무효화에 걸리지 않는다."""
    harness = make_harness()
    first = await harness.ingest("policy.txt", POLICY)

    async def replace_after_target_set() -> None:
        await harness.ingest("policy.txt", POLICY + "야근 식대는 1만원까지 지원합니다.\n")

    harness.registry.after_list_all = replace_after_target_set
    result = await harness.retrieval.search("교육비")

    assert all(chunk.revision != first.revision for chunk in result.chunks)
    current = harness.registry.documents[first.document_id].revision
    assert all(chunk.revision == current for chunk in result.chunks)


async def test_a_deletion_completed_after_the_store_query_removes_that_document():
    """질의를 마친 뒤 삭제가 완료되면 그 문서는 이번 검색에 아무것도 기여하지 않는다."""
    harness = make_harness()
    policy = await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)

    async def delete_after_store_query() -> None:
        harness.registry.documents.pop(policy.document_id)

    harness.registry.before_get = delete_after_store_query
    result = await harness.retrieval.search("승인")

    assert all(chunk.document_id != policy.document_id for chunk in result.chunks)


async def test_results_of_one_document_never_mix_two_revisions():
    """재업로드가 진행되는 동안에도 한 응답에 두 리비전이 섞이지 않는다."""
    harness = make_harness(vector_store=_store_that_cannot_delete())
    policy = await harness.ingest("policy.txt", POLICY)

    async def replace_after_target_set() -> None:
        await harness.ingest("policy.txt", POLICY + "야근 식대는 1만원까지 지원합니다.\n")

    harness.registry.after_list_all = replace_after_target_set
    result = await harness.retrieval.search("교육비")

    revisions = {
        chunk.revision for chunk in result.chunks if chunk.document_id == policy.document_id
    }
    assert len(revisions) <= 1


# ── 순서와 K (5.10) ──────────────────────────────────────────────────────


async def test_results_are_sorted_by_descending_score():
    harness = make_harness()
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)

    result = await harness.retrieval.search("교육비 지원")

    scores = [chunk.score for chunk in result.chunks]
    assert scores == sorted(scores, reverse=True)
    assert all(0 < score <= 1 for score in scores)


async def test_the_chunk_whose_text_matches_the_query_ranks_first():
    """페이크 벡터에는 의미가 없지만 결정론적이다 — 같은 문자열이면 벡터가 일치한다."""
    harness = make_harness()
    policy = await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)
    target = harness.chunk_text(policy.document_id, 2)

    result = await harness.retrieval.search(target)

    assert result.chunks[0].text == target
    assert result.chunks[0].document_id == policy.document_id


async def test_a_requested_top_k_beats_the_configured_default():
    harness = make_harness(top_k=5)
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)
    assert len(harness.store.records) > 2, "K 를 좁히는 단언이 성립하려면 청크가 더 많아야 한다"

    result = await harness.retrieval.search("교육비", top_k=2)

    assert result.top_k == 2
    assert result.count == 2


async def test_fewer_targets_than_k_returns_only_what_exists():
    """모자란 자리를 채우지도, 오류로 만들지도 않는다."""
    harness = make_harness(top_k=5)
    await harness.ingest("short.txt", SHORT)
    stored = len(harness.store.records)
    assert stored < 5

    result = await harness.retrieval.search("재택근무")

    assert result.count == stored


async def test_the_same_query_twice_gives_the_same_results():
    """결정성은 다음 change 의 캐싱이 성립하기 위한 전제다."""
    harness = make_harness()
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)

    first = await harness.retrieval.search("교육비 지원 한도")
    second = await harness.retrieval.search("교육비 지원 한도")

    assert [(c.document_id, c.chunk_index, c.score) for c in first.chunks] == [
        (c.document_id, c.chunk_index, c.score) for c in second.chunks
    ]


# ── 유사도 하한 (5.11) ───────────────────────────────────────────────────


async def test_every_returned_result_clears_the_floor():
    harness = make_harness()
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)
    floor = await _floor_between_the_top_two(harness)

    result = await harness.searching_with(min_score=floor).search(FLOOR_QUERY)

    assert 0 < result.count, "하한이 전부를 걸러 단언이 공허해졌다"
    assert all(chunk.contributions[0].native_score >= floor for chunk in result.chunks)


async def test_a_floor_of_one_empties_the_results_without_an_error():
    """하한에 걸려 아무것도 남지 않는 것은 오류가 아니다 — 검색은 거절 문구를 만들지 않는다."""
    harness = make_harness()
    await harness.ingest("policy.txt", POLICY)

    result = await harness.searching_with(min_score=1.0).search("교육비 지원")

    assert result.count == 0
    assert result.chunks == ()
    assert result.target_documents == 1, "하한이 대상 집합까지 지우면 안 된다"


async def test_lowering_the_floor_only_adds_results():
    harness = make_harness()
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)
    floor = await _floor_between_the_top_two(harness)

    strict = await harness.searching_with(min_score=floor).search(FLOOR_QUERY)
    lenient = await harness.searching_with(min_score=0.0).search(FLOOR_QUERY)

    assert 0 < strict.count < lenient.count, "하한이 아무것도 가르지 못했다"
    strict_ids = {(c.document_id, c.chunk_index) for c in strict.chunks}
    lenient_ids = {(c.document_id, c.chunk_index) for c in lenient.chunks}
    assert strict_ids <= lenient_ids


async def _floor_between_the_top_two(harness) -> float:
    """1위와 2위의 밀집 원점수 사이의 하한.

    응답의 `score` 는 척도가 달라 못 쓰고, 상수로 박으면 단언이 공허해진다."""
    everything = await harness.searching_with(min_score=0.0).search(FLOOR_QUERY)
    assert everything.count >= 2
    scores = [chunk.contributions[0].native_score for chunk in everything.chunks]
    return (scores[0] + scores[1]) / 2


# ── 팬아웃과 융합 (6.4) ──────────────────────────────────────────────────


async def test_both_retrievers_contribute_to_one_search():
    """양쪽이 찾아낸 청크는 내역이 둘이고, 응답의 기여 목록에 두 이름이 모두 있다."""
    harness = make_harness(retrievers=HYBRID)
    await harness.ingest("policy.txt", POLICY)
    target = harness.chunk_text(next(iter(harness.registry.documents)), 1)

    result = await harness.retrieval.search(target)

    assert result.retrievers == HYBRID
    both = [chunk for chunk in result.chunks if len(chunk.contributions) == 2]
    assert both, "양쪽이 같은 청크를 찾은 적이 없어 단언이 공허해졌다"
    for chunk in both:
        assert {credit.retriever for credit in chunk.contributions} == set(HYBRID)
        assert all(credit.rank >= 1 for credit in chunk.contributions)


async def test_every_result_carries_at_least_one_contribution():
    harness = make_harness(retrievers=HYBRID)
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)

    result = await harness.retrieval.search("교육비 지원 한도")

    assert result.count > 1
    assert all(chunk.contributions for chunk in result.chunks)


async def test_a_single_retriever_configuration_preserves_its_own_order():
    """융합이 순서를 보존한다는 것이 회귀 판정의 기준선이다.

    기준선이 없으면 하이브리드가 "좋아졌다"고 말할 수 없다 — 무엇과 비교할지가 없다."""
    harness = make_harness(retrievers=HYBRID)
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)
    document_id = next(iter(harness.registry.documents))
    query = harness.chunk_text(document_id, 0)

    dense_only = await harness.searching_with(retrievers=("dense",)).search(query)
    direct = await harness.store.query(
        await harness.embedder.embed_query(query),
        top_k=dense_only.top_k,
        versions=await harness.store.list_stored_versions(),
    )

    assert [(chunk.document_id, chunk.chunk_index) for chunk in dense_only.chunks] == [
        (chunk.document_id, chunk.chunk_index) for chunk in direct
    ]


async def test_every_retriever_sees_the_very_same_target_list():
    """대상 집합을 각자 계산하면 융합 결과에 두 세대가 섞인다."""
    harness = make_harness(retrievers=HYBRID)
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)

    await harness.retrieval.search("교육비")

    dense_targets = harness.store.queries[-1][1]
    lexical_targets = harness.lexical.searches[-1][2]
    searchable = {
        (document.document_id, document.revision, document.index_signature)
        for document in harness.registry.documents.values()
    }
    assert dense_targets == lexical_targets
    assert {(v.document_id, v.revision, v.index_signature) for v in dense_targets} == searchable


async def test_no_targets_means_no_retriever_is_called_at_all():
    harness = make_harness(retrievers=HYBRID)

    result = await harness.retrieval.search("교육비")

    assert result.count == 0
    assert result.retrievers == ()
    assert harness.store.queries == []
    assert harness.lexical.searches == []


async def test_a_lexical_only_configuration_never_embeds_the_query():
    """어휘 retriever 가 임베딩에 의존하면 두 목록이 같은 결함을 공유한다."""
    harness = make_harness(retrievers=("lexical",), required=("lexical",))
    await harness.ingest("guide.md", GUIDE)
    batches_after_ingestion = len(harness.embedder.batches)

    result = await harness.retrieval.search("P1")

    assert result.count > 0
    assert result.retrievers == ("lexical",)
    assert harness.embedder.queries == []
    assert len(harness.embedder.batches) == batches_after_ingestion


# ── 부분 실패의 세 처분 (6.4) ────────────────────────────────────────────


async def test_a_required_retriever_failure_becomes_a_storage_error():
    """빈 결과로 위장하면 벡터 스토어가 죽은 동안 "근거를 찾지 못했습니다"가 나간다."""
    harness = make_harness(
        vector_store=StubVectorStore(fail_query=True), retrievers=HYBRID, required=("dense",)
    )
    await harness.ingest("guide.md", GUIDE)

    with pytest.raises(StorageUnavailable):
        await harness.retrieval.search("P1")


async def test_an_optional_retriever_failure_proceeds_with_the_rest(caplog):
    """넘기되 응답과 로그에 드러낸다 — 기여 목록에서 이름이 빠진 것이 곧 신호다."""
    harness = make_harness(
        lexical_index=StubLexicalIndex(fail_search=True), retrievers=HYBRID, required=("dense",)
    )
    await harness.ingest("guide.md", GUIDE)

    with caplog.at_level("WARNING"):
        result = await harness.retrieval.search("P1 장애")

    assert result.count > 0
    assert result.retrievers == ("dense",)
    credited = {credit.retriever for chunk in result.chunks for credit in chunk.contributions}
    assert credited == {"dense"}
    assert any(record.levelname == "WARNING" for record in caplog.records)


async def test_every_retriever_failing_is_a_storage_error_even_when_all_are_optional():
    harness = make_harness(
        vector_store=StubVectorStore(fail_query=True),
        lexical_index=StubLexicalIndex(fail_search=True),
        retrievers=HYBRID,
        required=(),
    )
    await harness.ingest("guide.md", GUIDE)

    with pytest.raises(StorageUnavailable):
        await harness.retrieval.search("P1")


async def test_an_optional_failure_does_not_squash_the_score_scale():
    """실패한 목록은 분모에서도 빠진다 — 빈 목록으로 넘기면 모든 점수가 절반이 된다.

    하한이 걸러 비운 목록은 판정이라 척도에 남고, 실패한 retriever 는 판정한 적이 없다."""
    broken = make_harness(
        lexical_index=StubLexicalIndex(fail_search=True), retrievers=HYBRID, required=("dense",)
    )
    await broken.ingest("guide.md", GUIDE)

    failing = await broken.retrieval.search("P1 장애")
    unconfigured = await broken.searching_with(retrievers=("dense",)).search("P1 장애")

    assert failing.top_score == unconfigured.top_score == 1.0


def _store_that_cannot_delete():
    """이전 세대 정리가 실패하는 저장소.

    교체 자체는 성립하고 이전 리비전 청크만 남는다 — 수집이 허용한다고 정해 둔 상태다."""
    from tests.stubs import StubVectorStore

    return StubVectorStore(fail_delete=True)


# ── 리랭킹 (4.8~4.11) ────────────────────────────────────────────────────

#: 두 문서 모두에서 후보가 나오는 질의. 리랭킹 대상이 한 문서로 쏠리면 순서 단언이
#: 융합과 리랭킹 중 무엇을 봤는지 갈리지 않는다.
RERANK_QUERY = "교육비 지원"


def _identities(result) -> list[tuple[str, int]]:
    return [(chunk.document_id, chunk.chunk_index) for chunk in result.chunks]


def _ranking_by_text(chunks, order: Sequence[int]) -> dict[str, float]:
    """본문 → 점수. `order` 가 앞에 세울 순서라 그대로 뒤집거나 섞을 수 있다."""
    return {chunk.text: float(len(order) - place) for place, chunk in enumerate(order)}


async def _fused_baseline(harness):
    """리랭커를 끈 같은 구성의 결과. 순서·집합 비교의 기준선이다."""
    return await harness.retrieval.search(RERANK_QUERY)


async def _harness_with_two_documents():
    harness = make_harness()
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)
    return harness


async def test_reranking_reorders_the_results_without_changing_the_set():
    """순서를 뒤집는 리랭커에서 순서만 바뀐다 — 빠진 청크도 더해진 청크도 없다."""
    harness = await _harness_with_two_documents()
    fused = await _fused_baseline(harness)
    assert fused.count > 1, "결과가 하나면 순서 단언이 공허해진다"
    scores = {chunk.text: float(place) for place, chunk in enumerate(fused.chunks)}
    reranker = FakeReranker(scorer=lambda query, text: scores.get(text, -1.0))

    result = await harness.searching_with(reranker=reranker).search(RERANK_QUERY)

    assert _identities(result) == list(reversed(_identities(fused)))
    assert set(_identities(result)) == set(_identities(fused))
    assert result.ordered_by == "rerank"
    assert result.reranker == reranker.name
    assert all(chunk.rerank_score is not None for chunk in result.chunks)


async def test_reranking_leaves_the_fusion_score_and_its_contributions_alone():
    """두 신호가 함께 산다 — 리랭킹 점수가 융합 점수의 자리를 대신하지 않는다."""
    harness = await _harness_with_two_documents()
    fused = await _fused_baseline(harness)
    before = {
        (chunk.document_id, chunk.chunk_index): (chunk.score, chunk.contributions)
        for chunk in fused.chunks
    }

    result = await harness.searching_with(reranker=FakeReranker()).search(RERANK_QUERY)

    common = [chunk for chunk in result.chunks if (chunk.document_id, chunk.chunk_index) in before]
    assert common, "공통 청크가 없어 단언이 공허해졌다"
    for chunk in common:
        assert (chunk.score, chunk.contributions) == before[(chunk.document_id, chunk.chunk_index)]


async def test_candidates_outside_the_rerank_depth_keep_the_fusion_order():
    """깊이 밖은 버려지지도 섞이지도 않는다 — 융합 순서 그대로 뒤에 온다."""
    harness = await _harness_with_two_documents()
    fused = await _fused_baseline(harness)
    assert fused.count > 2
    head = list(fused.chunks[:2])
    scores = _ranking_by_text(fused.chunks, list(reversed(head)))

    result = await harness.searching_with(
        reranker=FakeReranker(scorer=lambda query, text: scores.get(text, 0.0)),
        rerank_candidates=2,
    ).search(RERANK_QUERY)

    reversed_head = [(chunk.document_id, chunk.chunk_index) for chunk in reversed(head)]
    assert _identities(result)[:2] == reversed_head
    assert _identities(result)[2:] == _identities(fused)[2:]


async def test_a_failing_reranker_ends_in_the_fusion_order(caplog):
    """리랭커가 죽어도 사라지는 것은 순서의 질뿐이다 — 근거는 그대로 있다."""
    harness = await _harness_with_two_documents()
    off = await _fused_baseline(harness)
    broken = harness.searching_with(reranker=FakeReranker(error=RuntimeError("주입된 실패")))

    with caplog.at_level("WARNING"):
        result = await broken.search(RERANK_QUERY)

    assert result.chunks == off.chunks, "축소 결과가 리랭커를 끈 구성과 달라졌다"
    assert result.ordered_by == "fusion"
    assert result.reranker is None
    assert any(record.levelname == "WARNING" for record in caplog.records)


async def test_a_reranker_slower_than_the_timeout_ends_in_the_fusion_order():
    """상한이 없으면 축소 경로는 있지만 실제로는 아무것도 지켜 주지 못한다."""
    harness = await _harness_with_two_documents()
    off = await _fused_baseline(harness)
    delay = 0.3
    slow = harness.searching_with(
        reranker=FakeReranker(delay=delay), rerank_timeout_seconds=0.05
    )

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await slow.search(RERANK_QUERY)
    elapsed = loop.time() - started

    assert result.chunks == off.chunks
    assert result.ordered_by == "fusion"
    assert elapsed < delay, f"제한 시간이 지연을 끊지 못했다 — {elapsed:.3f}s"


async def test_reranking_runs_once_between_the_fusion_and_the_revalidation():
    """리랭킹이 재검증 뒤로 밀리면 그 사이의 창으로 밀려난 리비전이 새어 나간다."""
    harness = await _harness_with_two_documents()
    policy = harness.registry.documents[
        next(
            document_id
            for document_id, document in harness.registry.documents.items()
            if document.filename == "policy.txt"
        )
    ]
    reranker = FakeReranker()

    async def delete_after_the_store_query() -> None:
        harness.registry.documents.pop(policy.document_id)

    harness.registry.before_get = delete_after_the_store_query
    result = await harness.searching_with(reranker=reranker).search(RERANK_QUERY)

    assert len(reranker.calls) == 1, "리랭킹이 요청당 한 번이 아니다"
    reranked_texts = reranker.calls[0][1]
    assert any(
        harness.chunk_text(policy.document_id, index) in reranked_texts
        for index in range(len(harness.store.chunks_of(policy.document_id)))
    ), "리랭킹이 재검증 뒤에 돌아 떨어진 문서를 보지 못했다"
    assert all(chunk.document_id != policy.document_id for chunk in result.chunks)


async def test_the_rerank_signature_is_empty_without_a_reranker():
    """빈 값도 값이다 — 캐시가 켠 구성과 끈 구성을 이 값으로 가른다."""
    harness = await _harness_with_two_documents()
    reranker = FakeReranker()

    assert harness.retrieval.rerank_signature == ""
    assert harness.searching_with(reranker=reranker).rerank_signature == reranker.signature
