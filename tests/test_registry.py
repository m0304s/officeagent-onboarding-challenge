"""문서 레지스트리 (SQLite).

레지스트리가 답하는 질문은 하나다 — **"이 문서의 지금 유효한 리비전과 색인 서명은
무엇인가."** 그 답이 하나여야 리비전 교체가 원자적이 되므로, 여기서 고정하는 것은
"재커밋이 값을 쌓지 않고 교체한다"와 "프로세스가 죽어도 답이 남아 있다"이다.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.adapters.protocols import DocumentRegistry
from app.adapters.registry import SqliteDocumentRegistry
from app.core.documents import Document, DocumentFormat, IndexStatus
from app.core.exceptions import StorageUnavailable

DOCUMENT_ID = "11111111-2222-3333-4444-555555555555"


def make_document(**overrides) -> Document:
    values = {
        "document_id": DOCUMENT_ID,
        "filename": "company-policy.txt",
        "format": DocumentFormat.TXT,
        "revision": "rev-1",
        "index_signature": "sig-1",
        "chunk_count": 3,
        "byte_size": 1024,
        "ingested_at": datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
    }
    return Document(**{**values, **overrides})


@pytest.fixture
def registry(data_dir: Path) -> SqliteDocumentRegistry:
    return SqliteDocumentRegistry(data_dir / "registry.sqlite3")


def test_the_implementation_satisfies_the_protocol(registry):
    assert isinstance(registry, DocumentRegistry)


# ── 커밋과 조회 ──────────────────────────────────────────────────────────


async def test_a_committed_document_comes_back_whole(registry):
    document = make_document()

    await registry.commit(document)

    assert await registry.get(DOCUMENT_ID) == document


async def test_an_unknown_document_is_none_not_an_error(registry):
    """없는 것과 실패한 것은 다르다. 뭉개면 호출자가 최초 수집을 판별할 수 없다."""
    assert await registry.get("없는-문서") is None


async def test_recommitting_replaces_the_revision_instead_of_accumulating(registry):
    """레코드가 쌓이면 "지금 유효한 리비전"의 답이 둘이 된다."""
    await registry.commit(make_document(revision="rev-1"))
    await registry.commit(make_document(revision="rev-2", chunk_count=7))

    stored = await registry.get(DOCUMENT_ID)
    assert stored.revision == "rev-2"
    assert stored.chunk_count == 7
    assert len(await registry.list_all()) == 1


async def test_recommitting_with_only_the_signature_changed_replaces_the_signature(registry):
    """재색인은 `revision` 이 그대로인 채 일어난다 — 서명만 갈리는 경로가 실재한다."""
    await registry.commit(make_document(revision="rev-1", index_signature="sig-1"))
    await registry.commit(make_document(revision="rev-1", index_signature="sig-2"))

    stored = await registry.get(DOCUMENT_ID)
    assert (stored.revision, stored.index_signature) == ("rev-1", "sig-2")


async def test_the_stale_status_survives_a_commit(registry):
    """기동 정리가 붙인 `stale` 이 조회에서 사라지면 무엇을 다시 올려야 할지 알 수 없다."""
    await registry.commit(make_document(index_status=IndexStatus.STALE, chunk_count=0))

    stored = await registry.get(DOCUMENT_ID)
    assert stored.index_status is IndexStatus.STALE


async def test_listing_returns_every_document(registry):
    await registry.commit(make_document())
    await registry.commit(make_document(document_id="other", filename="guide.md"))

    listed = await registry.list_all()

    assert {document.document_id for document in listed} == {DOCUMENT_ID, "other"}


# ── 삭제 ────────────────────────────────────────────────────────────────


async def test_deleting_returns_the_record_it_removed(registry):
    """호출자는 이 반환값으로 벡터 스토어를 정리한다. 미리 조회하게 하면 그 사이에 값이 바뀐다."""
    document = make_document()
    await registry.commit(document)

    removed = await registry.delete(DOCUMENT_ID)

    assert removed == document
    assert await registry.get(DOCUMENT_ID) is None


async def test_deleting_an_unknown_document_reports_nothing_removed(registry):
    assert await registry.delete("없는-문서") is None


async def test_deleting_leaves_other_documents_alone(registry):
    await registry.commit(make_document())
    await registry.commit(make_document(document_id="other", filename="guide.md"))

    await registry.delete(DOCUMENT_ID)

    assert [document.document_id for document in await registry.list_all()] == ["other"]


# ── 영속성 ──────────────────────────────────────────────────────────────


async def test_the_schema_is_created_on_first_access(data_dir: Path):
    """마이그레이션 단계나 준비 스크립트를 요구하면 "한 줄 실행"이 깨진다."""
    path = data_dir / "nested" / "registry.sqlite3"
    registry = SqliteDocumentRegistry(path)

    assert await registry.get(DOCUMENT_ID) is None  # 스키마가 없으면 여기서 터진다
    assert path.exists()


async def test_the_answer_survives_a_new_process(data_dir: Path):
    """인메모리였다면 재기동 때마다 모든 문서가 최초 수집으로 보인다."""
    path = data_dir / "registry.sqlite3"
    await SqliteDocumentRegistry(path).commit(make_document())

    reopened = SqliteDocumentRegistry(path)

    assert (await reopened.get(DOCUMENT_ID)).revision == "rev-1"


# ── 실패 ────────────────────────────────────────────────────────────────


async def test_storage_failures_surface_as_a_domain_error(data_dir: Path):
    """`sqlite3.OperationalError` 가 라우터까지 새면 내부 경로가 응답에 노출된다."""
    directory = data_dir / "not-a-database"
    directory.mkdir()
    registry = SqliteDocumentRegistry(directory)

    with pytest.raises(StorageUnavailable):
        await registry.get(DOCUMENT_ID)
