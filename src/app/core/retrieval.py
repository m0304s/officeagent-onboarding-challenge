"""검색 결과 값 객체.

`adapters/` 가 아니라 `core/` 인 이유는 import 방향이다 — 상위 계층이 소비하는 타입을
어댑터에 두면 계층이 역전된다. 표준 라이브러리만 쓴다.
"""

from dataclasses import dataclass

from app.core.documents import ChunkLocation, DocumentFormat


@dataclass(frozen=True)
class ScoredChunk:
    """검색이 돌려주는 청크 하나 — 정체성·본문·출처·점수.

    `score` 는 거리가 아니라 유사도다. 변환을 어댑터에 가두려는 것이다."""

    document_id: str
    revision: str
    index_signature: str
    chunk_index: int
    text: str
    location: ChunkLocation
    filename: str
    format: DocumentFormat
    score: float

    def __post_init__(self) -> None:
        if self.chunk_index < 0:
            raise ValueError("chunk_index 는 0 이상이어야 한다")
        # 어댑터의 거리→유사도 변환이 어긋나면 상위 계층의 임계값 비교가 조용히
        # 무의미해진다. 그 전에 여기서 걸린다.
        if not 0 <= self.score <= 1:
            raise ValueError("score 는 0 과 1 사이의 유사도여야 한다")


@dataclass(frozen=True)
class RetrievedChunk:
    """retriever 하나가 자기 척도로 매긴 청크 하나 — 융합에 들어가는 입력.

    척도가 retriever 마다 달라 `score` 자리에 담으면 `[0, 1]` 이 뜻을 잃는다."""

    document_id: str
    revision: str
    index_signature: str
    chunk_index: int
    text: str
    location: ChunkLocation
    filename: str
    format: DocumentFormat
    native_score: float

    def __post_init__(self) -> None:
        if self.chunk_index < 0:
            raise ValueError("chunk_index 는 0 이상이어야 한다")
