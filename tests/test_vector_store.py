"""벡터 스토어 (Chroma) — 쓰기·삭제·집계·질의. 실물 서버가 필요하다.

무게가 실린 단언 둘: 같은 리비전의 서로 다른 서명 청크가 공존하는가, 빈 대상 목록이
전체 검색으로 뒤집히지 않는가. 근거는 `tests/README.md` 에 있다.
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
    """테스트마다 자기 컬렉션을 쓴다.

    서버가 하나라 공유하면 한 테스트의 잔여물이 다음 테스트의 개수 단언을 깨뜨린다."""
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


#: 질의 테스트가 쓰는 기준 벡터. `make_query_vectors` 의 첫 벡터와 방향이 같다.
QUERY_VECTOR = [1.0, 0.0, 0.0, 0.0]


def make_query_vectors(count: int, *, offset: float = 0.0) -> list[list[float]]:
    """`QUERY_VECTOR` 에서 뒤로 갈수록 멀어지는 벡터.

    첫 벡터가 영벡터인 `make_vectors` 는 질의에 쓸 수 없다 — 코사인 거리가 정의되지 않는다."""
    return [[1.0, 0.3 * (index + offset), 0.0, 0.0] for index in range(count)]


async def store_chunks(store: ChromaVectorStore, chunks, *, vectors=None, **kwargs) -> None:
    await store.add_chunks(
        chunks,
        make_vectors(len(chunks)) if vectors is None else vectors,
        filename=kwargs.get("filename", "company-policy.txt"),
        document_format=kwargs.get("document_format", DocumentFormat.TXT),
    )


def version_of(
    document_id: str = DOCUMENT_ID, revision: str = "rev-1", index_signature: str = "sig-1"
) -> StoredIndexVersion:
    return StoredIndexVersion(
        document_id=document_id, revision=revision, index_signature=index_signature
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

    쓰는 도중에 이미 이전 벡터가 사라져, 실패해도 되돌릴 원본이 없다."""
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
    """되돌리기가 두 번 불려도 실패하지 않는다 — 지울 것이 없는 것은 오류가 아니다."""
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


# ── 질의 ────────────────────────────────────────────────────────────────


async def test_a_stored_chunk_comes_back_for_a_query(store):
    chunks = make_chunks(3)
    await store_chunks(store, chunks, vectors=make_query_vectors(3))

    results = await store.query(QUERY_VECTOR, top_k=5, versions=[version_of()])

    assert [result.text for result in results] == [chunk.text for chunk in chunks]
    assert all(0 <= result.native_score <= 1 for result in results)


async def test_the_result_count_is_capped_by_top_k(store):
    """`top_k` 가 저장소 부하이자 응답 크기다 — 넘겨도 조용히 무시되면 상한이 무의미해진다."""
    await store_chunks(store, make_chunks(3), vectors=make_query_vectors(3))

    results = await store.query(QUERY_VECTOR, top_k=2, versions=[version_of()])

    assert len(results) == 2


async def test_results_are_ordered_by_descending_score(store):
    """벡터를 뒤로 갈수록 멀게 만들어 두었으므로 순서가 미리 정해져 있다."""
    await store_chunks(store, make_chunks(4), vectors=make_query_vectors(4))

    results = await store.query(QUERY_VECTOR, top_k=4, versions=[version_of()])

    scores = [result.native_score for result in results]
    assert scores == sorted(scores, reverse=True)
    assert [result.chunk_index for result in results] == [0, 1, 2, 3]


async def test_a_version_outside_the_target_set_is_not_returned(store):
    """교체 뒤 이전 세대 정리가 실패해 남은 청크가 이 경로로 새어 나간다."""
    await store_chunks(store, make_chunks(2, revision="rev-1"), vectors=make_query_vectors(2))
    await store_chunks(
        store, make_chunks(2, revision="rev-2"), vectors=make_query_vectors(2, offset=5)
    )

    results = await store.query(QUERY_VECTOR, top_k=10, versions=[version_of(revision="rev-2")])

    assert len(results) == 2
    assert {result.revision for result in results} == {"rev-2"}


async def test_an_empty_target_set_returns_nothing_rather_than_everything(store):
    """"조건 없음 = 전체"로 뒤집히면 사용자가 지운 문서가 검색된다.

    잔여 청크를 저장해 둔 채로 확인한다 — 그 상태가 정확히 이 실수의 모양이다."""
    await store_chunks(store, make_chunks(3), vectors=make_query_vectors(3))

    assert await store.query(QUERY_VECTOR, top_k=10, versions=[]) == []


async def test_the_same_revision_under_another_signature_is_not_mixed_in(store):
    """재색인은 `revision` 이 그대로인 채 일어난다 — 서명 축이 빠지면 두 세대가 섞인다."""
    await store_chunks(
        store, make_chunks(2, index_signature="sig-1"), vectors=make_query_vectors(2)
    )
    await store_chunks(
        store, make_chunks(2, index_signature="sig-2"), vectors=make_query_vectors(2, offset=5)
    )

    results = await store.query(
        QUERY_VECTOR, top_k=10, versions=[version_of(index_signature="sig-1")]
    )

    assert len(results) == 2
    assert {result.index_signature for result in results} == {"sig-1"}


async def test_a_query_spans_every_target_in_the_set(store):
    """대상이 여럿이면 `$or` 로 묶인다 — 하나만 통과하면 나머지 문서가 통째로 사라진다."""
    await store_chunks(store, make_chunks(1), vectors=make_query_vectors(1))
    await store_chunks(
        store, make_chunks(1, document_id=OTHER_ID), vectors=make_query_vectors(1, offset=2)
    )

    results = await store.query(
        QUERY_VECTOR, top_k=10, versions=[version_of(), version_of(document_id=OTHER_ID)]
    )

    assert {result.document_id for result in results} == {DOCUMENT_ID, OTHER_ID}


async def test_the_result_carries_the_source_metadata(store):
    """출처 표기가 이 값들을 그대로 읽는다. 하나라도 빠지면 "어느 문서의 어디"를 말할 수 없다."""
    await store_chunks(
        store,
        make_chunks(1, page=3),
        vectors=make_query_vectors(1),
        filename="handbook.pdf",
        document_format=DocumentFormat.PDF,
    )

    result = (await store.query(QUERY_VECTOR, top_k=1, versions=[version_of()]))[0]

    assert result.document_id == DOCUMENT_ID
    assert result.revision == "rev-1"
    assert result.index_signature == "sig-1"
    assert result.chunk_index == 0
    assert result.filename == "handbook.pdf"
    assert result.format is DocumentFormat.PDF
    assert (result.location.char_start, result.location.char_end) == (0, 50)
    assert result.location.page == 3


async def test_a_page_less_format_comes_back_without_a_page(store):
    """수집이 값 없는 키를 넣지 않는다는 규약의 반대편이다 — 없는 키에서도 터지지 않는다."""
    await store_chunks(store, make_chunks(1), vectors=make_query_vectors(1))

    result = (await store.query(QUERY_VECTOR, top_k=1, versions=[version_of()]))[0]

    assert result.location.page is None
    assert result.format is DocumentFormat.TXT


async def test_a_failing_query_becomes_a_domain_error(store):
    """빈 결과로 위장하면 벡터 스토어가 죽은 동안 "근거를 찾지 못했습니다"가 나간다."""
    await store_chunks(store, make_chunks(1), vectors=make_query_vectors(1))

    with pytest.raises(StorageUnavailable):
        # 차원이 다른 질의 벡터 — 저장소가 거절한다.
        await store.query([0.5] * (DIMENSION + 4), top_k=1, versions=[version_of()])


# ── 컬렉션은 차원마다 나뉜다 ────────────────────────────────────────────


def test_the_collection_name_carries_the_dimension():
    assert collection_for(384) != collection_for(768)


async def test_a_collection_keeps_its_dimension_after_being_emptied(collection_name: str):
    """`collection_for` 가 존재하는 이유를 실물로 고정한다 — 비워도 차원이 남는다.

    이름을 나누지 않으면 "재업로드하면 복구된다"는 약속이 차원 교체에서 거짓이 된다."""
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
    """서버 모드에서 영속성은 서버(와 볼륨)의 책임이다.

    어댑터 쪽에서 확인할 수 있는 것은 상태를 프로세스 안에 들고 있지 않다는 것뿐이다."""
    await store_chunks(
        ChromaVectorStore(VECTOR_STORE_URL, collection_name=collection_name), make_chunks(2)
    )

    reopened = ChromaVectorStore(VECTOR_STORE_URL, collection_name=collection_name)

    assert await reopened.count_chunks(DOCUMENT_ID) == 2
