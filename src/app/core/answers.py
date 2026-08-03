"""답변 도메인 — 종료 사유, 인용, 조립된 답변.

스펙의 두 규칙(빈 답변은 근거 없음뿐, 거절에 인용 없음)을 `Answer.__post_init__` 이
불변식으로 든다. 깨진 응답은 화면에서 정상과 장애가 같아 보여 타입이 막아야 한다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.documents import ChunkLocation, DocumentFormat
from app.core.retrieval import ScoredChunk


class FinishReason(StrEnum):
    """스트림이 왜 끝났는가 — 정상 종료의 세 갈래.

    뒤의 둘을 가르는 이유는 사용자가 할 일이 다르기 때문이다(문서를 올린다 / 질문을 바꾼다)."""

    STOP = "stop"
    #: 근거가 0건이라 생성기를 부르지 않았다. 본문은 빈 문자열이다.
    NO_EVIDENCE = "no_evidence"
    #: 근거는 있었지만 생성기가 그것으로 답할 수 없다고 판정했다. 본문은 그 사유다.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class Citation:
    """답변이 실제로 인용한 근거 하나.

    `ScoredChunk` 와 합치지 않는 것은 마커가 이 답변 안에서만 유효한 라벨이기 때문이다."""

    marker: int
    document_id: str
    filename: str
    format: DocumentFormat
    revision: str
    chunk_index: int
    location: ChunkLocation
    score: float

    def __post_init__(self) -> None:
        # 0 이나 음수가 여기까지 왔다면 마커 검증이 범위 밖을 안 버렸다는 뜻이다.
        if self.marker < 1:
            raise ValueError("marker 는 1 이상이어야 한다")

    @classmethod
    def of(cls, marker: int, chunk: ScoredChunk) -> "Citation":
        """검증을 통과한 마커와 그 마커가 가리키는 검색 결과로 인용 하나를 만든다."""
        return cls(
            marker=marker,
            document_id=chunk.document_id,
            filename=chunk.filename,
            format=chunk.format,
            revision=chunk.revision,
            chunk_index=chunk.chunk_index,
            location=chunk.location,
            score=chunk.score,
        )


def build_citations(markers: Sequence[int], sources: Sequence[ScoredChunk]) -> tuple[Citation, ...]:
    """검증된 마커를 근거 목록에 대응시켜 인용 목록을 만든다.

    1-base ↔ 0-base 변환을 한 곳에 가둔다 — 흩어지면 인용이 옆 청크를 가리킨다."""
    return tuple(Citation.of(marker, sources[marker - 1]) for marker in markers)


@dataclass(frozen=True)
class Answer:
    """조립이 끝난 답변 한 건 — 본문, 종료 사유, 검증된 인용, 버려진 마커 수.

    `dropped_markers` 가 늘어나는 것이 프롬프트 열화의 가장 이른 신호라 숨기지 않는다."""

    text: str
    finish_reason: FinishReason
    citations: tuple[Citation, ...] = ()
    dropped_markers: int = 0

    def __post_init__(self) -> None:
        if self.dropped_markers < 0:
            raise ValueError("dropped_markers 는 0 이상이어야 한다")

        # 빈 본문에 이르는 경로가 둘이면 근거 없음(정상)과 빈 생성(장애)이 같은 화면이
        # 되어 장애가 조용해진다.
        if self.finish_reason is FinishReason.NO_EVIDENCE:
            if self.text:
                raise ValueError("근거가 없으면 답변 문자열을 만들지 않는다")
        elif not self.text:
            raise ValueError(f"{self.finish_reason} 로 끝나는 답변의 본문은 비어 있을 수 없다")

        # 인용을 남기면 거절문에 출처가 붙어 답변처럼 보인다. 판정이 마커를 이긴다.
        if self.finish_reason is not FinishReason.STOP and self.citations:
            raise ValueError(f"{self.finish_reason} 로 끝나는 답변에는 인용이 실리지 않는다")

        markers = [citation.marker for citation in self.citations]
        if len(markers) != len(set(markers)):
            raise ValueError("같은 근거가 두 번 인용될 수 없다")

    @classmethod
    def no_evidence(cls) -> "Answer":
        """근거가 0건일 때의 종료.

        문구를 만들지 않는다 — 답변 문자열을 만드는 곳은 생성기뿐이라는 규칙 때문이다."""
        return cls(text="", finish_reason=FinishReason.NO_EVIDENCE)
