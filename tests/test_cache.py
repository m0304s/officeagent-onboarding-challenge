"""캐시 도메인 — 질의 정규화, 항목 지문 유도, 유사 매치 판정.

I/O 도 비동기도 없어 `pytest` 한 줄에서 Redis 없이 돈다. 키가 무엇에 반응하고 질의를
복원 가능하게 담지 않는지가 축이다 (묶음별 근거는 `tests/README.md`).
"""

import unicodedata

import pytest

from app.core.answers import Answer, Citation, FinishReason
from app.core.cache import (
    CachedAnswer,
    CacheLayer,
    CacheLookup,
    best_match,
    cosine_similarity,
    derive_cache_key,
    derive_cache_scope,
    negation_polarity,
    normalize_query,
)
from app.core.documents import ChunkLocation, DocumentFormat
from app.core.retrieval import ScoredChunk

KEY_MATERIALS = {
    "query": "교육비 지원 한도가 얼마인가요?",
    "top_k": 5,
    "prompt_version": "qa-ko-1",
    "index_signature": "a1b2c3d4e5f60718",
    "model": "gpt-5-codex",
}


def chunk(**overrides) -> ScoredChunk:
    fields = {
        "document_id": "doc-1",
        "revision": "rev-1",
        "index_signature": "sig-1",
        "chunk_index": 0,
        "text": "교육비는 연 200만원까지 지원됩니다.",
        "location": ChunkLocation(char_start=120, char_end=540),
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


# ── 질의 정규화 ──────────────────────────────────────────────────────────


def test_normalization_collapses_surrounding_and_repeated_whitespace():
    """복사·붙여넣기가 만드는 공백 차이는 다른 질문이 아니다."""
    assert normalize_query("  교육비   지원  한도는?  ") == normalize_query("교육비 지원 한도는?")


def test_normalization_folds_case():
    """`RAG` 와 `rag` 를 다른 질문으로 세면 영문 섞인 질의가 매번 미스가 된다."""
    assert normalize_query("What is RAG?") == normalize_query("what is rag?")


def test_normalization_unifies_unicode_composition():
    """macOS(NFD)와 리눅스(NFC)에서 온 같은 한글 질문이 다른 항목이 되면 매번 미스가 된다."""
    nfc = "연차 규정"
    nfd = unicodedata.normalize("NFD", nfc)

    assert nfc != nfd, "픽스처가 실제로 서로 다른 정규화 형태여야 한다"
    assert normalize_query(nfd) == normalize_query(nfc)


def test_normalization_keeps_meaning_bearing_characters():
    """조사·어미·부정 표지를 건드리면 정확 매치가 의미를 판정하기 시작한다."""
    assert normalize_query("환불이 안 되나요?") != normalize_query("환불이 되나요?")


# ── 키의 정체성 ──────────────────────────────────────────────────────────


def test_key_ignores_whitespace_and_case():
    """정규화가 키에 실제로 적용되는지 — 호출부가 정규화를 잊어도 같은 키여야 한다."""
    noisy = derive_cache_key(**{**KEY_MATERIALS, "query": "  교육비 지원 한도가   얼마인가요?  "})

    assert noisy == derive_cache_key(**KEY_MATERIALS)


def test_key_ignores_unicode_composition():
    """정규화 형태만 다른 질문은 같은 항목이다."""
    decomposed = unicodedata.normalize("NFD", KEY_MATERIALS["query"])

    assert derive_cache_key(**{**KEY_MATERIALS, "query": decomposed}) == derive_cache_key(
        **KEY_MATERIALS
    )


@pytest.mark.parametrize(
    ("material", "other"),
    [
        ("query", "재택근무 규정이 어떻게 되나요?"),
        ("top_k", 3),
        ("prompt_version", "qa-ko-2"),
        ("index_signature", "ffffffffffffffff"),
        ("model", "gpt-5"),
    ],
)
def test_key_changes_when_any_single_material_changes(material, other):
    """다섯 재료 중 하나만 달라도 다른 항목이어야 한다 (`response-cache` 스펙의 표).

    K 나 프롬프트나 모델을 바꾼 뒤 이전 세대의 답이 새 구성의 답인 척 남는 경로를 막는다."""
    assert derive_cache_key(**{**KEY_MATERIALS, material: other}) != derive_cache_key(
        **KEY_MATERIALS
    )


def test_key_does_not_confuse_material_boundaries():
    """값 경계가 모호한 직렬화면 재료를 옮겨 담은 두 구성이 같은 키를 받는다."""
    shifted = derive_cache_key(
        **{**KEY_MATERIALS, "prompt_version": "qa-ko-1x", "index_signature": "a1b2c3d4e5f6071"}
    )

    assert shifted != derive_cache_key(**KEY_MATERIALS)


def test_key_does_not_expose_the_query():
    """키가 질의를 복원 가능한 형태로 담으면 `KEYS` 와 로그에 질문이 그대로 노출된다."""
    key = derive_cache_key(**KEY_MATERIALS)

    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    for fragment in ("교육비", "얼마", "지원"):
        assert fragment not in key
    assert normalize_query(KEY_MATERIALS["query"]) not in key


def test_key_rejects_unresolved_top_k():
    """`top_k` 를 유도하지 않은 채 키를 만들면 요청마다 항목이 갈린다 (design 결정 14)."""
    with pytest.raises(ValueError):
        derive_cache_key(**{**KEY_MATERIALS, "top_k": 0})

    with pytest.raises(TypeError):
        derive_cache_key(**{**KEY_MATERIALS, "top_k": None})


# ── 부정 극성 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "연차를 이월할 수 없나요?",
        "연차를 이월하면 안 되나요?",
        "재택근무가 불가능한가요?",
        "이월이 금지되어 있나요?",
        "쓰지 않으면 어떻게 되나요?",
        "승인 없이는 못 하나요?",
        "그건 연차가 아닌가요?",
        "can i not carry over my leave?",
        "is it never allowed?",
        "can't i carry it over?",
    ],
)
def test_a_negated_question_is_marked_negative(query):
    """부정 한 글자 차이는 코사인이 잡지 못한다 — 이 축이 없으면 정반대 답이 히트로 나간다."""
    assert negation_polarity(normalize_query(query)) is True


@pytest.mark.parametrize(
    "query",
    [
        "연차를 이월할 수 있나요?",
        "교육비 지원 안내를 알려주세요",
        "보안 규정은 무엇인가요?",
        "can i carry over my leave?",
        "안전 교육은 언제인가요?",
    ],
)
def test_a_plain_question_is_not_marked_negative(query):
    """`안내`·`안전` 은 `안` 으로 시작할 뿐이다 — 앞머리만 보고 세면 긍정문이 갈린다."""
    assert negation_polarity(normalize_query(query)) is False


def test_the_polarity_does_not_distinguish_which_negation_was_used():
    """세분하면 같은 뜻의 두 질의가 갈려 히트를 잃는다 — 그 손실은 미스이고, 막는 것은 오답이다."""
    assert negation_polarity(normalize_query("휴가를 안 쓰면 어떻게 되나요?")) is negation_polarity(
        normalize_query("휴가를 쓰지 않으면 어떻게 되나요?")
    )


# ── 후보 집합 ────────────────────────────────────────────────────────────


def scope_materials(**overrides) -> dict:
    dropped = ("query", "top_k")
    fields = {name: value for name, value in KEY_MATERIALS.items() if name not in dropped}
    return {**fields, **overrides}


def test_scope_ignores_the_query():
    """유사 매치는 질의가 다른 항목을 찾는 층이라, 질의가 후보 집합을 가르면 그 층이 할 일을
    잃는다."""
    assert derive_cache_scope(**scope_materials()) == derive_cache_scope(**scope_materials())


def test_scope_ignores_the_top_k():
    """K 가 달라도 같은 질문이다. 후보 집합을 K 로 쪼개면 유사 매치가 볼 이웃만 줄어든다."""
    assert derive_cache_key(**KEY_MATERIALS) != derive_cache_key(**{**KEY_MATERIALS, "top_k": 3})
    assert derive_cache_scope(**scope_materials()) == derive_cache_scope(**scope_materials())


@pytest.mark.parametrize(
    ("material", "other"),
    [
        ("prompt_version", "qa-ko-2"),
        ("index_signature", "ffffffffffffffff"),
        ("model", "gpt-5"),
    ],
)
def test_scope_splits_on_every_material_that_changes_the_answer(material, other):
    """프롬프트·색인 세대·모델이 다른 항목이 유사도만으로 히트가 되면 그 셋이 뜻을 잃는다."""
    assert derive_cache_scope(**scope_materials(**{material: other})) != derive_cache_scope(
        **scope_materials()
    )


def test_scope_is_not_a_cache_key():
    """둘이 같은 재료를 쓰므로 값이 겹치면 후보 집합 이름이 항목 지문 자리에 들어간다."""
    assert derive_cache_scope(**scope_materials()) not in derive_cache_key(**KEY_MATERIALS)


# ── 캐시 항목 ────────────────────────────────────────────────────────────


def test_entry_reports_one_version_per_document():
    """같은 문서의 청크 여럿이 태그와 재검증 대상을 부풀리면 무효화가 관계없는 항목까지 지운다."""
    item = entry(sources=(chunk(chunk_index=0), chunk(chunk_index=1), chunk(document_id="doc-2")))

    assert item.document_ids == ("doc-1", "doc-2")
    assert {version.document_id for version in item.source_versions} == {"doc-1", "doc-2"}


def test_entry_records_the_revision_it_answered_from():
    """리비전이 없으면 재검증이 "지금도 현재인가"를 물을 기준을 잃는다."""
    (version,) = entry(sources=(chunk(revision="rev-7"),)).source_versions

    assert (version.document_id, version.revision) == ("doc-1", "rev-7")


def test_no_evidence_entry_has_no_source_versions():
    """근거 0건 항목은 태그도 재검증 대상도 없다 — 부정 집합이 유일한 방어다."""
    item = entry(answer=Answer.no_evidence(), sources=())

    assert item.source_versions == () and item.document_ids == ()


def test_entry_keeps_sources_that_were_never_cited():
    """인용만 담으면 인용되지 않은 근거가 낡아도 무효화 대상에서 빠진다."""
    cited = chunk(document_id="doc-1")
    uncited = chunk(document_id="doc-2")
    answer = Answer(
        text="연 200만원입니다. [1]",
        finish_reason=FinishReason.STOP,
        citations=(Citation.of(1, cited),),
    )

    assert entry(answer=answer, sources=(cited, uncited)).document_ids == ("doc-1", "doc-2")


# ── 조회 결과 ────────────────────────────────────────────────────────────


def test_miss_carries_neither_layer_nor_similarity():
    """미스가 층이나 유사도를 들면 응답이 히트로 보인다."""
    lookup = CacheLookup.miss()

    assert not lookup.hit
    assert lookup.entry is None and lookup.layer is None and lookup.similarity is None


def test_exact_hit_has_no_similarity():
    """정확 매치는 유사도를 계산하지 않는다 — 값이 실리면 운영자가 임계값을 오독한다."""
    lookup = CacheLookup.exact("fp-1", entry())

    assert lookup.hit and lookup.layer is CacheLayer.EXACT
    assert lookup.similarity is None


def test_semantic_hit_carries_the_similarity_used_to_judge():
    """임계값 조정 근거가 응답 밖에만 있으면 운영자가 로그를 파야 한다."""
    lookup = CacheLookup.semantic("fp-1", entry(), 0.9612)

    assert lookup.hit and lookup.layer is CacheLayer.SEMANTIC
    assert lookup.similarity == pytest.approx(0.9612)


def test_hit_identifies_the_entry_it_came_from():
    """지문이 없으면 재검증에서 버린 항목을 지울 수 없다 — 유사 매치는 저장소만 안다."""
    assert CacheLookup.semantic("fp-7", entry(), 0.95).fingerprint == "fp-7"
    assert CacheLookup.miss().fingerprint is None


@pytest.mark.parametrize(
    "fields",
    [
        {"layer": CacheLayer.EXACT},
        {"similarity": 0.99},
        {"entry": entry()},
        {"entry": entry(), "layer": CacheLayer.EXACT},
        {"fingerprint": "fp-1"},
    ],
)
def test_lookup_rejects_half_built_hits(fields):
    """층이나 지문이 빠진 항목은 응답에서 서로를 부정한다."""
    with pytest.raises(ValueError):
        CacheLookup(**fields)


def test_lookup_rejects_similarity_on_exact_layer():
    """정확 매치에 유사도가 실리면 두 층의 구분이 무의미해진다."""
    with pytest.raises(ValueError):
        CacheLookup(entry=entry(), layer=CacheLayer.EXACT, similarity=1.0, fingerprint="fp-1")


# ── 유사 매치 판정 ───────────────────────────────────────────────────────


def test_identical_vectors_are_maximally_similar():
    assert cosine_similarity([0.6, 0.8], [0.6, 0.8]) == pytest.approx(1.0)


def test_similarity_ignores_magnitude():
    """길이가 아니라 방향을 재는 값이라 정규화되지 않은 벡터도 같은 판정을 받는다."""
    assert cosine_similarity([1.0, 0.0], [4.0, 0.0]) == pytest.approx(1.0)


def test_orthogonal_vectors_score_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_zero_vector_never_becomes_a_hit():
    """영벡터에는 방향이 없다 — 예외로 요청을 깨는 대신 임계값 아래로 떨어뜨린다."""
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


@pytest.mark.parametrize(
    ("left", "right"),
    [([1.0, 0.0], [1.0, 0.0, 0.0]), ([], [1.0]), ([1.0], [])],
)
def test_similarity_rejects_incomparable_vectors(left, right):
    """차원이 갈린 것은 임베더 세대가 갈렸다는 신호라, 조용히 0 을 돌려주면 그 신호가 묻힌다."""
    with pytest.raises(ValueError):
        cosine_similarity(left, right)


def test_best_match_picks_the_closest_candidate():
    """후보가 여럿이면 유사도가 가장 높은 하나다."""
    match = best_match(
        [1.0, 0.0],
        [("far", [0.0, 1.0]), ("near", [0.99, 0.14]), ("closer", [1.0, 0.02])],
        threshold=0.9,
    )

    assert match is not None and match.fingerprint == "closer"


def test_best_match_rejects_candidates_below_the_threshold():
    """임계값 미달은 미스다 — 이 값이 "얼마나 틀려도 되는가"를 정한다."""
    assert best_match([1.0, 0.0], [("other", [0.7, 0.71])], threshold=0.93) is None


def test_best_match_accepts_the_threshold_itself():
    """경계값이 히트인지 미스인지가 설정의 뜻을 정한다 — 임계값 이상이 히트다."""
    candidate = [0.93, (1 - 0.93**2) ** 0.5]

    match = best_match([1.0, 0.0], [("edge", candidate)], threshold=0.93)

    assert match is not None and match.similarity == pytest.approx(0.93)


def test_best_match_on_empty_candidates_is_a_miss():
    """캐시가 비어 있는 상태(평가자의 첫 실행)에서도 조회가 성립한다."""
    assert best_match([1.0, 0.0], [], threshold=0.93) is None


def test_best_match_keeps_the_first_of_equally_close_candidates():
    """동점에서 후보 순서가 판정을 정한다 — 최신순으로 훑으므로 최신이 남는다."""
    match = best_match([1.0, 0.0], [("newer", [1.0, 0.0]), ("older", [2.0, 0.0])], threshold=0.9)

    assert match is not None and match.fingerprint == "newer"
