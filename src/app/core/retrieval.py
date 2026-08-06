"""검색 결과 값 객체.

`adapters/` 가 아니라 `core/` 인 이유는 import 방향이다 — 상위 계층이 소비하는 타입을
어댑터에 두면 계층이 역전된다. 표준 라이브러리만 쓴다.
"""

from dataclasses import dataclass

from app.core.documents import ChunkLocation, DocumentFormat
from app.core.fusion import Contribution

#: `Contribution` 은 융합이 만드는 값이라 정의가 `core/fusion.py` 에 있다. 여기서 다시
#: 내보내는 이유는 소비자가 결과 타입과 그 안의 내역을 한 모듈에서 가져오게 하려는 것이다.
__all__ = ["Contribution", "RetrievedChunk", "ScoredChunk"]


@dataclass(frozen=True)
class ScoredChunk:
    """검색이 돌려주는 청크 하나 — 정체성·본문·출처·융합 점수·기여 내역.

    `score` 는 유사도가 아니라 여러 retriever 가 얼마나 같은 것을 꼽았는가다."""

    document_id: str
    revision: str
    index_signature: str
    chunk_index: int
    text: str
    location: ChunkLocation
    filename: str
    format: DocumentFormat
    score: float
    contributions: tuple[Contribution, ...] = ()

    def __post_init__(self) -> None:
        if self.chunk_index < 0:
            raise ValueError("chunk_index 는 0 이상이어야 한다")
        # 융합은 어느 목록에도 없는 항목을 만들지 않으므로 원점수가 항상 양수다. `0` 이나
        # `1` 초과가 나오면 정규화 분모가 입력 목록 집합과 어긋났다는 신호다.
        if not 0 < self.score <= 1:
            raise ValueError("score 는 0 보다 크고 1 이하인 융합 점수여야 한다")


@dataclass(frozen=True)
class RetrievedChunk:
    """retriever 하나가 자기 척도로 매긴 청크 하나 — 융합에 들어가는 입력.

    척도가 retriever 마다 달라 `score` 자리에 담으면 `(0, 1]` 이 뜻을 잃는다."""

    document_id: str
    revision: str
    index_signature: str
    chunk_index: int
    text: str
    location: ChunkLocation
    filename: str
    format: DocumentFormat
    native_score: float
    #: 이 retriever 의 관련성 하한을 넘었는가. 통과하지 못한 항목도 목록에 남는다 — 빼면
    #: 남은 항목의 순위가 당겨져 융합이 볼 교집합이 좁아진다. 기본값은 하한 없는 retriever 용.
    gate_passed: bool = True

    def __post_init__(self) -> None:
        if self.chunk_index < 0:
            raise ValueError("chunk_index 는 0 이상이어야 한다")
