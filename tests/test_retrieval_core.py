"""검색 결과 값 객체의 불변식.

값 객체가 스스로 지키는 불변식이 있어야, 어댑터의 변환 버그(거리→유사도 클램프 누락,
지표 방향 오독)가 상위 계층의 임계값 비교를 조용히 무의미하게 만들기 전에 걸린다.
"""

import pytest

from app.core.documents import ChunkLocation, DocumentFormat
from app.core.retrieval import ScoredChunk


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


class TestScoreIsASimilarityInUnitRange:
    @pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
    def test_accepts_the_whole_range(self, score):
        assert make(score=score).score == score

    @pytest.mark.parametrize("score", [-0.1, 1.1])
    def test_rejects_values_outside_the_range(self, score):
        """정의역을 벗어난 점수는 유사도가 아니다 — 거리를 그대로 흘렸다는 신호다."""
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
