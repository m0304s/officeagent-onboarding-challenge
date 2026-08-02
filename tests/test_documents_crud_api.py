"""문서 목록·상세·삭제와 재업로드 의미론.

업로드 한 건이 아니라 **문서의 생애**를 다룬다 — 올린 것이 조회되는가, 다시 올리면
무엇이 바뀌는가, 지우면 정말 사라지는가, 색인 구성을 바꾸면 어떻게 드러나는가.

`status` 네 값(`created`·`replaced`·`reindexed`·`unchanged`)의 구분이 이 파일의 축이다.
넷을 뭉개면 클라이언트는 자기가 올린 파일이 실제로 색인됐는지 알 수 없다.
"""

import pytest

from tests.api_harness import LONG_KOREAN, SHORT_KOREAN, document_ids, upload
from tests.conftest import booted
from tests.stubs import FakeEmbedder, StubDocumentRegistry, StubVectorStore

DATA = LONG_KOREAN.encode("utf-8")
OTHER_DATA = (LONG_KOREAN + "\n\n연차는 입사 첫해부터 15일입니다.").encode("utf-8")


async def post(client, filename: str = "policy.txt", data: bytes = DATA) -> dict:
    """업로드하고 본문을 돌려준다. 실패는 여기서 드러난다 — 뒤 단언이 엉뚱하게 깨지지 않게."""
    response = await client.post("/documents", **upload(filename, data))
    assert response.status_code in (200, 201), response.text
    return response.json()


# ── 목록과 상세 ─────────────────────────────────────────────────────────


async def test_the_list_is_empty_before_anything_is_collected(client):
    response = await client.get("/documents")

    assert response.status_code == 200
    assert response.json() == {"documents": [], "count": 0}


async def test_a_collected_document_appears_in_the_list_and_detail(client):
    created = await post(client)

    listing = (await client.get("/documents")).json()
    detail = await client.get(f"/documents/{created['document_id']}")

    assert listing["count"] == 1
    assert listing["documents"][0]["document_id"] == created["document_id"]
    assert detail.status_code == 200
    # 상세는 업로드 응답과 같은 사실을 말해야 한다 — `status` 만 업로드 쪽에 더 있다.
    assert detail.json() == {
        key: value for key, value in created.items() if key not in ("status", "previous_revision")
    }


async def test_the_detail_carries_the_index_signature_and_status(client):
    """검색되지 않는 문서를 설명할 수 있는 값은 이 둘뿐이다."""
    created = await post(client)

    detail = (await client.get(f"/documents/{created['document_id']}")).json()

    assert detail["index_signature"] == created["index_signature"]
    assert detail["index_status"] == "indexed"
    assert detail["chunk_count"] >= 1
    assert detail["ingested_at"] == created["ingested_at"]


async def test_the_list_is_newest_first(client):
    first = await post(client, "policy.txt")
    second = await post(client, "guide.md")

    assert await document_ids(client) == [second["document_id"], first["document_id"]]


async def test_an_unknown_document_is_a_404(client):
    response = await client.get("/documents/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ── 재업로드 — status 네 값 ─────────────────────────────────────────────


async def test_new_content_replaces_the_previous_revision(client, vector_store):
    first = await post(client, data=DATA)

    again = await post(client, data=OTHER_DATA)

    assert again["status"] == "replaced"
    assert again["revision"] != first["revision"]
    assert again["previous_revision"] == first["revision"]
    document_id = first["document_id"]
    assert await vector_store.count_chunks(document_id, revision=first["revision"]) == 0
    assert (
        await vector_store.count_chunks(document_id, revision=again["revision"])
        == again["chunk_count"]
    )


async def test_replacing_a_long_document_with_a_short_one_shrinks_the_chunk_count(
    client, vector_store
):
    """이전 청크가 누적되면 지워진 내용이 계속 검색된다."""
    first = await post(client, data=DATA)
    assert first["chunk_count"] > 1

    again = await post(client, data=SHORT_KOREAN.encode("utf-8"))

    detail = (await client.get(f"/documents/{first['document_id']}")).json()
    assert again["chunk_count"] == 1
    assert detail["chunk_count"] == 1
    assert await vector_store.count_chunks(first["document_id"]) == 1


async def test_identical_bytes_are_not_reindexed(client, embedder):
    first = await post(client)
    batches_after_first = len(embedder.batches)

    again = await post(client)

    assert again["status"] == "unchanged"
    assert (again["revision"], again["index_signature"]) == (
        first["revision"],
        first["index_signature"],
    )
    assert (again["chunk_count"], again["ingested_at"]) == (
        first["chunk_count"],
        first["ingested_at"],
    )
    assert len(embedder.batches) == batches_after_first, "재색인하지 않아야 할 요청이 임베딩했다"


async def test_the_response_status_code_distinguishes_creation_from_update(client):
    """`201` 은 무언가 새로 생겼을 때만이다."""
    created = await client.post("/documents", **upload("policy.txt", DATA))
    replaced = await client.post("/documents", **upload("policy.txt", OTHER_DATA))
    unchanged = await client.post("/documents", **upload("policy.txt", OTHER_DATA))

    assert created.status_code == 201
    assert replaced.status_code == 200
    assert unchanged.status_code == 200


# ── 삭제 ────────────────────────────────────────────────────────────────


async def test_deleting_a_document_removes_it_and_its_chunks(client, vector_store):
    created = await post(client)

    response = await client.delete(f"/documents/{created['document_id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert await document_ids(client) == []
    assert (await client.get(f"/documents/{created['document_id']}")).status_code == 404
    assert await vector_store.count_chunks(created["document_id"]) == 0


async def test_deleting_one_document_leaves_the_other_untouched(client, vector_store):
    kept = await post(client, "guide.md")
    removed = await post(client, "policy.txt", OTHER_DATA)

    await client.delete(f"/documents/{removed['document_id']}")

    detail = (await client.get(f"/documents/{kept['document_id']}")).json()
    assert detail["chunk_count"] == kept["chunk_count"]
    assert await vector_store.count_chunks(kept["document_id"]) == kept["chunk_count"]
    assert await vector_store.count_chunks(removed["document_id"]) == 0


async def test_deleting_an_unknown_document_is_a_404(client):
    response = await client.delete("/documents/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_recollecting_a_deleted_document_starts_over(client):
    """삭제 뒤의 재수집은 교체가 아니라 최초 수집이다 — 가리킬 이전 리비전이 없다."""
    created = await post(client)
    await client.delete(f"/documents/{created['document_id']}")

    response = await client.post("/documents", **upload("policy.txt", DATA))

    body = response.json()
    assert response.status_code == 201
    assert body["status"] == "created"
    assert body["previous_revision"] is None
    assert body["document_id"] == created["document_id"]
    assert body["chunk_count"] >= 1


# ── 색인 서명 ───────────────────────────────────────────────────────────


async def collect_in_isolation(make_client, **overrides) -> dict:
    """독립된 저장소에 같은 문서를 수집한다.

    저장소를 공유하면 두 번째 수집이 교체·재색인이 되어, 확인하려는 것(구성이 서명을
    어떻게 바꾸는가)과 다른 경로가 섞인다.
    """
    async with make_client(
        vector_store=StubVectorStore(), registry=StubDocumentRegistry(), **overrides
    ) as client:
        return await post(client)


async def test_the_same_configuration_yields_the_same_signature(client):
    first = await post(client, "policy.txt", DATA)
    second = await post(client, "guide.md", OTHER_DATA)

    assert first["index_signature"] == second["index_signature"]
    assert first["revision"] != second["revision"]


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"chunk_size": 200}, id="chunk_size"),
        pytest.param({"chunk_overlap": 37}, id="chunk_overlap"),
    ],
)
async def test_changing_the_chunking_changes_the_signature(make_client, settings, update):
    baseline = await collect_in_isolation(make_client)

    changed = await collect_in_isolation(make_client, settings=settings.model_copy(update=update))

    assert changed["index_signature"] != baseline["index_signature"]
    assert changed["revision"] == baseline["revision"]


async def test_changing_the_embedder_identity_changes_the_signature(make_client):
    baseline = await collect_in_isolation(make_client)

    changed = await collect_in_isolation(
        make_client, embedder=FakeEmbedder(dimension=16, signature="other-model/16/raw/none-v1")
    )

    assert changed["index_signature"] != baseline["index_signature"]
    assert changed["revision"] == baseline["revision"]


async def test_settings_that_do_not_change_the_vectors_keep_the_signature(make_client, settings):
    """성능 튜닝이 전면 재색인을 유발하면 아무도 그 값을 만지지 못한다."""
    baseline = await collect_in_isolation(make_client)

    tuned = await collect_in_isolation(
        make_client,
        settings=settings.model_copy(
            update={
                "embedding_batch_size": 1,
                "max_upload_bytes": 4096,
                "ingestion_concurrency": 5,
            }
        ),
    )

    assert tuned["index_signature"] == baseline["index_signature"]


async def test_stored_chunks_carry_the_signature_from_the_response(client, vector_store):
    created = await post(client)

    chunks = vector_store.chunks_of(created["document_id"])
    assert chunks
    assert {chunk.index_signature for chunk in chunks} == {created["index_signature"]}


async def test_the_same_bytes_are_reindexed_after_the_configuration_changes(
    make_client, settings, vector_store
):
    """`unchanged` 단축이 `revision` 만 봤다면 여기서 아무 일도 일어나지 않는다."""
    async with make_client() as client:
        first = await post(client)

    resized = settings.model_copy(update={"chunk_size": 150, "chunk_overlap": 30})
    async with make_client(settings=resized) as client:
        again = await post(client)

    assert again["status"] == "reindexed"
    assert again["revision"] == first["revision"]
    assert again["index_signature"] != first["index_signature"]
    assert again["previous_revision"] is None  # 내용은 그대로다 — "이전 리비전"이 없다
    document_id = first["document_id"]
    assert (
        await vector_store.count_chunks(document_id, index_signature=first["index_signature"]) == 0
    )
    assert (
        await vector_store.count_chunks(document_id, index_signature=again["index_signature"])
        == again["chunk_count"]
    )


# ── 재기동 ──────────────────────────────────────────────────────────────
#
# `ASGITransport` 는 lifespan 을 돌리지 않으므로 위 테스트들은 기동 훅을 타지 않는다.
# 기동 정리가 앱에 **실제로 배선되어 있는지**는 여기서만 드러난다.


async def test_restarting_with_the_same_configuration_keeps_the_chunks(make_app, vector_store):
    async with booted(make_app()) as client:
        created = await post(client)

    async with booted(make_app()) as client:
        detail = (await client.get(f"/documents/{created['document_id']}")).json()

    assert detail["chunk_count"] == created["chunk_count"]
    assert detail["index_status"] == "indexed"
    assert await vector_store.count_chunks(created["document_id"]) == created["chunk_count"]


async def test_restarting_with_a_changed_configuration_marks_the_document_stale(
    make_app, settings, vector_store
):
    """원본 바이트를 보관하지 않으므로 자동 재색인은 불가능하다 — 지우고 드러낸다."""
    async with booted(make_app()) as client:
        created = await post(client)

    resized = settings.model_copy(update={"chunk_size": 150, "chunk_overlap": 30})
    async with booted(make_app(settings=resized)) as client:
        detail = (await client.get(f"/documents/{created['document_id']}")).json()

        assert await document_ids(client) == [created["document_id"]]
        assert detail["chunk_count"] == 0
        assert detail["index_status"] == "stale"
        assert await vector_store.count_chunks(created["document_id"]) == 0

        # 복구는 재업로드뿐이다.
        recovered = await post(client)
        assert recovered["status"] == "reindexed"
        assert recovered["index_status"] == "indexed"
        assert recovered["chunk_count"] >= 1
        assert await vector_store.count_chunks(created["document_id"]) == recovered["chunk_count"]
