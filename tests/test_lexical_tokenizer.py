"""어휘 토크나이저의 계약 (`lexical-index` 스펙 「조사·어미가 붙은 질의도 어근에 닿는다」).

토큰화가 어긋나면 색인은 오류 없이 빈 목록만 낸다 — 하이브리드 검색이 꺼진 것과 구별되지
않는 실패라, 규약 자체를 여기서 단언한다.
"""

from app.core.lexical import DEFAULT_TOKENIZER, MIN_STEM_LENGTH, RuleTokenizer, Tokenizer


def tokens(text: str, tokenizer: Tokenizer = DEFAULT_TOKENIZER) -> set[str]:
    return set(tokenizer.tokenize(text))


class TestParticlesReachTheStem:
    def test_a_query_with_a_particle_shares_a_token_with_the_indexed_text(self):
        """`"교육비 지원"` 청크가 `"교육비는 얼마인가요"` 질의에 닿아야 한다."""
        assert "교육비" in tokens("교육비 지원") & tokens("교육비는 얼마인가요")

    def test_the_original_token_survives_alongside_the_stem(self):
        """규칙 기반이라 과벗김이 나온다 — 원형이 남아 있으면 최악이 재현율 증가로 끝난다."""
        assert {"교육비는", "교육비"} <= tokens("교육비는")

    def test_a_stem_shorter_than_the_floor_is_not_stripped(self):
        stripped = DEFAULT_TOKENIZER.strip_suffix("수도")

        assert stripped is None
        assert MIN_STEM_LENGTH == 2
        assert tokens("수도") == {"수도"}

    def test_the_longest_matching_suffix_wins(self):
        assert DEFAULT_TOKENIZER.strip_suffix("서울에서") == "서울"


class TestIdentifiersStayWhole:
    def test_a_letter_digit_identifier_is_one_unit(self):
        """`P1` 이 `p` 와 `1` 로 쪼개지면 색인 거의 전체가 그 토큰을 갖는다."""
        assert "p1" in tokens("P1 장애")
        assert "v2" in tokens("v2 배포")

    def test_a_mixed_token_keeps_both_the_whole_form_and_its_pieces(self):
        result = tokens("연 200만원까지 지원")

        assert {"200만원까지", "200만원", "200", "만원"} <= result

    def test_a_query_for_the_whole_form_matches_the_indexed_text(self):
        assert "200만원" in tokens("교육비는 200만원까지") & tokens("200만원을 지원한다")

    def test_underscores_separate_identifier_words(self):
        assert {"chunk", "index"} <= tokens("chunk_index")


class TestNormalizationAbsorbsSurfaceDifferences:
    def test_case_does_not_block_a_match(self):
        assert tokens("P1") == tokens("p1")

    def test_full_width_characters_normalize_to_half_width(self):
        assert tokens("Ｐ１") == tokens("p1")

    def test_punctuation_is_a_boundary_not_a_token(self):
        assert tokens("오늘 서울 날씨 어때?") == tokens("오늘, 서울! 날씨 - 어때")


class TestTokenizationIsDeterministic:
    def test_the_same_input_yields_the_same_sequence(self):
        text = "P1 장애가 나면 교육비는 200만원까지 지원됩니다."

        assert DEFAULT_TOKENIZER.tokenize(text) == DEFAULT_TOKENIZER.tokenize(text)

    def test_repetition_is_preserved_for_the_frequency_term(self):
        assert DEFAULT_TOKENIZER.tokenize("연차 연차").count("연차") == 2


class TestSignatureMaterialTracksConfiguration:
    def test_the_same_configuration_yields_the_same_material(self):
        assert RuleTokenizer().signature_material == RuleTokenizer().signature_material

    def test_a_different_version_yields_different_material(self):
        assert (
            RuleTokenizer(version=1).signature_material
            != RuleTokenizer(version=2).signature_material
        )

    def test_a_different_suffix_list_yields_different_material(self):
        """접미 목록을 고치면 어절이 다른 토큰으로 갈려 기존 색인이 질의에 닿지 않는다."""
        trimmed = RuleTokenizer(suffixes=("는", "은"))

        assert trimmed.signature_material != RuleTokenizer().signature_material

    def test_the_material_is_a_stable_string(self):
        assert isinstance(DEFAULT_TOKENIZER.signature_material, str)
        assert "version" in DEFAULT_TOKENIZER.signature_material
