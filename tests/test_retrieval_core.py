"""검색 결과 값 객체의 불변식과, 관련성 하한이 걸리는 지점.

하한이 여기 있는 이유는 그것이 값이 아니라 순서에 관한 성질이기 때문이다 — 융합 앞이냐
뒤냐는 점수 하나로는 알 수 없고, 한 retriever 의 하한을 극단까지 올려야 관측된다.
"""

import pytest

from app.core.documents import ChunkLocation, DocumentFormat
from app.core.fusion import Contribution
from app.core.retrieval import ScoredChunk
from tests.retrieval_harness import GUIDE, POLICY, make_harness

#: 두 문서 어디에도 없는 어휘로만 이루어진 질의. 대역의 어휘 색인이 토큰 교집합으로
#: 매기므로 겹치는 토큰이 없으면 빈 목록이 된다 — 실물의 변별력 판정과 같은 결과다.
NO_OVERLAP_QUERY = "quokka wombat"

#: 문서에 그대로 적힌 식별자. 어휘 쪽만 확실히 찾아내는 질의를 만드는 데 쓴다.
IDENTIFIER_QUERY = "P1"

HYBRID = ("dense", "lexical")


def make(**overrides) -> ScoredChunk:
    fields = {
        "document_id": "doc-1",
        "revision": "rev-1",
        "index_signature": "sig-1",
        "chunk_index": 0,
        "text": "교육비는 연 200만원까지 지원됩니다.",
        "location": ChunkLocation(char_start=0, char_end=20),
        "filename": "company-policy.txt",
        "format": DocumentFormat.TXT,
        "score": 0.8,
    }
    return ScoredChunk(**{**fields, **overrides})


class TestScoreIsAFusionScoreInUnitRange:
    @pytest.mark.parametrize("score", [1e-9, 0.5, 1.0])
    def test_accepts_the_whole_range(self, score):
        assert make(score=score).score == score

    @pytest.mark.parametrize("score", [-0.1, 0.0, 1.1])
    def test_rejects_values_outside_the_range(self, score):
        """`0` 도 정의역 밖이다 — 융합은 어느 목록에도 없는 항목을 만들지 않는다."""
        with pytest.raises(ValueError, match="score"):
            make(score=score)


class TestChunkIndexIsAPosition:
    def test_rejects_a_negative_index(self):
        with pytest.raises(ValueError, match="chunk_index"):
            make(chunk_index=-1)


class TestResultCarriesItsOwnProvenance:
    def test_identity_location_and_document_facts_round_trip(self):
        """소비자가 인용 한 줄을 만들려고 문서를 다시 조회할 필요가 없어야 한다."""
        result = make(chunk_index=3, location=ChunkLocation(char_start=10, char_end=40, page=2))

        assert (result.document_id, result.revision, result.index_signature) == (
            "doc-1",
            "rev-1",
            "sig-1",
        )
        assert result.chunk_index == 3
        assert (result.location.char_start, result.location.char_end, result.location.page) == (
            10,
            40,
            2,
        )
        assert result.filename == "company-policy.txt"
        assert result.format is DocumentFormat.TXT


class TestContributionsRideAlong:
    def test_the_credit_of_each_retriever_survives_assembly(self):
        """가중치 조정의 근거가 이 내역뿐이다 — 융합 점수만으로는 "왜 1위"에 답할 수 없다."""
        credits = (
            Contribution(retriever="dense", rank=1, native_score=0.91),
            Contribution(retriever="lexical", rank=4, native_score=3.7),
        )

        result = make(contributions=credits)

        assert result.contributions == credits
        assert {credit.retriever for credit in result.contributions} == {"dense", "lexical"}

    def test_defaults_to_empty_so_unrelated_consumers_need_not_know(self):
        """프롬프트 조립과 인용은 내역을 보지 않는다 — 그 층의 테스트를 묶지 않는다."""
        assert make().contributions == ()


# ── 하한은 융합 앞에서, 각자의 단위로 (6.5) ──────────────────────────────


async def test_the_dense_floor_is_applied_before_fusion():
    """하한을 넘긴 청크만 밀집 목록에 실린다 — 융합 뒤에 걸면 척도가 이미 사라졌다.

    관측 방법은 기여 내역이다. 걸러진 자리가 융합 뒤였다면 그 흔적이 내역에 남는다."""
    harness = make_harness(retrievers=HYBRID)
    await harness.ingest("policy.txt", POLICY)
    await harness.ingest("guide.md", GUIDE)
    floor = await _floor_between_the_top_two_dense_scores(harness)

    result = await harness.searching_with(min_score=floor).search(IDENTIFIER_QUERY)

    dense_credits = [
        credit
        for chunk in result.chunks
        for credit in chunk.contributions
        if credit.retriever == "dense"
    ]
    assert dense_credits, "밀집 기여가 하나도 없어 단언이 공허해졌다"
    assert all(credit.native_score >= floor for credit in dense_credits)


async def test_a_dense_floor_of_one_still_leaves_the_lexical_results():
    """밀집을 완전히 막아도 어휘 목록은 남는다 — 하한이 공용이 아니라는 증거다."""
    harness = make_harness(retrievers=HYBRID)
    await harness.ingest("guide.md", GUIDE)

    result = await harness.searching_with(min_score=1.0).search(IDENTIFIER_QUERY)

    assert result.count > 0, "밀집 하한이 어휘 결과까지 지웠다"
    assert result.retrievers == HYBRID, "실패가 아니므로 두 이름 모두 남아야 한다"
    credits = {credit.retriever for chunk in result.chunks for credit in chunk.contributions}
    assert credits == {"lexical"}


async def test_failing_both_floors_empties_the_results_without_an_error():
    """양쪽 판정을 모두 통과하지 못하면 `200` 과 빈 결과다 — 거절 문구를 만들지 않는다."""
    harness = make_harness(retrievers=HYBRID)
    await harness.ingest("guide.md", GUIDE)

    result = await harness.searching_with(min_score=1.0).search(NO_OVERLAP_QUERY)

    assert result.count == 0
    assert result.chunks == ()
    assert result.target_documents == 1, "하한이 대상 집합까지 지우면 안 된다"


async def _floor_between_the_top_two_dense_scores(harness) -> float:
    """1위와 2위의 밀집 원점수 사이의 하한.

    상수로 박으면 분포가 조금만 움직여도 단언이 공허해지고, 그때도 테스트는 초록이다."""
    dense_only = harness.searching_with(min_score=0.0, retrievers=("dense",))
    everything = await dense_only.search(IDENTIFIER_QUERY)
    scores = [chunk.contributions[0].native_score for chunk in everything.chunks]
    assert len(scores) >= 2
    return (scores[0] + scores[1]) / 2
