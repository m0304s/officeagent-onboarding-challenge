"""리랭킹 어댑터 — 로딩 없이 서명을 읽는가, 점수 규약이 새지 않는가, 선언을 지키는가.

실제 모델을 쓰는 단언은 파일 끝의 통합 테스트뿐이고 가중치가 없으면 스킵한다. 나머지는
대역 모델로 돈다 — `pytest` 한 줄이 2.2GB 다운로드에 묶이지 않게.
"""

import pytest

from app.adapters.protocols import Reranker
from app.adapters.reranking import (
    KNOWN_RERANKER_PROFILES,
    SCORE_CONVENTION,
    CrossEncoderReranker,
    RerankerProfile,
)
from app.adapters.reranking.local import _identity
from app.core.exceptions import ConfigurationError
from tests.conftest import RERANKER_MODEL as DEFAULT_MODEL
from tests.conftest import needs_reranker_weights
from tests.stubs import FakeReranker

KOREAN_QUERY = "교육비는 얼마까지 지원되나요?"
RELEVANT = "교육비는 연 200만원까지 지원합니다. 신청은 인사팀에 합니다."
IRRELEVANT = "P1 장애가 발생하면 30분 안에 원인을 파악합니다."


class StubModel:
    """`CrossEncoder` 자리에 들어가는 대역 — 정해진 로짓을 순서대로 돌려준다."""

    def __init__(self, logits, *, num_labels: int = 1, window: int | None = 8192) -> None:
        self.activation_fn = _identity
        self.num_labels = num_labels
        if window is not None:
            self.max_seq_length = window
        self._logits = logits
        self.calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs, show_progress_bar=False):
        self.calls.append(list(pairs))
        return self._logits


def loaded(logits, **kwargs) -> CrossEncoderReranker:
    """가중치 대신 대역을 끼운 리랭커. 로딩 경로를 건너뛴다."""
    reranker = CrossEncoderReranker(DEFAULT_MODEL)
    reranker._model = StubModel(logits, **kwargs)
    return reranker


# ── 프로토콜 준수 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reranker",
    [FakeReranker(), CrossEncoderReranker(DEFAULT_MODEL)],
    ids=["fake", "cross-encoder"],
)
def test_implementations_satisfy_the_protocol(reranker):
    """페이크가 프로토콜을 벗어나면 페이크로 통과한 테스트가 실물에서 깨진다."""
    assert isinstance(reranker, Reranker)


# ── 서명 — 캐시 항목의 재료 ──────────────────────────────────────────────


class TestTheSignatureIsReadableWithoutLoading:
    def test_reading_it_does_not_load_the_weights(self):
        """서명은 캐시 조회가 검색보다 앞이라 그 전에 필요하다 — 읽기가 2.2GB 를 올리는 순간
        지연 로딩이 무의미해진다."""
        reranker = CrossEncoderReranker(DEFAULT_MODEL)

        assert reranker.signature
        assert reranker.max_input_tokens == 8192
        assert reranker._model is None, "속성을 읽었을 뿐인데 모델이 올라왔다"

    def test_it_carries_the_name_the_revision_and_the_convention(self):
        reranker = CrossEncoderReranker(DEFAULT_MODEL)
        revision = KNOWN_RERANKER_PROFILES[DEFAULT_MODEL].revision

        assert reranker.signature == f"{DEFAULT_MODEL}@{revision}/{SCORE_CONVENTION}"

    def test_a_different_revision_of_the_same_name_is_a_different_signature(self):
        """이름이 같아도 가중치가 다르면 다른 순서를 낸다 — 캐시가 그것을 구별해야 한다."""
        pinned = CrossEncoderReranker(DEFAULT_MODEL)
        moved = CrossEncoderReranker(DEFAULT_MODEL, revision="0" * 40)

        assert pinned.signature != moved.signature

    def test_a_different_model_is_a_different_signature(self, monkeypatch):
        monkeypatch.setitem(
            KNOWN_RERANKER_PROFILES,
            "other-org/other-reranker",
            RerankerProfile(max_input_tokens=512, revision="abc123"),
        )

        assert (
            CrossEncoderReranker(DEFAULT_MODEL).signature
            != CrossEncoderReranker("other-org/other-reranker").signature
        )


class TestUnknownModelsStopStartup:
    def test_a_model_outside_the_table_is_a_configuration_error(self):
        """입력 창을 모르면 질의+청크가 조용히 잘리는 구성을 걸러낼 수 없다."""
        with pytest.raises(ConfigurationError) as failure:
            CrossEncoderReranker("cross-encoder/ms-marco-MiniLM-L-6-v2")

        assert "cross-encoder/ms-marco-MiniLM-L-6-v2" in str(failure.value)
        assert DEFAULT_MODEL in str(failure.value), "사용 가능한 이름을 알려 주지 않는다"


# ── 채점 ────────────────────────────────────────────────────────────────


class TestScoringKeepsTheOrderAndTheShape:
    async def test_scores_match_the_candidates_one_for_one(self):
        reranker = loaded([-2.0, 3.0, 0.0])

        scores = await reranker.rerank(KOREAN_QUERY, ["가", "나", "다"])

        assert len(scores) == 3
        assert scores[1] > scores[2] > scores[0], "시그모이드가 단조가 아니다"

    async def test_every_score_lands_between_zero_and_one(self):
        reranker = loaded([-1000.0, -0.5, 0.0, 0.5, 1000.0])

        scores = await reranker.rerank(KOREAN_QUERY, list("abcde"))

        assert all(0.0 <= score <= 1.0 for score in scores)
        assert scores == sorted(scores), "정렬은 어댑터의 일이 아니지만 단조는 지켜야 한다"

    async def test_extreme_logits_do_not_overflow(self):
        """큰 음수에서 `exp` 가 넘치는 자리라 두 갈래로 계산한다."""
        reranker = loaded([-800.0, 800.0])

        assert await reranker.rerank(KOREAN_QUERY, ["가", "나"]) == [0.0, 1.0]

    async def test_the_same_input_gives_the_same_scores(self):
        reranker = loaded([0.3, -0.3])

        first = await reranker.rerank(KOREAN_QUERY, ["가", "나"])
        second = await reranker.rerank(KOREAN_QUERY, ["가", "나"])

        assert first == second

    async def test_the_query_is_paired_with_every_candidate(self):
        """크로스인코더의 전부가 이 쌍이다 — 질의가 빠지면 그냥 문서 분류기가 된다."""
        reranker = loaded([0.1, 0.2])

        await reranker.rerank(KOREAN_QUERY, [RELEVANT, IRRELEVANT])

        assert reranker._model.calls == [[(KOREAN_QUERY, RELEVANT), (KOREAN_QUERY, IRRELEVANT)]]

    async def test_no_candidates_means_no_model_call(self):
        reranker = CrossEncoderReranker(DEFAULT_MODEL)

        assert await reranker.rerank(KOREAN_QUERY, []) == []
        assert reranker._model is None, "빈 목록이 2.2GB 로딩을 유발했다"


# ── 선언 검증 ────────────────────────────────────────────────────────────


class TestTheDeclarationIsCheckedAgainstTheRealModel:
    def test_a_matching_model_passes(self):
        reranker = CrossEncoderReranker(DEFAULT_MODEL)

        reranker._assert_matches_declaration(StubModel([], window=8192))

    def test_a_narrower_window_stops_loading(self):
        """선언보다 좁으면 청크 뒷부분이 조용히 잘린 채로 점수가 나온다."""
        reranker = CrossEncoderReranker(DEFAULT_MODEL)

        with pytest.raises(ConfigurationError, match="입력 창"):
            reranker._assert_matches_declaration(StubModel([], window=512))

    def test_an_unreadable_window_stops_loading(self):
        """읽지 못한 것을 통과시키면 검증이 있는 척만 하게 된다."""
        reranker = CrossEncoderReranker(DEFAULT_MODEL)

        with pytest.raises(ConfigurationError, match="입력 창"):
            reranker._assert_matches_declaration(StubModel([], window=None))

    def test_a_multi_label_model_stops_loading(self):
        """라벨이 둘이면 `predict` 가 값이 아니라 벡터를 돌려준다."""
        reranker = CrossEncoderReranker(DEFAULT_MODEL)

        with pytest.raises(ConfigurationError, match="라벨"):
            reranker._assert_matches_declaration(StubModel([], num_labels=2))

    def test_an_activation_we_did_not_set_stops_loading(self):
        """라이브러리 기본 시그모이드가 남아 있으면 우리 시그모이드가 두 번 씌워진다."""
        reranker = CrossEncoderReranker(DEFAULT_MODEL)
        model = StubModel([])
        model.activation_fn = None

        with pytest.raises(ConfigurationError, match="활성화"):
            reranker._assert_matches_declaration(model)


# ── 실물 (가중치가 있을 때만) ────────────────────────────────────────────


@needs_reranker_weights
async def test_the_real_model_puts_the_answer_above_the_unrelated_chunk():
    """한국어에서 실제로 판정이 되는가 — 다국어 모델을 고른 이유가 이 단언이다."""
    reranker = CrossEncoderReranker(DEFAULT_MODEL)

    relevant, irrelevant = await reranker.rerank(KOREAN_QUERY, [RELEVANT, IRRELEVANT])

    assert relevant > irrelevant


@needs_reranker_weights
async def test_the_real_model_warms_up():
    """기동 시점에 도는 경로라, 여기서 깨지면 컨테이너가 매번 경고를 남기며 뜬다."""
    reranker = CrossEncoderReranker(DEFAULT_MODEL)

    await reranker.warm_up()

    assert reranker._model is not None
    assert reranker._model.activation_fn is _identity, "라이브러리 활성화가 남아 있다"
