"""재정렬의 성질 — 순서·결정성·집합 보존, 그리고 쓸 수 없는 점수의 처분.

여기서 보는 것은 정책이지 모델이 아니다. 점수를 손으로 주는 이유는 "관련 있는 것이 위로
오는가"가 모델의 성질이라 실물로만 확인되기 때문이다.
"""

import pytest

from app.core.documents import ChunkLocation, DocumentFormat
from app.core.fusion import Contribution
from app.core.reranking import reorder, targets
from app.core.retrieval import ScoredChunk


def make(document_id: str = "doc-1", chunk_index: int = 0, score: float = 0.5) -> ScoredChunk:
    return ScoredChunk(
        document_id=document_id,
        revision="rev-1",
        index_signature="sig-1",
        chunk_index=chunk_index,
        text=f"{document_id}#{chunk_index}",
        location=ChunkLocation(char_start=0, char_end=20),
        filename="company-policy.txt",
        format=DocumentFormat.TXT,
        score=score,
    )


def identities(chunks) -> list[tuple[str, int]]:
    return [(chunk.document_id, chunk.chunk_index) for chunk in chunks]


class TestOrderFollowsTheRerankScore:
    def test_highest_score_comes_first(self):
        chunks = [make(chunk_index=0), make(chunk_index=1), make(chunk_index=2)]

        ordered = reorder(chunks, [0.1, 0.9, 0.4], depth=3)

        assert identities(ordered) == [("doc-1", 1), ("doc-1", 2), ("doc-1", 0)]
        assert [chunk.rerank_score for chunk in ordered] == [0.9, 0.4, 0.1]

    def test_ties_break_on_identity_not_on_input_order(self):
        """입력을 정체성 역순으로 준다 — 안정 정렬이 답을 대신 맞히지 못하게."""
        chunks = [make("doc-2", 1), make("doc-2", 0), make("doc-1", 5)]

        ordered = reorder(chunks, [0.5, 0.5, 0.5], depth=3)

        assert identities(ordered) == [("doc-1", 5), ("doc-2", 0), ("doc-2", 1)]

    def test_the_same_input_gives_the_same_order_every_time(self):
        """동점이 잦은 자리라 결정성을 반복으로 확인한다 — 캐시가 이 성질 위에 선다."""
        chunks = [make("doc-2", 0), make("doc-1", 1), make("doc-1", 0), make("doc-2", 1)]
        scores = [0.7, 0.7, 0.7, 0.2]

        orders = {tuple(identities(reorder(chunks, scores, depth=4))) for _ in range(10)}

        assert len(orders) == 1


class TestReorderingNeverFilters:
    def test_every_candidate_survives(self):
        chunks = [make(chunk_index=index) for index in range(5)]

        ordered = reorder(chunks, [0.01] * 5, depth=5)

        assert len(ordered) == 5
        assert set(identities(ordered)) == set(identities(chunks))

    def test_candidates_beyond_the_depth_keep_the_fusion_order_at_the_back(self):
        """깊이 밖을 버리면 재검증이 앞을 떨어뜨렸을 때 결과가 K 보다 짧아진다."""
        chunks = [make(chunk_index=index, score=1.0 - index / 10) for index in range(5)]

        ordered = reorder(chunks, [0.1, 0.9], depth=2)

        assert identities(ordered[:2]) == [("doc-1", 1), ("doc-1", 0)]
        assert identities(ordered[2:]) == identities(chunks[2:])

    def test_candidates_beyond_the_depth_carry_no_rerank_score(self):
        chunks = [make(chunk_index=index) for index in range(3)]

        ordered = reorder(chunks, [0.9], depth=1)

        assert ordered[0].rerank_score == 0.9
        assert [chunk.rerank_score for chunk in ordered[1:]] == [None, None]

    def test_an_empty_result_stays_empty(self):
        assert reorder([], [], depth=30) == []


class TestTheFusionScoreSurvives:
    def test_score_and_contributions_ride_through_the_reorder(self):
        """두 값이 서로 다른 질문에 답한다 — 하나를 덮으면 "왜 이 순위인가" 의 절반이 사라진다."""
        credits = (Contribution(retriever="dense", rank=1, native_score=0.91),)
        chunk = ScoredChunk(
            document_id="doc-1",
            revision="rev-1",
            index_signature="sig-1",
            chunk_index=0,
            text="교육비는 연 200만원까지 지원됩니다.",
            location=ChunkLocation(char_start=0, char_end=20),
            filename="company-policy.txt",
            format=DocumentFormat.TXT,
            score=1.0,
            contributions=credits,
        )

        ordered = reorder([chunk], [0.42], depth=1)

        assert ordered[0].score == 1.0
        assert ordered[0].contributions == credits
        assert ordered[0].rerank_score == 0.42


class TestUnusableScoresStopHere:
    @pytest.mark.parametrize("scores", [[0.1], [0.1, 0.2, 0.3]])
    def test_a_score_count_that_does_not_match_is_an_error(self, scores):
        """어긋난 채로 짝지으면 남의 점수가 실린다 — 예외가 아니라 조용한 오답이다."""
        chunks = [make(chunk_index=0), make(chunk_index=1)]

        with pytest.raises(ValueError, match="점수 개수"):
            reorder(chunks, scores, depth=2)

    def test_nan_is_an_error(self):
        """NaN 은 비교가 전부 거짓이라 순서를 임의로 만든다."""
        chunks = [make(chunk_index=0), make(chunk_index=1)]

        with pytest.raises(ValueError, match="NaN"):
            reorder(chunks, [0.5, float("nan")], depth=2)


class TestTargetsIsTheFusedHead:
    def test_it_takes_the_top_of_the_fused_list(self):
        chunks = [make(chunk_index=index) for index in range(5)]

        assert identities(targets(chunks, depth=2)) == [("doc-1", 0), ("doc-1", 1)]

    def test_fewer_candidates_than_the_depth_is_normal(self):
        """후보가 깊이보다 적은 것은 하한이 걸러 낸 정상 경로다."""
        chunks = [make(chunk_index=0)]

        assert len(targets(chunks, depth=30)) == 1
