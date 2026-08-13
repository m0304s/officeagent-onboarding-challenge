"""kiwipiepy 어댑터의 계약 — 내용어만 남고 조사·어미·기호는 버려진다.

실물 형태소 분석기라 모델이 필요하다. 검색 계약 테스트(`test_retrieval_*`)는 모델
없는 `RuleTokenizer` 로 돌고, 이 파일만 kiwipiepy 를 실제로 부른다.
"""

import pytest

from app.adapters.tokenizer import KiwiTokenizer


@pytest.fixture(scope="module")
def kiwi() -> KiwiTokenizer:
    return KiwiTokenizer()


def test_particles_and_endings_are_dropped(kiwi: KiwiTokenizer):
    result = set(kiwi.tokenize("재택근무를 합니다"))
    assert "를" not in result
    assert "습니다" not in result


def test_a_noun_survives(kiwi: KiwiTokenizer):
    result = set(kiwi.tokenize("재택근무 규정"))
    assert "재택근무" in result or {"재택", "근무"} & result


def test_a_verb_stem_survives(kiwi: KiwiTokenizer):
    assert "승인" in set(kiwi.tokenize("승인되나요"))


def test_latin_is_casefolded(kiwi: KiwiTokenizer):
    assert "pr" in set(kiwi.tokenize("PR 리뷰"))


def test_a_number_survives(kiwi: KiwiTokenizer):
    assert "200" in set(kiwi.tokenize("200만원"))


def test_write_and_read_share_the_convention(kiwi: KiwiTokenizer):
    """색인('교육비 지원')과 질의('교육비는 얼마인가요')가 같은 토큰에 닿는다."""
    assert set(kiwi.tokenize("교육비 지원")) & set(kiwi.tokenize("교육비는 얼마인가요"))


def test_signature_changes_with_tag_set(kiwi: KiwiTokenizer):
    narrowed = KiwiTokenizer(content_tags=frozenset({"NNG"}))
    assert narrowed.signature_material != kiwi.signature_material


def test_signature_names_the_engine(kiwi: KiwiTokenizer):
    assert "kiwipiepy" in kiwi.signature_material
