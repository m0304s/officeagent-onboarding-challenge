"""벡터 스토어 (Chroma) — 쓰기·삭제·집계.

가장 중요한 단언은 **같은 `revision` 의 서로 다른 서명 청크가 공존한다**는 것이다.
재색인은 `revision` 이 그대로인 채 일어나므로, 청크 id 에 서명이 없으면 새 청크가
이전 청크를 그 자리에서 덮어쓴다. 그러면 "새로 쓰고 → 커밋 → 지우기" 순서가 무너져
쓰는 도중에 이미 이전 벡터가 사라지고, 실패해도 되돌릴 원본이 없다.

질의는 검증하지 않는다 — `query` 는 이 어댑터에 없다(retrieval change 의 몫).

**실물 서버가 필요하다.** Chroma 를 서버 모드로 쓰므로 이 파일은 대역으로 대체할 수 없다 —
대역으로 바꾸는 순간 확인하려는 것(우리 메타데이터·필터·id 규약이 *실제 Chroma* 에서
성립하는가)이 사라진다. 서버가 없으면 파일 전체를 건너뛰고, 나머지 스위트는 그대로 돈다.
서버를 띄우는 명령은 `make vector-store` 다.
"""

import asyncio
from uuid import uuid4

import pytest

from app.adapters.protocols import VectorStore
from app.adapters.vector_store import ChromaVectorStore, collection_for
from app.adapters.vector_store.client import create_client, parse_url
from app.core.documents import (
    Chunk,
    ChunkLocation,
    DocumentFormat,
    StoredIndexVersion,
)
from app.core.exceptions import StorageUnavailable
from tests.conftest import VECTOR_STORE_URL, needs_vector_store

pytestmark = needs_vector_store

DOCUMENT_ID = "doc-1"
OTHER_ID = "doc-2"
DIMENSION = 4


def _drop(collection_name: str) -> None:
    """정리. 컬렉션을 한 번도 안 만든 테스트도 있으므로 없는 것은 넘어간다."""
    try:
        create_client(parse_url(VECTOR_STORE_URL)).delete_collection(collection_name)
    except Exception:  # noqa: BLE001 - 정리 실패가 테스트 결과를 뒤집으면 안 된다
        pass


@pytest.fixture
async def collection_name():
    """테스트마다 **자기 컬렉션**을 쓴다.

    서버는 하나뿐이라 임시 디렉터리 같은 격리가 없다. 컬렉션을 공유하면 테스트 순서가
    결과를 바꾸고, 한 테스트의 잔여물이 다음 테스트의 개수 단언을 깨뜨린다.
    """
    name = f"test_{uuid4().hex}"
    yield name
    await asyncio.to_thread(_drop, name)


@pytest.fixture
def store(collection_name: str) -> ChromaVectorStore:
    return ChromaVectorStore(VECTOR_STORE_URL, collection_name=collection_name)


def make_chunks(
    count: int = 2,
    *,
    document_id: str = DOCUMENT_ID,
    revision: str = "rev-1",
    index_signature: str = "sig-1",
    page: int | None = None,
) -> tuple[Chunk, ...]:
    return tuple(
        Chunk(
            document_id=document_id,
            revision=revision,
            index_signature=index_signature,
            chunk_index=index,
            text=f"{index}번째 청크 본문입니다.",
            location=ChunkLocation(char_start=index * 100, char_end=index * 100 + 50, page=page),
        )
        for index in range(count)
    )


def make_vectors(count: int) -> list[list[float]]:
    return [[float(index)] * DIMENSION for index in range(count)]


async def store_chunks(store: ChromaVectorStore, chunks, **kwargs) -> None:
    await store.add_chunks(
        chunks,
        make_vectors(len(chunks)),
        filename=kwargs.get("filename", "company-policy.txt"),
        document_format=kwargs.get("document_format", DocumentFormat.TXT),
    )


def test_the_implementation_satisfies_the_protocol(store):
    assert isinstance(store, VectorStore)


# ── 쓰기와 집계 ──────────────────────────────────────────────────────────


async def test_every_stored_chunk_is_counted(store):
    await store_chunks(store, make_chunks(5))

    assert await store.count_chunks() == 5
    assert await store.count_chunks(DOCUMENT_ID) == 5


async def test_counting_narrows_by_revision_and_signature(store):
    await store_chunks(store, make_chunks(2, revision="rev-1"))
    await store_chunks(store, make_chunks(3, revision="rev-2"))

    assert await store.count_chunks(DOCUMENT_ID, revision="rev-1") == 2
    assert await store.count_chunks(DOCUMENT_ID, revision="rev-2") == 3
    assert await store.count_chunks(DOCUMENT_ID) == 5


async def test_the_metadata_round_trips(store):
    """출처 표기의 재료다. 하나라도 빠지면 "어느 문서의 어디"를 말할 수 없다."""
    chunks = make_chunks(1, page=3)

    await store_chunks(store, chunks, filename="handbook.pdf", document_format=DocumentFormat.PDF)

    stored = store._get_collection().get(include=["metadatas", "documents"])
    metadata = stored["metadatas"][0]
    assert metadata == {
        "document_id": DOCUMENT_ID,
        "revision": "rev-1",
        "index_signature": "sig-1",
        "filename": "handbook.pdf",
        "format": "pdf",
        "chunk_index": 0,
        "char_start": 0,
        "char_end": 50,
        "page": 3,
    }
    assert stored["documents"][0] == chunks[0].text


async def test_a_page_less_format_omits_the_key_entirely(store):
    """널 메타데이터는 허용되지 않고, 센티널 값을 쓰면 소비자가 그 규약을 알아야 한다."""
    await store_chunks(store, make_chunks(1))

    metadata = store._get_collection().get(include=["metadatas"])["metadatas"][0]
    assert "page" not in metadata


async def test_a_vector_count_mismatch_is_refused(store):
    """조용히 잘라 넣으면 청크 일부가 벡터 없이 사라지고 뒤늦게 개수 불일치로만 드러난다."""
    with pytest.raises(ValueError):
        await store.add_chunks(
            make_chunks(3),
            make_vectors(2),
            filename="company-policy.txt",
            document_format=DocumentFormat.TXT,
        )


async def test_storing_nothing_is_not_an_error(store):
    await store_chunks(store, ())

    assert await store.count_chunks() == 0


# ── 두 세대가 공존한다 ───────────────────────────────────────────────────


async def test_the_same_revision_under_two_signatures_coexists(store):
    """id 에 서명이 없으면 재색인이 이전 청크를 그 자리에서 덮어쓴다.

    그러면 "새로 쓰고 → 커밋 → 지우기" 순서가 성립하지 않는다 — 쓰는 도중에 이미
    이전 벡터가 사라지고, 실패해도 되돌릴 원본이 없다.
    """
    await store_chunks(store, make_chunks(2, revision="rev-1", index_signature="sig-1"))
    await store_chunks(store, make_chunks(2, revision="rev-1", index_signature="sig-2"))

    assert await store.count_chunks(DOCUMENT_ID) == 4
    assert await store.count_chunks(DOCUMENT_ID, index_signature="sig-1") == 2
    assert await store.count_chunks(DOCUMENT_ID, index_signature="sig-2") == 2


# ── 삭제 ────────────────────────────────────────────────────────────────


async def test_deleting_one_revision_leaves_the_other(store):
    """교체 순서의 마지막 단계다. 넓게 지우면 방금 쓴 새 리비전이 함께 사라진다."""
    await store_chunks(store, make_chunks(2, revision="rev-1"))
    await store_chunks(store, make_chunks(3, revision="rev-2"))

    removed = await store.delete_document(DOCUMENT_ID, revision="rev-1")

    assert removed == 2
    assert await store.count_chunks(DOCUMENT_ID) == 3
    assert await store.count_chunks(DOCUMENT_ID, revision="rev-2") == 3


async def test_deleting_one_signature_leaves_the_other(store):
    await store_chunks(store, make_chunks(2, index_signature="sig-1"))
    await store_chunks(store, make_chunks(2, index_signature="sig-2"))

    removed = await store.delete_document(DOCUMENT_ID, index_signature="sig-1")

    assert removed == 2
    assert await store.count_chunks(DOCUMENT_ID, index_signature="sig-2") == 2


async def test_deleting_a_document_removes_every_combination(store):
    """문서 삭제는 `revision`·서명이 무엇이든 전부 지운다 — 잔여물이 남으면 삭제가 거짓이 된다."""
    await store_chunks(store, make_chunks(2, revision="rev-1", index_signature="sig-1"))
    await store_chunks(store, make_chunks(2, revision="rev-2", index_signature="sig-2"))

    removed = await store.delete_document(DOCUMENT_ID)

    assert removed == 4
    assert await store.count_chunks(DOCUMENT_ID) == 0


async def test_deleting_leaves_other_documents_alone(store):
    await store_chunks(store, make_chunks(2))
    await store_chunks(store, make_chunks(3, document_id=OTHER_ID))

    await store.delete_document(DOCUMENT_ID)

    assert await store.count_chunks(OTHER_ID) == 3


async def test_deleting_nothing_reports_zero(store):
    """되돌리기가 두 번 불려도 실패하면 안 된다 — 지울 것이 없는 것은 오류가 아니다."""
    assert await store.delete_document("없는-문서") == 0


# ── 저장된 조합 목록 (기동 정리용) ───────────────────────────────────────


async def test_stored_versions_report_every_combination(store):
    """레지스트리만 보면 잔여 청크의 존재 자체를 알 수 없다."""
    await store_chunks(store, make_chunks(2, revision="rev-1", index_signature="sig-1"))
    await store_chunks(store, make_chunks(2, revision="rev-2", index_signature="sig-1"))
    await store_chunks(store, make_chunks(1, document_id=OTHER_ID, index_signature="sig-2"))

    assert await store.list_stored_versions() == [
        StoredIndexVersion(DOCUMENT_ID, "rev-1", "sig-1"),
        StoredIndexVersion(DOCUMENT_ID, "rev-2", "sig-1"),
        StoredIndexVersion(OTHER_ID, "rev-1", "sig-2"),
    ]


async def test_stored_versions_are_empty_on_a_fresh_store(store):
    assert await store.list_stored_versions() == []


# ── 컬렉션은 차원마다 나뉜다 ────────────────────────────────────────────


def test_the_collection_name_carries_the_dimension():
    assert collection_for(384) != collection_for(768)


async def test_a_collection_keeps_its_dimension_after_being_emptied(collection_name: str):
    """`collection_for` 가 존재하는 이유를 실물로 고정한다.

    컬렉션을 비워도 차원이 남는다. 이름을 나누지 않으면, 차원이 다른 모델로 바꾼 뒤
    기동 정리가 청크를 전부 지워도 재업로드가 영구히 실패한다 — "재업로드하면 복구된다"는
    약속이 그 순간 거짓이 된다. Chroma 의 동작이 바뀌면 여기서 알게 된다.
    """
    store = ChromaVectorStore(VECTOR_STORE_URL, collection_name=collection_name)
    await store_chunks(store, make_chunks(1))
    await store.delete_document(DOCUMENT_ID)
    assert await store.count_chunks() == 0

    with pytest.raises(StorageUnavailable):
        await store.add_chunks(
            make_chunks(1),
            [[0.5] * (DIMENSION + 4)],  # 차원이 다른 모델로 바꾼 상황
            filename="company-policy.txt",
            document_format=DocumentFormat.TXT,
        )


# ── 서버가 진실의 원천이다 ──────────────────────────────────────────────


async def test_a_new_client_sees_what_another_wrote(collection_name: str):
    """서버 모드에서 영속성은 서버(와 그 볼륨)의 책임이다.

    어댑터 쪽에서 확인할 수 있는 것은 **상태를 프로세스 안에 들고 있지 않다**는 것뿐이다.
    새 인스턴스가 같은 값을 본다는 사실이 그것을 말한다 — 캐시를 들고 있었다면 여기서
    0이 나온다.
    """
    await store_chunks(
        ChromaVectorStore(VECTOR_STORE_URL, collection_name=collection_name), make_chunks(2)
    )

    reopened = ChromaVectorStore(VECTOR_STORE_URL, collection_name=collection_name)

    assert await reopened.count_chunks(DOCUMENT_ID) == 2
