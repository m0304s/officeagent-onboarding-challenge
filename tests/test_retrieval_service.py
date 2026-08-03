"""검색 서비스 — 임베딩 경로·대상 집합·동시 변경·순서·하한.

API 계층의 요청 검증과 응답 모양은 `test_search_api.py` 가, 임베딩이 의미를 잡는지는
`test_retrieval_quality.py` 가 실물 모델로 덮는다. 여기서 고정하는 것은 **순서와 판정**이다 —
무엇을 언제 부르는가, 무엇을 대상으로 삼고 무엇을 버리는가.

전부 페이크 임베더로 돈다. 벡터에 의미가 없어도 **결정론적**이라, 질의 문자열을 특정 청크
본문과 똑같이 두면 벡터가 정확히 일치해 그 청크의 1위가 확정된다. 그 성질로 정렬 방향과
필터를 의미 없이도 결정적으로 잰다.
"""

from dataclasses import replace

from app.core.documents import IndexStatus
from tests.retrieval_harness import GUIDE, POLICY, SHORT, make_harness

#: 하한 테스트가 공유하는 질의. 하한을 점수 분포에서 유도하므로 세 테스트가 **같은
#: 질의**를 봐야 유도한 값이 그대로 쓰인다.
FLOOR_QUERY = "교육비 지원"

# ── 임베딩 경로 (5.7) ────────────────────────────────────────────────────


async def test_a_search_uses_the_query_embedding_path_exactly_once():
    """경로를 바꿔 써도 결과 형식은 멀쩡하다 — 호출 기록이 아니면 검출되지 않는다.

    `embed_documents([query])[0]` 도 벡터를 내고 오류도 나지 않지만, 역할에 따라 입력을
    다르게 다루는 모델에서는 점수 분포가 통째로 이동해 하한이 조용히 무효가 된다.
    """
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

    잔여 청크가 무해하다는 근거가 이 필터 하나뿐이다. 깨지면 사용자는 자기가 지운
    문장을 근거로 인용한 답변을 받는다.
    """
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

    낡은 `revision` 이 결과에 실려 나가면 다음 change 의 캐시가 그 값으로 항목을 만들고,
    무효화가 이미 지나간 뒤라 **그 항목은 어떤 무효화에도 걸리지 않는다.**
    """
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
    assert all(0 <= score <= 1 for score in scores)


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
    assert all(chunk.score >= floor for chunk in result.chunks)


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
    """1위와 2위 점수 **사이**의 하한.

    상수로 박으면 페이크의 점수 분포가 조금만 움직여도 단언이 공허해진다 — 전부
    통과하거나 전부 걸리는 쪽으로 조용히 넘어가고, 그때 테스트는 여전히 초록이다.
    """
    everything = await harness.searching_with(min_score=0.0).search(FLOOR_QUERY)
    assert everything.count >= 2
    return (everything.chunks[0].score + everything.chunks[1].score) / 2


def _store_that_cannot_delete():
    """이전 세대 정리가 실패하는 저장소.

    교체 자체는 성립하고(저장과 커밋은 지나갔다) 이전 리비전 청크만 남는다 — 수집이
    "정리 실패는 응답을 바꾸지 않는다"고 정해 둔 그 상태다.
    """
    from tests.stubs import StubVectorStore

    return StubVectorStore(fail_delete=True)
