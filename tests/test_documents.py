"""문서 식별자·리비전·색인 서명 유도 규칙.

이 세 값이 저장된 벡터의 정체성 전부다. 규칙이 흔들리면 재업로드 교체도 캐시
무효화도 근거를 잃으므로, 각각이 무엇에 반응하고 무엇에 반응하지 **않는지**를 고정한다.
"""

import inspect
from datetime import UTC, datetime

import pytest

from app.core.chunking import CHUNK_STRATEGY_VERSION, ChunkStrategy
from app.core.documents import (
    Chunk,
    ChunkLocation,
    Document,
    DocumentFormat,
    IndexStatus,
    IngestionStatus,
    StoredIndexVersion,
    derive_document_id,
    derive_index_signature,
    derive_revision,
    normalize_filename,
)
from app.core.lexical import DEFAULT_TOKENIZER, Tokenizer

BASE_SIGNATURE_MATERIALS = {
    "embedder_signature": "multilingual-e5-small/384/l2norm/e5-prefix-v1",
    "chunk_strategy": ChunkStrategy.RECURSIVE.value,
    "chunk_strategy_version": CHUNK_STRATEGY_VERSION,
    "chunk_size": 600,
    "chunk_overlap": 100,
    "tokenizer_signature": DEFAULT_TOKENIZER.signature_material,
}


# ── document_id ──────────────────────────────────────────────────────────


def test_same_filename_yields_the_same_document_id():
    """같은 파일명은 조회 없이 같은 문서로 판정되어야 한다."""
    assert derive_document_id("policy.txt") == derive_document_id("policy.txt")


def test_document_id_ignores_case():
    """대소문자만 다른 파일명은 같은 문서다."""
    assert derive_document_id("Policy.TXT") == derive_document_id("policy.txt")


def test_document_id_ignores_unicode_normalization_form():
    """macOS(NFD)와 리눅스(NFC)에서 같은 한글 파일명이 다른 문서가 되면 안 된다."""
    nfc = "사내규정.txt"  # NFC
    nfd = "사내규정.txt".replace("사", "\u1109\u1161").replace("내", "\u1102\u1162")

    assert nfc != nfd, "픽스처가 실제로 서로 다른 정규화 형태여야 한다"
    assert derive_document_id(nfc) == derive_document_id(nfd)


def test_document_id_ignores_client_supplied_path():
    """업로드 파일명에 섞여 오는 경로는 문서 정체성이 아니다."""
    expected = derive_document_id("policy.txt")

    assert derive_document_id("/home/user/policy.txt") == expected
    assert derive_document_id("C:\\Users\\me\\policy.txt") == expected


def test_different_filenames_are_different_documents():
    assert derive_document_id("policy.txt") != derive_document_id("guide.txt")


def test_empty_filename_is_rejected():
    """빈 파일명으로 문서를 만들 수 없다 — 모든 빈 이름이 한 문서로 뭉친다."""
    with pytest.raises(ValueError):
        derive_document_id("   ")


def test_normalize_filename_strips_path_and_folds_case():
    assert normalize_filename("/tmp/Policy.TXT") == "policy.txt"


# ── revision ─────────────────────────────────────────────────────────────


def test_identical_bytes_yield_the_same_revision():
    assert derive_revision(b"hello") == derive_revision(b"hello")


def test_one_byte_difference_changes_the_revision():
    """한 바이트만 달라도 다른 리비전이어야 재업로드가 교체로 인식된다."""
    assert derive_revision(b"hello") != derive_revision(b"hellp")


def test_revision_does_not_depend_on_filename():
    """리비전은 내용의 함수다. 파일명은 document_id 의 몫이다."""
    data = b"same content"
    assert derive_revision(data) == derive_revision(data)


# ── index_signature ──────────────────────────────────────────────────────


def test_same_materials_yield_the_same_signature():
    assert derive_index_signature(**BASE_SIGNATURE_MATERIALS) == derive_index_signature(
        **BASE_SIGNATURE_MATERIALS
    )


@pytest.mark.parametrize(
    ("material", "changed"),
    [
        # 임베더 정체성 네 갈래는 어댑터가 하나의 문자열로 요약해 넘긴다.
        ("embedder_signature", "bge-m3/384/l2norm/e5-prefix-v1"),  # 모델 식별자
        ("embedder_signature", "multilingual-e5-small/768/l2norm/e5-prefix-v1"),  # 차원
        ("embedder_signature", "multilingual-e5-small/384/none/e5-prefix-v1"),  # 정규화
        ("embedder_signature", "multilingual-e5-small/384/l2norm/e5-prefix-v2"),  # 접두사 규약
        ("chunk_strategy", "parent-child"),  # 전략 이름
        ("chunk_strategy_version", CHUNK_STRATEGY_VERSION + 1),  # 전략 버전
        ("chunk_size", 400),
        ("chunk_overlap", 50),
        # 토큰화가 달라지면 어휘 색인의 내용이 달라진다. 하나의 서명이 두 색인을 함께
        # 지배하므로 이 변경도 재색인을 유발해야 한다.
        ("tokenizer_signature", Tokenizer(version=99).signature_material),
        ("tokenizer_signature", Tokenizer(suffixes=("는", "은")).signature_material),
    ],
)
def test_each_material_changes_the_signature(material, changed):
    """재료가 하나라도 달라지면 서명이 달라져야 한다.

    달라지지 않으면 그 구성 변경은 재색인을 유발하지 못하고, 구 구성 벡터가 인덱스에
    남은 채 캐시는 낡은 답변을 계속 내보낸다.
    """
    baseline = derive_index_signature(**BASE_SIGNATURE_MATERIALS)
    variant = derive_index_signature(**{**BASE_SIGNATURE_MATERIALS, material: changed})

    assert variant != baseline


def test_material_boundaries_are_unambiguous():
    """한 재료에 구분자를 흉내 낸 문자가 섞여도 다른 재료로 새어서는 안 된다.

    재료를 `key=value` 줄로 이어 붙이면 값 안의 개행이 구분자와 구별되지 않아, 서로
    다른 구성이 같은 정규 문자열을 만든다. 서명이 같으면 재색인이 일어나지 않으므로
    구 구성 벡터가 그대로 남는다 — 서명이 막으려던 실패가 서명을 통해 돌아온다.
    """
    injected = derive_index_signature(
        **{
            **BASE_SIGNATURE_MATERIALS,
            "embedder_signature": "e\nstrategy=s2",
            "chunk_strategy": "s",
        }
    )
    shifted = derive_index_signature(
        **{
            **BASE_SIGNATURE_MATERIALS,
            "embedder_signature": "e",
            "chunk_strategy": "s2\nstrategy=s",
        }
    )

    assert injected != shifted


def test_signature_does_not_depend_on_document_content_or_filename():
    """서명은 구성의 함수다 — 서로 다른 문서라도 같은 구성이면 같은 서명이다.

    함수가 문서를 아예 받지 않는다는 사실로 이를 고정한다. 내용·파일명이 재료가 되면
    "지금 구성이 저장 당시와 같은가"를 물을 수 없게 된다.
    """
    parameters = set(inspect.signature(derive_index_signature).parameters)

    assert not parameters & {"data", "content", "filename", "document_id", "revision"}


def test_settings_that_do_not_change_vectors_are_not_materials():
    """배치 크기·업로드 상한·동시성 상한은 결과 벡터를 바꾸지 않는다.

    재료로 삼으면 성능 튜닝이 전면 재색인을 유발한다.
    """
    parameters = set(inspect.signature(derive_index_signature).parameters)

    assert not parameters & {
        "embedding_batch_size",
        "max_upload_bytes",
        "ingestion_concurrency",
    }
    assert parameters == set(BASE_SIGNATURE_MATERIALS), "재료 목록이 계약이다"


def test_search_time_settings_are_not_materials():
    """retriever 목록·가중치·후보 깊이·하한·융합 상수는 저장물을 바꾸지 않는다.

    재료로 삼으면 가중치를 조정할 때마다 전면 재색인이 돌아 아무도 조정하지 않게 된다.
    """
    parameters = set(inspect.signature(derive_index_signature).parameters)

    assert not parameters & {
        "retrievers",
        "retriever_weights",
        "candidate_depth",
        "rrf_k",
        "retrieval_min_score",
        "lexical_min_token_rarity",
    }


def test_the_tokenizer_configuration_reaches_the_signature():
    """토큰화 구성이 서명에 닿지 않으면 규칙을 고쳐도 기존 색인이 `stale` 이 되지 않는다.

    그 색인은 옛 규약으로 쓰였고 질의는 새 규약으로 오므로, 어휘 검색이 오류 없이
    아무것도 찾지 못하는 상태로 남는다.
    """
    baseline = derive_index_signature(**BASE_SIGNATURE_MATERIALS)
    retuned = derive_index_signature(
        **{
            **BASE_SIGNATURE_MATERIALS,
            "tokenizer_signature": Tokenizer(
                suffixes=(*DEFAULT_TOKENIZER.suffixes, "께서")
            ).signature_material,
        }
    )

    assert retuned != baseline


def test_signature_is_short_enough_to_embed_in_a_chunk_id():
    signature = derive_index_signature(**BASE_SIGNATURE_MATERIALS)

    assert len(signature) == 16
    assert signature.isalnum()


# ── 값 객체 ──────────────────────────────────────────────────────────────


def test_chunk_id_separates_revisions_and_signatures():
    """재색인은 revision 이 그대로인 채 일어난다.

    id 에 서명이 없으면 새 청크가 이전 청크를 덮어써, "새로 쓰고 → 커밋 → 지우기"
    순서가 4단계에서 이미 깨진다. 되돌릴 원본이 사라지는 것이 실제 피해다.
    """
    location = ChunkLocation(char_start=0, char_end=10)
    common = {"document_id": "doc-1", "revision": "a" * 64, "chunk_index": 0, "text": "본문"}

    old = Chunk(index_signature="1111111111111111", location=location, **common)
    new = Chunk(index_signature="2222222222222222", location=location, **common)

    assert old.id != new.id


def test_chunk_index_is_zero_padded_so_ids_sort_in_document_order():
    location = ChunkLocation(char_start=0, char_end=10)
    common = {
        "document_id": "doc-1",
        "revision": "a" * 64,
        "index_signature": "1111111111111111",
        "text": "본문",
    }

    ids = [Chunk(chunk_index=i, location=location, **common).id for i in (0, 2, 10)]

    assert ids == sorted(ids)


@pytest.mark.parametrize(
    ("start", "end", "page"),
    [(0, 0, None), (5, 3, None), (-1, 3, None), (0, 3, 0)],
)
def test_invalid_locations_are_rejected(start, end, page):
    """위치가 무의미하면 출처 표기가 무의미해진다. 만들 수 없게 막는다."""
    with pytest.raises(ValueError):
        ChunkLocation(char_start=start, char_end=end, page=page)


def test_empty_chunk_is_rejected():
    with pytest.raises(ValueError):
        Chunk(
            document_id="doc-1",
            revision="a" * 64,
            index_signature="1111111111111111",
            chunk_index=0,
            text="",
            location=ChunkLocation(char_start=0, char_end=1),
        )


# ── 레코드가 스스로 하는 판정 ────────────────────────────────────────────
#
# 수집(재색인·`stale` 처리·기동 정리)과 검색(대상 집합·현재성 재검증)이 **같은 축들**을
# 본다. 판정이 두 서비스에 따로 있으면 한쪽만 고쳐진 순간 수집이 유효하다고 여기는
# 문서와 검색이 찾는 문서가 어긋나는데, 그때 **각자 자기 기준으로는 옳아서 어디에도
# 오류가 남지 않는다.** 그래서 판정을 레코드에 두고, 여기서 직접 고정한다.

SIGNATURE = "1111111111111111"
OTHER_SIGNATURE = "2222222222222222"
REVISION = "a" * 64
OTHER_REVISION = "b" * 64


def make_document(**overrides) -> Document:
    defaults = {
        "document_id": "doc-1",
        "filename": "policy.txt",
        "format": DocumentFormat.TXT,
        "revision": REVISION,
        "index_signature": SIGNATURE,
        "chunk_count": 3,
        "byte_size": 100,
        "ingested_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return Document(**{**defaults, **overrides})


def test_a_document_indexed_under_another_configuration_does_not_match():
    """서명이 다르면 벡터가 다른 의미 공간에 있다."""
    document = make_document()

    assert document.matches_index(SIGNATURE)
    assert not document.matches_index(OTHER_SIGNATURE)


def test_a_stale_document_matches_the_index_but_is_not_searchable():
    """기동 정리는 `stale` 문서의 **서명을 그대로 둔다** — 재업로드를 재색인으로 잇기 위해서다.

    그래서 서명만 보는 판정으로는 걸러지지 않는다. 청크가 0개인 문서를 검색 대상에
    넣으면 대상 수만 늘고 결과는 나오지 않아, 빈 결과의 이유를 진단할 수 없게 된다.
    """
    stale = make_document(index_status=IndexStatus.STALE, chunk_count=0)

    assert stale.matches_index(SIGNATURE), "서명은 그대로여야 재업로드가 재색인으로 이어진다"
    assert not stale.is_searchable_under(SIGNATURE)


def test_being_up_to_date_needs_all_three_axes():
    """하나라도 어긋나면 다시 색인해야 한다.

    `revision` 만 보면 모델이나 청킹을 바꾼 뒤 같은 파일을 다시 올려도 아무 일이
    일어나지 않고, 사용자는 재색인했다고 믿는다.
    """
    document = make_document()
    current = {"revision": REVISION, "index_signature": SIGNATURE}

    assert document.is_up_to_date(**current)
    assert not document.is_up_to_date(**{**current, "revision": OTHER_REVISION})
    assert not document.is_up_to_date(**{**current, "index_signature": OTHER_SIGNATURE})
    assert not make_document(index_status=IndexStatus.STALE, chunk_count=0).is_up_to_date(**current)


def test_a_version_carries_all_three_axes_of_the_record():
    """축 하나를 빠뜨려도 타입은 멀쩡하다 — 리비전이 빠지면 지운 문장이 검색된다."""
    document = make_document()

    version = StoredIndexVersion.of(document)

    assert version == StoredIndexVersion(
        document_id="doc-1", revision=REVISION, index_signature=SIGNATURE
    )


def test_versions_of_different_generations_are_different_values():
    """값 객체로 비교하므로 집합 연산이 성립한다 — 기동 정리가 그 성질에 기댄다."""
    document = make_document()

    assert StoredIndexVersion.of(document) != StoredIndexVersion.of(
        make_document(revision=OTHER_REVISION)
    )
    assert len({StoredIndexVersion.of(document), StoredIndexVersion.of(make_document())}) == 1


@pytest.mark.parametrize(
    ("current", "revision", "expected"),
    [
        (None, REVISION, IngestionStatus.CREATED),
        ("same", OTHER_REVISION, IngestionStatus.REPLACED),
        ("same", REVISION, IngestionStatus.REINDEXED),
    ],
    ids=["최초", "내용이 바뀜", "내용은 같음"],
)
def test_the_status_says_what_this_request_did(current, revision, expected):
    """`REINDEXED` 를 `REPLACED` 와 뭉개면 `previous_revision` 이 현재 값과 같아진다.

    응답이 "이전 리비전은 지금 리비전과 같다"고 말하게 되어 자기모순이 된다.
    """
    record = make_document() if current == "same" else None

    assert IngestionStatus.of(record, revision) is expected
