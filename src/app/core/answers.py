"""답변 도메인 — 종료 사유, 인용, 조립된 답변.

검색이 근거 후보까지 돌려주고(`core/retrieval.py`), 여기서 정하는 것은 **그 근거로 무엇을
만들었는가**다. 답변 한 건은 세 가지를 스스로 말한다 — 왜 끝났는가(`FinishReason`),
무엇을 인용했는가(`Citation`), 그리고 그 둘이 서로 모순되지 않는가.

**모순 없음을 값 객체가 직접 지킨다.** `Answer.__post_init__` 의 불변식은 스펙의 "빈
답변으로 끝나는 종료는 근거 없음뿐이다"와 "답할 수 없다는 판정에 인용이 실리지 않는다"를
그대로 옮긴 것이다. 서비스가 조립을 잘못하면 응답이 나가기 전에 여기서 걸린다 — 두 규칙이
깨진 응답은 소비자 화면에서 정상과 장애가 같아 보이게 만들기 때문에, 테스트가 아니라
타입이 막아야 하는 종류다.

**`elapsed_ms` 는 여기 없다.** 소요 시간은 답변의 성질이 아니라 그 답변을 만든 요청의
측정값이다. `done` 이벤트가 두 값을 합쳐 싣고, 그 조립은 서비스가 한다.

`core/` 는 표준 라이브러리만 쓴다 — pydantic 도 쓰지 않는다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.documents import ChunkLocation, DocumentFormat
from app.core.retrieval import ScoredChunk


class FinishReason(StrEnum):
    """스트림이 왜 끝났는가 — 정상 종료의 세 갈래.

    실패로 끝난 스트림은 여기 없다. 그쪽은 `done` 이 아니라 `error` 이벤트로 끝나고
    오류 코드가 사유를 말한다. 이 열거형이 가르는 것은 **답변 자리에 무엇이 있는가**다.

    `NO_EVIDENCE` 와 `INSUFFICIENT_EVIDENCE` 를 뭉개지 않는 이유는 사용자가 할 일이
    다르기 때문이다 — 앞은 문서를 올려야 하고 뒤는 질문을 바꿔야 한다. 판정 주체도
    다르다: 앞은 서비스(근거가 0건이라는 사실만으로 판정된다), 뒤는 생성기(근거 본문을
    읽어야 판정된다).
    """

    STOP = "stop"
    #: 근거가 0건이라 생성기를 **부르지 않았다**. 본문은 빈 문자열이다.
    NO_EVIDENCE = "no_evidence"
    #: 근거는 있었지만 생성기가 그것으로 답할 수 없다고 판정했다. 본문은 그 사유다.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class Citation:
    """답변이 실제로 인용한 근거 하나.

    `ScoredChunk` 에 **마커 번호**가 붙은 모양이다. 두 타입을 합치지 않는 이유는 마커가
    검색의 사실이 아니라 이 답변 안에서만 유효한 라벨이기 때문이다 — 같은 청크가 다음
    질문에서는 다른 번호를 받는다.

    나머지 값은 `ScoredChunk` 에서 **그대로 복사한다**(`of`). 인용은 같은 스트림이 이미
    `sources` 로 보여 준 근거를 가리킬 뿐 새로운 사실을 만들지 않으므로, 여기서 값을
    다시 조회하거나 가공하면 두 이벤트가 어긋날 수 있는 자리가 생긴다.
    """

    marker: int
    document_id: str
    filename: str
    format: DocumentFormat
    revision: str
    chunk_index: int
    location: ChunkLocation
    score: float

    def __post_init__(self) -> None:
        # 마커는 문맥 블록의 라벨이고 그 라벨은 1부터 매긴다. 0 이나 음수가 여기까지
        # 왔다면 마커 검증이 범위 밖을 버리지 않았다는 뜻이다.
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

    **마커는 1-base 라벨이고 목록은 0-base 다.** 이 변환이 호출부마다 흩어지면 한 곳만
    틀려도 인용이 옆 청크를 가리키는데, 그 오류는 형식이 멀쩡해서 눈에 띄지 않는다 —
    출처 표기가 오히려 신뢰를 만들어 내는 장치가 되는 자리라 변환을 한 곳에 가둔다.

    범위 검증은 **이미 끝나 있어야 한다**(`core.prompting.parse_answer`). 여기서 다시
    거르지 않고 `IndexError` 로 터지게 두는 이유는, 검증되지 않은 마커가 여기까지 오는
    것이 조용히 넘어갈 사건이 아니라 파이프라인 배선이 잘못됐다는 뜻이기 때문이다.
    """
    return tuple(Citation.of(marker, sources[marker - 1]) for marker in markers)


@dataclass(frozen=True)
class Answer:
    """조립이 끝난 답변 한 건 — 본문, 종료 사유, 검증된 인용, 버려진 마커 수.

    `dropped_markers` 를 숨기지 않는 이유는 그 수가 늘어나는 것이 프롬프트가 나빠졌다는
    가장 이른 신호이기 때문이다. 응답에 실려 있지 않으면 "인용이 왜 하나뿐인가"를 로그
    없이는 설명할 수 없다.
    """

    text: str
    finish_reason: FinishReason
    citations: tuple[Citation, ...] = ()
    dropped_markers: int = 0

    def __post_init__(self) -> None:
        if self.dropped_markers < 0:
            raise ValueError("dropped_markers 는 0 이상이어야 한다")

        # ── 빈 본문의 의미를 하나로 고정한다 ────────────────────────────
        #
        # 소비자는 본문이 비었을 때 자기 문구를 띄우게 된다. 그 자리에 도달하는 경로가
        # 둘이면 "근거가 없습니다"(정상)와 "모델이 아무것도 쓰지 않았습니다"(장애)가 같은
        # 화면이 되어 장애가 조용해진다.
        if self.finish_reason is FinishReason.NO_EVIDENCE:
            if self.text:
                raise ValueError("근거가 없으면 답변 문자열을 만들지 않는다")
        elif not self.text:
            raise ValueError(f"{self.finish_reason} 로 끝나는 답변의 본문은 비어 있을 수 없다")

        # ── 인용은 답한 경우에만 붙는다 ─────────────────────────────────
        #
        # "답할 수 없다"면서 근거를 인용하는 출력은 모순이고, 둘 중 판정을 이기게 하는
        # 편이 안전하다 — 인용을 남기면 거절문에 출처가 붙어 답변처럼 보인다.
        if self.finish_reason is not FinishReason.STOP and self.citations:
            raise ValueError(f"{self.finish_reason} 로 끝나는 답변에는 인용이 실리지 않는다")

        markers = [citation.marker for citation in self.citations]
        if len(markers) != len(set(markers)):
            raise ValueError("같은 근거가 두 번 인용될 수 없다")

    @classmethod
    def no_evidence(cls) -> "Answer":
        """근거가 0건일 때의 종료.

        **문자열을 만들지 않는다는 사실 자체가 이 생성자의 내용이다.** 근거가 없다는
        사실은 `finish_reason` 이 나르고, 사용자에게 보일 표현은 소비자가 정한다.
        답변 문자열을 만드는 곳이 생성기뿐이라는 규칙에 예외를 두지 않으려면 이 경로가
        비어 있어야 한다.
        """
        return cls(text="", finish_reason=FinishReason.NO_EVIDENCE)
