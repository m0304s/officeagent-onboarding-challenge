"""수집 파이프라인 — 저장까지 이어지는 경로.

여기서 고정하는 것은 **순서와 판정**이다.

- 새로 쓰고 → 커밋 → 지우기. 실패하면 같은 요청 안에서 되돌린다.
- 단축(`unchanged`)의 조건은 `revision` 과 `index_signature` **둘 다**.
- 기동 정리는 레지스트리가 가리키지 않는 청크를 지우고, 구성이 바뀐 문서를 `stale` 로 만든다.

저장소 어댑터 자체의 성질은 `test_registry.py`·`test_vector_store.py` 가 실물로 덮는다.
"""

import asyncio

import pytest

from app.core.documents import (
    Chunk,
    ChunkLocation,
    IndexStatus,
    IngestionStatus,
    derive_document_id,
)
from app.core.exceptions import (
    DocumentNotFound,
    EmptyDocument,
    NoExtractableText,
    StorageUnavailable,
)
from app.core.lexical import DEFAULT_TOKENIZER, Tokenizer
from app.services.ingestion import ReconciliationReport
from tests.ingestion_harness import LONG_KOREAN, make_service
from tests.pdf_fixtures import BLANK_PAGE, make_pdf
from tests.stubs import (
    FakeEmbedder,
    StubDocumentRegistry,
    StubLexicalIndex,
    StubParser,
    StubVectorStore,
)

POLICY = "policy.txt"
DATA = LONG_KOREAN.encode("utf-8")
OTHER_DATA = (LONG_KOREAN + " 추가된 문단입니다.").encode("utf-8")
SHORT_DATA = "짧은 문서입니다.".encode()


@pytest.fixture
def store() -> StubVectorStore:
    return StubVectorStore()


@pytest.fixture
def lexical() -> StubLexicalIndex:
    return StubLexicalIndex()


@pytest.fixture
def registry() -> StubDocumentRegistry:
    return StubDocumentRegistry()


@pytest.fixture
def service(store, lexical, registry):
    return make_service(vector_store=store, lexical_index=lexical, registry=registry)


# ── 최초 수집 ────────────────────────────────────────────────────────────


async def test_a_first_upload_is_created_and_stored(service, store):
    result = await service.ingest(POLICY, DATA)

    document = result.document
    assert result.status is IngestionStatus.CREATED
    assert result.previous_revision is None
    assert document.index_status is IndexStatus.INDEXED
    assert document.chunk_count > 1
    assert await store.count_chunks(document.document_id) == document.chunk_count


async def test_every_stored_chunk_carries_the_index_signature(service, store):
    """서명이 청크에 없으면 두 세대를 구분할 수 없고 재색인 정리가 성립하지 않는다."""
    result = await service.ingest(POLICY, DATA)

    chunks = store.chunks_of(result.document.document_id)
    assert {chunk.index_signature for chunk in chunks} == {service.index_signature}
    assert result.document.index_signature == service.index_signature


async def test_chunk_indices_are_complete(service, store):
    """배치 경계에서 누락되거나 중복 저장된 청크가 없다는 증거다."""
    result = await service.ingest(POLICY, DATA)

    indices = [chunk.chunk_index for chunk in store.chunks_of(result.document.document_id)]
    assert indices == list(range(result.document.chunk_count))


async def test_the_document_is_readable_from_the_registry(service, registry):
    result = await service.ingest(POLICY, DATA)

    assert await registry.get(result.document.document_id) == result.document


# ── 배치 ─────────────────────────────────────────────────────────────────


async def test_chunks_are_embedded_and_written_in_batches(store, registry):
    """메모리가 문서 크기가 아니라 배치 크기에 비례해야 한다."""
    embedder = FakeEmbedder()
    service = make_service(vector_store=store, registry=registry, embedder=embedder, batch_size=2)

    result = await service.ingest(POLICY, DATA)

    chunk_count = result.document.chunk_count
    assert chunk_count > 2, "배치 경계를 넘지 않는 문서로는 이 테스트가 아무것도 검증하지 않는다"
    assert store.batch_sizes == [2] * (chunk_count // 2) + ([1] if chunk_count % 2 else [])
    assert [len(batch) for batch in embedder.batches] == store.batch_sizes
    assert await store.count_chunks(result.document.document_id) == chunk_count


# ── 토큰 가드 ────────────────────────────────────────────────────────────


async def test_chunks_over_the_token_window_are_resplit(store, registry):
    """문자 기준 청크는 토큰 수를 보장하지 못한다.

    상한을 넘는 청크를 그대로 넘기면 뒷부분이 **조용히 잘린다** — 벡터에는 반영되지
    않으면서 청크 본문에는 남아, 저장은 됐는데 검색되지 않는 텍스트가 생긴다.
    """
    # 문자 하나가 토큰 하나. 200자 청크는 64토큰 상한을 반드시 넘는다.
    embedder = FakeEmbedder(chars_per_token=1, max_input_tokens=64)
    service = make_service(vector_store=store, registry=registry, embedder=embedder, size=200)

    result = await service.ingest(POLICY, DATA)

    chunks = store.chunks_of(result.document.document_id)
    assert all(embedder.count_document_tokens(chunk.text) <= 64 for chunk in chunks)
    assert result.document.chunk_count == len(chunks)


async def test_the_token_guard_keeps_chunk_text_pointing_at_the_source(store, registry):
    """재분할해도 출처는 원문의 같은 자리를 가리켜야 한다."""
    embedder = FakeEmbedder(chars_per_token=1, max_input_tokens=64)
    service = make_service(vector_store=store, registry=registry, embedder=embedder, size=200)

    result = await service.ingest(POLICY, DATA)

    for chunk in store.chunks_of(result.document.document_id):
        assert chunk.text in LONG_KOREAN


async def test_chunks_within_the_window_are_left_alone(service, store):
    result = await service.ingest(POLICY, DATA)

    # 기본 페이크는 2자당 1토큰이라 200자 청크가 100토큰 — 512 상한 안이다.
    assert result.document.chunk_count == len(store.chunks_of(result.document.document_id))


# ── 재업로드 ─────────────────────────────────────────────────────────────


async def test_new_content_replaces_the_previous_revision(service, store):
    first = await service.ingest(POLICY, DATA)

    second = await service.ingest(POLICY, OTHER_DATA)

    assert second.status is IngestionStatus.REPLACED
    assert second.document.revision != first.document.revision
    assert second.previous_revision == first.document.revision
    document_id = second.document.document_id
    assert await store.count_chunks(document_id, revision=first.document.revision) == 0
    assert (
        await store.count_chunks(document_id, revision=second.document.revision)
        == second.document.chunk_count
    )


async def test_chunks_do_not_accumulate_across_revisions(service, store):
    """짧은 문서로 교체하면 청크 수가 새 문서 기준이 되어야 한다."""
    first = await service.ingest(POLICY, DATA)
    assert first.document.chunk_count > 1

    second = await service.ingest(POLICY, SHORT_DATA)

    assert second.document.chunk_count == 1
    assert await store.count_chunks(second.document.document_id) == 1


async def test_identical_bytes_under_the_same_configuration_change_nothing(service, store):
    first = await service.ingest(POLICY, DATA)
    writes_before = store.add_calls

    second = await service.ingest(POLICY, DATA)

    assert second.status is IngestionStatus.UNCHANGED
    assert second.document == first.document  # ingested_at 까지 그대로다
    assert store.add_calls == writes_before, "재색인하지 않기로 해 놓고 다시 썼다"


async def test_identical_bytes_under_a_new_configuration_are_reindexed(store, registry):
    """`revision` 만 보고 단축하면 모델을 갈아도 재업로드가 조용히 무시된다."""
    first = await make_service(vector_store=store, registry=registry).ingest(POLICY, DATA)

    # 임베더 정체성만 바꾼다 — 내용도 파일명도 그대로다.
    changed = make_service(
        vector_store=store,
        registry=registry,
        embedder=FakeEmbedder(signature="other-model/8/l2norm/none-v1"),
    )
    second = await changed.ingest(POLICY, DATA)

    assert second.status is IngestionStatus.REINDEXED
    assert second.document.revision == first.document.revision
    assert second.document.index_signature != first.document.index_signature
    assert second.previous_revision is None  # 리비전이 그대로라 "이전 리비전"이 없다
    document_id = second.document.document_id
    old_signature = first.document.index_signature
    assert await store.count_chunks(document_id, index_signature=old_signature) == 0
    assert (
        await store.count_chunks(document_id, index_signature=second.document.index_signature)
        == second.document.chunk_count
    )


async def test_a_reupload_under_a_new_chunk_size_is_also_a_reindex(store, registry):
    await make_service(vector_store=store, registry=registry, size=200).ingest(POLICY, DATA)

    current = make_service(vector_store=store, registry=registry, size=400)
    result = await current.ingest(POLICY, DATA)

    assert result.status is IngestionStatus.REINDEXED
    assert await store.count_chunks(result.document.document_id) == result.document.chunk_count


# ── 저장 실패 — 이전 리비전은 살아남는다 ────────────────────────────────


async def test_a_write_failure_rolls_back_the_new_revision(registry):
    """부분 기록을 남긴 채 실패하면 잔여 청크가 검색 인덱스에 남는다."""
    store = StubVectorStore()
    service = make_service(vector_store=store, registry=registry, batch_size=1)
    first = await service.ingest(POLICY, DATA)

    # 배치 하나를 쓴 뒤 실패한다 — 되돌릴 것이 실제로 있는 상태.
    store.fail_add_after = store.add_calls + 1
    with pytest.raises(StorageUnavailable):
        await service.ingest(POLICY, OTHER_DATA)

    document_id = first.document.document_id
    stored = await registry.get(document_id)
    assert stored == first.document, "실패한 교체가 이전 리비전의 기록을 바꿨다"
    assert await store.count_chunks(document_id, revision=first.document.revision) == (
        first.document.chunk_count
    )
    # 응답이 반환된 시점에 새 리비전 청크가 0개여야 한다.
    assert await store.count_chunks(document_id) == first.document.chunk_count


async def test_a_commit_failure_also_rolls_back(store):
    """커밋은 교체가 확정되는 순간이다. 그 직전 실패도 같은 경로를 타야 한다."""
    registry = StubDocumentRegistry()
    service = make_service(vector_store=store, registry=registry)

    registry.fail_commit = True
    with pytest.raises(StorageUnavailable):
        await service.ingest(POLICY, DATA)

    assert await store.count_chunks() == 0
    assert await registry.list_all() == []


async def test_the_previous_revision_survives_even_when_the_roll_back_fails(registry):
    """되돌리기 실패는 응답을 바꾸지 않는다 — 잔여 청크는 기동 정리가 회수한다."""
    store = StubVectorStore()
    service = make_service(vector_store=store, registry=registry, batch_size=1)
    first = await service.ingest(POLICY, DATA)

    # 배치 하나를 쓴 뒤 실패하고, 그 부분 기록을 되돌리는 것마저 실패한다.
    store.fail_add_after = store.add_calls + 1
    store.fail_delete = True
    with pytest.raises(StorageUnavailable):
        await service.ingest(POLICY, OTHER_DATA)

    store.fail_delete = False
    document_id = first.document.document_id
    assert await registry.get(document_id) == first.document
    assert (
        await store.count_chunks(document_id, revision=first.document.revision)
        == first.document.chunk_count
    )
    # 되돌리지 못한 잔여 청크는 남는다. 레지스트리가 가리키지 않으므로 검색 대상이
    # 아니고, 다음 기동의 정리가 회수한다.
    leftover = await store.count_chunks(document_id) - first.document.chunk_count
    assert leftover > 0
    assert (await service.reconcile_storage()).removed_chunks == leftover


async def test_a_failure_to_purge_the_previous_generation_does_not_fail_the_request(
    store, registry, caplog
):
    """이전 청크 정리 실패는 이미 성립한 교체를 되돌릴 수 없다. 로그로만 드러낸다."""
    service = make_service(vector_store=store, registry=registry)
    first = await service.ingest(POLICY, DATA)

    # 쓰기와 커밋은 성공하고 마지막 정리 단계만 실패한다.
    store.fail_delete = True
    with caplog.at_level("WARNING", logger="app.services.ingestion"):
        result = await service.ingest(POLICY, OTHER_DATA)

    assert result.status is IngestionStatus.REPLACED
    assert any("이전 청크 정리" in record.message for record in caplog.records)
    store.fail_delete = False
    document_id, revision = first.document.document_id, first.document.revision
    assert await store.count_chunks(document_id, revision=revision) > 0


# ── 오류 경로 — 기존 문서를 건드리지 않는다 ─────────────────────────────


@pytest.mark.parametrize("data", [b"", b"   \n\n\t  "])
async def test_documents_without_content_are_refused(service, store, registry, data):
    with pytest.raises(EmptyDocument):
        await service.ingest(POLICY, data)

    assert await registry.list_all() == []
    assert await store.count_chunks() == 0


async def test_a_scanned_pdf_is_refused_with_its_own_code(service):
    with pytest.raises(NoExtractableText):
        await service.ingest("scanned.pdf", make_pdf([BLANK_PAGE, BLANK_PAGE]))


async def test_a_failed_upload_leaves_the_existing_document_alone(service, store, registry):
    """실패한 업로드가 기존 문서를 지우면, 고칠 수 있던 실수가 데이터 손실이 된다."""
    first = await service.ingest(POLICY, DATA)

    with pytest.raises(EmptyDocument):
        await service.ingest(POLICY, b"")

    assert await registry.get(first.document.document_id) == first.document
    assert await store.count_chunks(first.document.document_id) == first.document.chunk_count


# ── 삭제 ────────────────────────────────────────────────────────────────


async def test_deleting_removes_the_document_and_every_chunk(service, store, registry):
    result = await service.ingest(POLICY, DATA)
    document_id = result.document.document_id

    removed = await service.delete(document_id)

    assert removed == result.document
    assert await registry.get(document_id) is None
    assert await store.count_chunks(document_id) == 0


async def test_deleting_leaves_other_documents_alone(service, store):
    await service.ingest(POLICY, DATA)
    other = await service.ingest("guide.md", OTHER_DATA)

    await service.delete(derive_document_id(POLICY))

    assert await store.count_chunks(other.document.document_id) == other.document.chunk_count


async def test_deleting_an_unknown_document_is_a_not_found(service):
    with pytest.raises(DocumentNotFound):
        await service.delete("없는-문서")


async def test_a_document_can_be_ingested_again_after_deletion(service):
    first = await service.ingest(POLICY, DATA)
    await service.delete(first.document.document_id)

    again = await service.ingest(POLICY, DATA)

    assert again.status is IngestionStatus.CREATED
    assert again.document.document_id == first.document.document_id


# ── 기동 정리 ────────────────────────────────────────────────────────────


def orphan_chunk(document_id: str, *, revision: str, index_signature: str) -> Chunk:
    return Chunk(
        document_id=document_id,
        revision=revision,
        index_signature=index_signature,
        chunk_index=0,
        text="되돌리기가 닿지 못한 잔여물",
        location=ChunkLocation(char_start=0, char_end=10),
    )


async def test_leftover_chunks_are_reclaimed_and_current_ones_are_kept(service, store):
    """크래시 백스톱이다 — 요청 안의 되돌리기가 닿지 못한 잔여물만 회수한다."""
    result = await service.ingest(POLICY, DATA)
    document_id = result.document.document_id
    leftover = orphan_chunk(
        document_id, revision="죽은-리비전", index_signature=service.index_signature
    )
    store.records[leftover.id] = {"chunk": leftover, "embedding": [], "filename": POLICY}

    report = await service.reconcile_storage()

    assert report.removed_chunks == 1
    assert await store.count_chunks(document_id, revision="죽은-리비전") == 0
    assert await store.count_chunks(document_id) == result.document.chunk_count


async def test_a_changed_configuration_drops_the_chunks_and_marks_the_document_stale(
    store, registry
):
    """구 구성 벡터를 그대로 두면 다른 의미 공간의 벡터가 검색에 섞인다."""
    first = await make_service(vector_store=store, registry=registry).ingest(POLICY, DATA)

    reconfigured = make_service(
        vector_store=store,
        registry=registry,
        embedder=FakeEmbedder(signature="other-model/8/l2norm/none-v1"),
    )
    report = await reconfigured.reconcile_storage()

    stored = await registry.get(first.document.document_id)
    assert report.stale_documents == (first.document.document_id,)
    assert stored.index_status is IndexStatus.STALE
    assert stored.chunk_count == 0
    assert await store.count_chunks(first.document.document_id) == 0


async def test_an_unchanged_configuration_keeps_everything(service, store, registry):
    result = await service.ingest(POLICY, DATA)

    report = await service.reconcile_storage()

    assert report == ReconciliationReport()  # 아무 일도 하지 않았다
    stored = await registry.get(result.document.document_id)
    assert stored.index_status is IndexStatus.INDEXED
    assert stored.chunk_count == result.document.chunk_count
    assert await store.count_chunks(result.document.document_id) == result.document.chunk_count


async def test_documents_matching_the_current_signature_are_untouched(store, registry):
    """구성이 바뀐 뒤 수집한 문서까지 함께 지우면 방금 올린 문서가 사라진다."""
    stale_upload = await make_service(vector_store=store, registry=registry, size=200).ingest(
        POLICY, DATA
    )
    current = make_service(vector_store=store, registry=registry, size=400)
    fresh_upload = await current.ingest("guide.md", OTHER_DATA)

    report = await current.reconcile_storage()

    assert report.stale_documents == (stale_upload.document.document_id,)
    assert (
        await store.count_chunks(fresh_upload.document.document_id)
        == fresh_upload.document.chunk_count
    )
    assert (await registry.get(fresh_upload.document.document_id)).index_status is (
        IndexStatus.INDEXED
    )


async def test_a_stale_document_is_recovered_by_reuploading_it(store, registry):
    """원본 바이트를 보관하지 않으므로 재업로드가 유일한 복구 경로다."""
    await make_service(vector_store=store, registry=registry, size=200).ingest(POLICY, DATA)
    current = make_service(vector_store=store, registry=registry, size=400)
    await current.reconcile_storage()

    result = await current.ingest(POLICY, DATA)

    assert result.status is IngestionStatus.REINDEXED
    assert result.document.index_status is IndexStatus.INDEXED
    assert result.document.chunk_count >= 1
    assert await store.count_chunks(result.document.document_id) == result.document.chunk_count


async def test_reconciliation_never_fails_startup(store, registry, caplog):
    """평가자가 설정을 한 번 만져 본 순간 "한 줄 실행"이 깨지면 안 된다."""
    await make_service(vector_store=store, registry=registry, size=200).ingest(POLICY, DATA)
    # 구성이 바뀌어 정리할 일이 생겼는데, 그 정리가 실패하는 상태.
    reconfigured = make_service(vector_store=store, registry=registry, size=400)
    store.fail_delete = True

    with caplog.at_level("WARNING", logger="app.services.ingestion"):
        report = await reconfigured.reconcile_storage()

    assert report == ReconciliationReport()
    assert any("기동 정리에 실패" in record.message for record in caplog.records)


# ── 같은 문서의 동시 요청 ────────────────────────────────────────────────


# ── 두 색인은 한 트랜잭션의 두 면이다 ───────────────────────────────────
#
# 두 색인이 어긋나도 오류는 나지 않는다. 검색은 계속 `200` 을 내고, 다만 retriever 마다
# 다른 세대의 문장을 근거로 올린다. 융합은 그 둘을 정상 입력으로 받으므로 한 응답 안에
# 두 세대가 섞인다 — `retrieval` 이 보증하는 "한 문서의 결과는 전부 같은 리비전"이
# 저장 단계에서 이미 깨져 있는 상태다. 그래서 아래 단언은 전부 **양쪽**을 함께 본다.


async def test_ingested_chunks_land_in_both_indexes(service, store, lexical):
    result = await service.ingest(POLICY, DATA)

    document_id = result.document.document_id
    assert await store.count_chunks(document_id) == result.document.chunk_count
    assert await lexical.count_chunks(document_id) == result.document.chunk_count


async def test_both_indexes_carry_the_same_axes(service, store, lexical):
    """축이 어긋나면 "이 문서의 현재 세대"가 색인마다 다른 것을 가리킨다."""
    result = await service.ingest(POLICY, DATA)

    document_id = result.document.document_id
    assert lexical.chunks_of(document_id) == store.chunks_of(document_id)
    assert await lexical.list_stored_versions() == await store.list_stored_versions()


async def test_replacing_clears_the_previous_revision_from_both(service, store, lexical):
    first = await service.ingest(POLICY, DATA)

    second = await service.ingest(POLICY, OTHER_DATA)

    document_id = second.document.document_id
    old, new = first.document.revision, second.document.revision
    for index in (store, lexical):
        assert await index.count_chunks(document_id, revision=old) == 0
        assert await index.count_chunks(document_id, revision=new) == second.document.chunk_count


async def test_reindexing_clears_the_previous_signature_from_both(store, lexical, registry):
    first = await make_service(
        vector_store=store, lexical_index=lexical, registry=registry
    ).ingest(POLICY, DATA)

    changed = make_service(
        vector_store=store,
        lexical_index=lexical,
        registry=registry,
        embedder=FakeEmbedder(signature="other-model/8/l2norm/none-v1"),
    )
    second = await changed.ingest(POLICY, DATA)

    assert second.status is IngestionStatus.REINDEXED
    document_id = second.document.document_id
    old_signature = first.document.index_signature
    for index in (store, lexical):
        assert await index.count_chunks(document_id, index_signature=old_signature) == 0
        assert await index.count_chunks(document_id) == second.document.chunk_count


async def test_deleting_clears_both_indexes(service, store, lexical):
    result = await service.ingest(POLICY, DATA)

    await service.delete(result.document.document_id)

    assert await store.count_chunks(result.document.document_id) == 0
    assert await lexical.count_chunks(result.document.document_id) == 0


async def test_a_lexical_write_failure_rolls_back_the_vector_side(store, lexical, registry):
    """한쪽에만 저장된 채로 수집이 성공으로 끝나면 두 세대가 한 응답에 섞인다."""
    service = make_service(
        vector_store=store, lexical_index=lexical, registry=registry, batch_size=1
    )

    lexical.fail_add_after = 0
    with pytest.raises(StorageUnavailable):
        await service.ingest(POLICY, DATA)

    assert await store.count_chunks() == 0, "어휘 쪽이 실패했는데 벡터가 남았다"
    assert await lexical.count_chunks() == 0
    assert await registry.list_all() == []


async def test_a_vector_write_failure_rolls_back_the_lexical_side(store, lexical, registry):
    """되돌리기가 한쪽만 닿으면 레지스트리가 가리키지 않는 문장이 어휘 검색에 남는다."""
    service = make_service(
        vector_store=store, lexical_index=lexical, registry=registry, batch_size=1
    )

    # 배치 하나를 양쪽에 쓴 뒤 벡터 쓰기가 실패한다 — 어휘 쪽에 되돌릴 것이 실제로 있다.
    store.fail_add_after = 1
    with pytest.raises(StorageUnavailable):
        await service.ingest(POLICY, DATA)

    assert await lexical.count_chunks() == 0, "벡터 쪽이 실패했는데 어휘가 남았다"
    assert await store.count_chunks() == 0
    assert await registry.list_all() == []


async def test_a_failed_replacement_keeps_the_previous_revision_in_both(store, lexical, registry):
    service = make_service(
        vector_store=store, lexical_index=lexical, registry=registry, batch_size=1
    )
    first = await service.ingest(POLICY, DATA)

    lexical.fail_add_after = lexical.add_calls + 1
    with pytest.raises(StorageUnavailable):
        await service.ingest(POLICY, OTHER_DATA)

    document_id = first.document.document_id
    stored = await registry.get(document_id)
    assert (stored.revision, stored.chunk_count) == (
        first.document.revision,
        first.document.chunk_count,
    )
    for index in (store, lexical):
        assert await index.count_chunks(document_id) == first.document.chunk_count


async def test_reconciliation_reclaims_leftovers_from_both(service, store, lexical):
    """되돌리기가 닿지 못한 잔여물은 어느 색인에 남았든 회수되어야 한다."""
    result = await service.ingest(POLICY, DATA)
    document_id = result.document.document_id
    leftover = orphan_chunk(
        document_id, revision="죽은-리비전", index_signature=service.index_signature
    )
    store.records[leftover.id] = {"chunk": leftover, "embedding": [], "filename": POLICY}
    lexical.records[leftover.id] = {"chunk": leftover, "filename": POLICY, "format": None}

    report = await service.reconcile_storage()

    assert report.removed_chunks == 2, "두 색인에서 각각 하나씩 회수되어야 한다"
    for index in (store, lexical):
        assert await index.count_chunks(document_id, revision="죽은-리비전") == 0
        assert await index.count_chunks(document_id) == result.document.chunk_count


async def test_a_leftover_in_only_one_index_is_still_reclaimed(service, lexical):
    """합집합이 아니라 교집합으로 보면 한쪽에만 남은 잔여물이 영원히 회수되지 않는다."""
    result = await service.ingest(POLICY, DATA)
    document_id = result.document.document_id
    leftover = orphan_chunk(document_id, revision="죽은-리비전", index_signature="죽은-서명")
    lexical.records[leftover.id] = {"chunk": leftover, "filename": POLICY, "format": None}

    report = await service.reconcile_storage()

    assert report.removed_chunks == 1
    assert await lexical.count_chunks(document_id, revision="죽은-리비전") == 0


async def test_stale_handling_reaches_both_indexes(store, lexical, registry):
    first = await make_service(
        vector_store=store, lexical_index=lexical, registry=registry, size=200
    ).ingest(POLICY, DATA)

    reconfigured = make_service(
        vector_store=store, lexical_index=lexical, registry=registry, size=400
    )
    report = await reconfigured.reconcile_storage()

    document_id = first.document.document_id
    stored = await registry.get(document_id)
    assert report.stale_documents == (document_id,)
    assert (stored.index_status, stored.chunk_count) == (IndexStatus.STALE, 0)
    assert await store.count_chunks(document_id) == 0
    assert await lexical.count_chunks(document_id) == 0


async def test_a_tokenizer_change_makes_existing_documents_stale(store, lexical, registry):
    """토큰화 규칙을 고치면 기존 색인은 옛 규약으로 쓰였고 질의는 새 규약으로 온다."""
    first = await make_service(
        vector_store=store, lexical_index=lexical, registry=registry
    ).ingest(POLICY, DATA)

    retuned = make_service(
        vector_store=store,
        lexical_index=lexical,
        registry=registry,
        tokenizer=Tokenizer(suffixes=(*DEFAULT_TOKENIZER.suffixes, "께서")),
    )
    report = await retuned.reconcile_storage()

    assert retuned.index_signature != first.document.index_signature
    assert report.stale_documents == (first.document.document_id,)
    assert await lexical.count_chunks(first.document.document_id) == 0


async def test_concurrent_uploads_of_the_same_document_are_serialized(store, registry):
    """`get → 저장 → 커밋 → 삭제` 가 인터리빙되면 두 리비전의 청크가 함께 남는다.

    어느 쪽이 이기는지는 단언하지 않는다 — 비결정적이다. 최종 상태만 본다.
    """
    service = make_service(
        parsers=[StubParser(delay=0.05, text=LONG_KOREAN)],
        vector_store=store,
        registry=registry,
    )

    results = await asyncio.gather(service.ingest(POLICY, DATA), service.ingest(POLICY, OTHER_DATA))

    document_id = results[0].document.document_id
    assert {result.document.document_id for result in results} == {document_id}
    winner = await registry.get(document_id)
    assert winner.revision in {result.document.revision for result in results}
    assert await store.count_chunks(document_id) == winner.chunk_count
    assert await store.count_chunks(document_id, revision=winner.revision) == winner.chunk_count


async def test_different_documents_do_not_wait_for_each_other(store, registry):
    """잠금이 문서별이 아니라 전역이 되면 여기서 깨진다."""
    delay = 0.1
    service = make_service(
        parsers=[StubParser(delay=delay, text=LONG_KOREAN)],
        vector_store=store,
        registry=registry,
        concurrency=2,
    )

    started = asyncio.get_running_loop().time()
    await asyncio.gather(service.ingest("a.txt", DATA), service.ingest("b.txt", OTHER_DATA))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 2 * delay, "서로 다른 문서가 직렬화됐다"


async def test_the_concurrency_limit_holds(store, registry):
    """상한에 걸린 요청은 실패하지 않고 대기한다."""
    delay = 0.1
    service = make_service(
        parsers=[StubParser(delay=delay, text=LONG_KOREAN)],
        vector_store=store,
        registry=registry,
        concurrency=1,
    )

    started = asyncio.get_running_loop().time()
    results = await asyncio.gather(
        service.ingest("a.txt", DATA), service.ingest("b.txt", OTHER_DATA)
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert all(result.status is IngestionStatus.CREATED for result in results)
    assert elapsed >= 2 * delay, "상한 1인데 두 수집이 동시에 돌았다"


async def test_reconciling_twice_does_not_write_twice(store, registry):
    """아무것도 바뀌지 않은 기동이 쓰기를 만들면 안 된다.

    `stale` 문서의 서명은 그대로 두므로(그래야 재업로드가 재색인으로 이어진다) 매 기동
    마다 같은 문서가 다시 걸린다. 경고는 계속 남아야 하지만 — 여전히 다시 올려야 하는
    문서다 — 삭제와 커밋을 반복할 이유는 없다.
    """
    first = await make_service(vector_store=store, registry=registry, size=200).ingest(POLICY, DATA)
    reconfigured = make_service(vector_store=store, registry=registry, size=400)

    await reconfigured.reconcile_storage()
    writes_after_first = registry.commits
    again = await reconfigured.reconcile_storage()

    assert again.stale_documents == (first.document.document_id,)
    assert again.removed_chunks == 0
    assert registry.commits == writes_after_first
