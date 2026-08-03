"""프롬프트 조립과 모델 출력 파싱 — 순수 함수와 상태 기계 하나.

세 부품이 판정 줄 인식 규칙을 `_VERDICT_LINES` 하나로 공유한다 — 어긋나면 이어 붙인
조각과 `done.answer` 가 달라진다. 설계 의도는 `PROMPT_DESIGN.md` 에 있다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.retrieval import ScoredChunk

# 출력 문자열이 바뀌면 올린다 — 캐시 키에 들어갈 값이라, 없으면 프롬프트 개선이
# 낡은 캐시에 가려 관측되지 않는다.
PROMPT_VERSION = "qa-ko-1"


class Verdict(StrEnum):
    """모델이 첫 줄에 적는 판정 — 값이 곧 출력 문자열이다.

    `FinishReason` 과 합치면 프롬프트를 고칠 때 응답 스키마가 따라 바뀐다."""

    ANSWERABLE = "ANSWERABLE"
    INSUFFICIENT = "INSUFFICIENT"


_VERDICT_PREFIX = "VERDICT: "

# 정확히 일치해야 한다 — 관대하게 받으면 분리기가 버퍼 상한을 가질 수 없고, 형식을
# 어긴 회차에서만 스트리밍이 죽는다.
_VERDICT_LINES: dict[str, Verdict] = {
    f"{_VERDICT_PREFIX}{verdict.value}": verdict for verdict in Verdict
}

#: 판정 줄이 확정되기 전에 붙들 수 있는 최대 길이. 무한정 자라는 경로가 없다는 증거다.
MAX_VERDICT_LINE_CHARS = max(len(line) for line in _VERDICT_LINES)

# 답변 본문에서 `[12]` 형태만 마커로 본다.
_MARKER = re.compile(r"\[(\d+)\]")


# ── 프롬프트 조립 ────────────────────────────────────────────────────────

_INSTRUCTIONS = f"""\
당신은 사내 문서 질의응답 어시스턴트입니다. 아래 <근거>에 실린 내용만으로 <질문>에 답하십시오.

규칙
1. 근거에 없는 내용을 쓰지 마십시오. 이미 알고 있는 사실이라도 근거에 없으면 쓰지 않습니다.
2. **제공된 근거 밖을 조회하지 마십시오.** 파일을 열거나 명령을 실행하거나 웹을 검색하지 \
않습니다 — 답하는 데 필요한 것은 모두 아래에 있습니다.
3. 근거가 질문에 답하기에 충분하지 않으면 지어내지 말고, 무엇이 부족한지 밝히십시오.
4. 근거에서 가져온 문장은 **끝에 그 근거의 번호를 `[n]` 형태로** 답니다. 여러 근거를 함께 \
썼으면 `[1][2]` 처럼 이어 붙입니다. 근거 목록에 없는 번호는 쓰지 마십시오.

출력 형식
첫 줄에 판정을 아래 둘 중 하나로 **그대로** 적습니다. 앞뒤에 다른 말을 붙이지 않습니다.

{_VERDICT_PREFIX}{Verdict.ANSWERABLE.value}
{_VERDICT_PREFIX}{Verdict.INSUFFICIENT.value}

둘째 줄부터 본문을 한국어로 씁니다. {Verdict.ANSWERABLE.value} 이면 답변을, \
{Verdict.INSUFFICIENT.value} 이면 근거의 무엇이 부족한지를 씁니다. \
**어느 쪽이든 본문을 비워 두지 마십시오.**\
"""


def build_prompt(question: str, sources: Sequence[ScoredChunk]) -> str:
    """질문과 근거로 프롬프트 문자열 하나를 만든다.

    근거가 0건이면 만들지 않는다 — 빈 문맥에서 모델이 쓸 재료는 학습된 지식뿐이다."""
    if not sources:
        raise ValueError("근거 없이 프롬프트를 만들지 않는다")

    context = "\n\n".join(
        f"[{marker}] {chunk.filename} ({_describe(chunk)})\n{chunk.text}"
        for marker, chunk in enumerate(sources, start=1)
    )
    return f"{_INSTRUCTIONS}\n\n<근거>\n{context}\n</근거>\n\n<질문>\n{question}\n</질문>\n"


def _describe(chunk: ScoredChunk) -> str:
    """근거 하나의 원문 위치를 사람이 읽는 조각으로.

    PDF 는 쪽만 적는다 — 오프셋이 쪽 안의 값이라 적으면 문서 기준으로 오해한다."""
    if chunk.location.page is not None:
        return f"{chunk.location.page}쪽"
    return f"문자 {chunk.location.char_start}–{chunk.location.char_end}"


# ── 출력 파싱 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedAnswer:
    """모델 출력 하나를 형식 규칙대로 읽은 결과.

    정책을 담지 않아 같은 파서가 두 정책 아래에서 똑같이 동작한다."""

    verdict: Verdict
    body: str
    markers: tuple[int, ...] = ()
    dropped_markers: int = 0
    #: 판정 줄이 실제로 있었는가. 경고를 띄우는 것은 로깅 규약을 아는 서비스다.
    verdict_line_present: bool = True

    @property
    def has_body(self) -> bool:
        """본문이 실질적으로 있는가 — 공백뿐인 출력은 없는 것으로 본다."""
        return bool(self.body.strip())


def parse_answer(raw: str, source_count: int) -> ParsedAnswer:
    """완성된 모델 출력에서 판정·본문·검증된 마커를 뽑는다.

    판정 줄이 없어도 본문을 버리지 않고, 범위 밖 마커는 버리고 개수를 센다."""
    verdict, body = _split_verdict(raw)
    markers, dropped = _validate_markers(body, source_count)
    return ParsedAnswer(
        verdict=Verdict.ANSWERABLE if verdict is None else verdict,
        body=body,
        markers=markers,
        dropped_markers=dropped,
        verdict_line_present=verdict is not None,
    )


def _split_verdict(raw: str) -> tuple[Verdict | None, str]:
    """첫 줄을 판정으로 읽고 나머지를 본문으로. 없으면 전체가 본문이다.

    판정 줄 뒤의 공백만 걷어낸다 — 없을 때 건드리면 출력 일부가 버려진다."""
    head, _, rest = raw.partition("\n")
    verdict = _VERDICT_LINES.get(head)
    if verdict is None:
        return None, raw
    return verdict, rest.lstrip()


def _validate_markers(body: str, source_count: int) -> tuple[tuple[int, ...], int]:
    """마커를 등장 순서로 모으고, 중복을 지우고, 범위 밖을 버린다.

    중복 제거가 먼저다 — 세는 대상이 "잘못 가리킨 근거"이지 "잘못 적은 글자"가 아니다."""
    seen = list(dict.fromkeys(int(marker) for marker in _MARKER.findall(body)))
    kept = tuple(marker for marker in seen if 1 <= marker <= source_count)
    return kept, len(seen) - len(kept)


# ── 스트리밍용 판정 줄 분리 ──────────────────────────────────────────────


class VerdictSplitter:
    """도착하는 조각에서 판정 줄만 걷어내고 본문은 그대로 흘려보내는 상태 기계.

    접두사에서 이탈하는 즉시 확정해 전부 내보낸다 — 없으면 형식을 어긴 회차가 멎는다."""

    def __init__(self) -> None:
        self._buffer = ""
        self._verdict: Verdict | None = None
        self._settled = False
        self._had_verdict_line = False
        # `parse_answer` 의 `rest.lstrip()` 과 같은 규칙이라 두 경로의 본문이 일치한다.
        self._trimming = False

    @property
    def verdict(self) -> Verdict | None:
        """읽어 낸 판정. 판정 줄이 없었거나 아직 확정 전이면 `None`."""
        return self._verdict

    @property
    def settled(self) -> bool:
        """판정 줄 처리가 끝났는가. 이 뒤로는 모든 조각이 그대로 나간다."""
        return self._settled

    @property
    def had_verdict_line(self) -> bool:
        """확정된 판정이 실제 판정 줄에서 온 것인가 (`settled` 일 때만 의미가 있다)."""
        return self._had_verdict_line

    def feed(self, chunk: str) -> list[str]:
        """조각 하나를 넣고, 지금 내보낼 본문 조각들을 받는다.

        빈 문자열은 담기지 않는다 — 담으면 내용 없는 `answer` 이벤트가 나간다."""
        if self._settled:
            return self._emit(chunk)
        self._buffer += chunk
        return self._settle()

    def finish(self) -> list[str]:
        """스트림이 끝났음을 알리고 남은 버퍼를 정리한다.

        개행 없이 끝나는 출력이 있어 `feed` 만으로는 마지막 조각이 버퍼에 갇힌다."""
        if self._settled:
            return []
        return self._settle(final=True)

    # ── 내부 ────────────────────────────────────────────────────────────

    def _settle(self, *, final: bool = False) -> list[str]:
        pending = self._buffer
        head, separator, rest = pending.partition("\n")

        if separator:
            verdict = _VERDICT_LINES.get(head)
            if verdict is not None:
                self._accept(verdict)
                return self._emit(rest)
            return self._reject(pending)

        # 개행이 아직 없다 — 판정 줄이 진행 중일 수도, 애초에 없을 수도 있다.
        if final:
            verdict = _VERDICT_LINES.get(pending)
            if verdict is not None:
                self._accept(verdict)
                return []
            return self._reject(pending)

        if any(line.startswith(pending) for line in _VERDICT_LINES):
            return []
        return self._reject(pending)

    def _accept(self, verdict: Verdict) -> None:
        self._buffer = ""
        self._settled = True
        self._verdict = verdict
        self._had_verdict_line = True
        self._trimming = True

    def _reject(self, pending: str) -> list[str]:
        """판정 줄이 없다고 확정하고 붙들고 있던 것을 통째로 내보낸다."""
        self._buffer = ""
        self._settled = True
        self._trimming = False
        return self._emit(pending)

    def _emit(self, chunk: str) -> list[str]:
        if self._trimming:
            chunk = chunk.lstrip()
            if not chunk:
                return []
            self._trimming = False
        return [chunk] if chunk else []
