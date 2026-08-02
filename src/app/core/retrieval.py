"""검색 도메인 — 결과 값 객체.

검색 결과 하나가 **스스로 출처를 말한다.** 어댑터가 저장소 응답에서 조립하고 서비스와
API 가 그대로 소비하며, 인용을 만들 때 추가 조회가 필요 없다. 여기서 정하는 모양이 곧
답변의 출처 표기 능력의 상한이다.

`adapters/`가 아니라 `core/`에 두는 이유는 import 방향이다. 서비스와 API 가 이 타입을
소비하는데 어댑터 모듈에 두면 상위 계층이 `adapters/`를 import 하게 되어 계층이 역전된다.
`TextSegment`·`ProbeResult`와 같은 배치다.

`core/`는 표준 라이브러리만 쓴다 — pydantic 도 쓰지 않는다.
"""

from dataclasses import dataclass

from app.core.documents import ChunkLocation, DocumentFormat


@dataclass(frozen=True)
class ScoredChunk:
    """검색이 돌려주는 청크 하나 — 정체성·본문·출처·점수.

    `Chunk`(저장 단위)에 **점수와 문서 표시 정보**(`filename`·`format`)가 더 붙은 모양이다.
    두 타입을 합치지 않는 이유는 저장 시점에 점수가 존재하지 않고, 파일명·포맷은 청크가
    아니라 문서의 사실이라 벡터마다 들고 있을 값이 아니기 때문이다. 그럼에도 결과에
    싣는 것은 소비자가 인용 한 줄을 만들려고 문서를 다시 조회하지 않게 하기 위해서다.

    **`score`는 유사도다 — 클수록 가깝다.** 저장소가 돌려주는 거리(작을수록 가깝다)는
    어댑터가 `[0, 1]` 유사도로 바꿔 넣는다. 그 변환이 어댑터 안에만 있어야 저장소를
    바꿔 거리 지표가 달라져도 상위 계층의 비교 방향이 뒤집히지 않는다.

    `location`은 `ChunkLocation`을 그대로 재사용한다. 수집이 저장한 위치가 검색 결과에
    같은 모양으로 되돌아와야 출처가 원문의 같은 자리를 가리킨다.
    """

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
        # 범위를 값 객체가 스스로 지킨다. 어댑터의 거리→유사도 변환이 어긋나면 —
        # 클램프를 빠뜨렸거나 지표 방향을 잘못 읽었거나 — 그 버그가 상위 계층의 임계값
        # 비교를 조용히 무의미하게 만들기 전에 여기서 걸린다.
        if not 0 <= self.score <= 1:
            raise ValueError("score 는 0 과 1 사이의 유사도여야 한다")
