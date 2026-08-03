"""프롬프트 조립과 모델 출력 파싱 — 순수 함수와 상태 기계 하나.

**여기 있는 것이 "무엇을 지시했는가"의 진실 원천이다.** 프롬프트 문자열이 서비스나
어댑터 안에 흩어지면 생성기 구현마다 프롬프트가 하나씩 생기고, 프롬프트 회귀 테스트가
어느 어댑터의 것인지 정해야 한다. `core/` 에 두면 LLM·네트워크·구독 없이 문자열 단언으로
회귀를 고정할 수 있다 — 설계 의도 전체는 `PROMPT_DESIGN.md` 에 있다.

**모델에게 요구하는 형식은 판정 줄 + 마커 산문**이다. CLI 가 지원하는 JSON Schema 강제
출력을 쓰지 않는 이유는 부분 JSON 을 파싱할 수 없어 스트리밍과 원리적으로 충돌하기
때문이다. 판정을 첫 줄에 두면 그 한 줄만 버퍼링하고 나머지는 그대로 흘려보낼 수 있다.

```
VERDICT: ANSWERABLE
교육비는 연 200만원까지 지원됩니다 [1]. 신청은 부서장 승인 후 가능합니다 [1][2].
```

세 부품이 **같은 형식 규칙을 공유한다**:

- `build_prompt`  — 그 형식을 지시한다
- `parse_answer`  — 완성된 출력에서 그 형식을 읽는다
- `VerdictSplitter` — 도착하는 조각에서 판정 줄만 걷어낸다 (스트리밍용)

뒤의 둘이 어긋나면 `answer` 이벤트를 이어 붙인 것과 `done.answer` 가 달라진다 — 스펙이
같기를 요구하는 두 값이다. 그래서 둘은 판정 줄 인식 규칙을 상수 하나(`_VERDICT_LINES`)로
공유하고, 테스트가 임의의 조각 분할에 대해 두 결과가 같음을 직접 단언한다.

`core/` 는 표준 라이브러리만 쓴다 — 정규식과 문자열 연산뿐이다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.retrieval import ScoredChunk

# 프롬프트 판본. **출력 문자열이 바뀌면 반드시 올린다.**
#
# 다음 change 의 캐시 키에 들어갈 값이다 — 프롬프트가 바뀌면 같은 질문의 답이 달라지므로,
# 버전이 키에 없으면 프롬프트 개선이 낡은 캐시에 가려 관측되지 않는다. 여기서 값만 만들어
# 두고 키 설계는 캐싱 change 에 넘긴다.
PROMPT_VERSION = "qa-ko-1"


class Verdict(StrEnum):
    """모델이 첫 줄에 적는 판정.

    값이 대문자인 것은 이것이 내부 식별자가 아니라 **출력 문자열 그 자체**이기 때문이다.
    `FinishReason`(소비자가 보는 종료 사유)과 별도로 두는 이유도 같다 — 이쪽은 모델과의
    형식 약속이고 저쪽은 API 계약이다. 하나로 합치면 프롬프트를 고칠 때 응답 스키마가
    따라 바뀐다.
    """

    ANSWERABLE = "ANSWERABLE"
    INSUFFICIENT = "INSUFFICIENT"


_VERDICT_PREFIX = "VERDICT: "

# 판정 줄로 인정되는 문자열의 전부. 파서와 스트리밍 분리기가 이 하나를 공유한다.
#
# **정확히 일치해야 한다** — 앞뒤 공백도 허용하지 않는다. 관대하게 받으면 분리기가
# "판정 줄이 아직 끝나지 않았을 가능성"을 판정할 수 없어(뒤에 공백이 몇 개 더 올지 모른다)
# 버퍼 상한이 사라지고, 그 순간 형식을 어긴 회차에서만 스트리밍이 죽는다. 형식을 어긴
# 출력에는 이미 정의된 동작이 있다 — 전체를 본문으로 본다.
_VERDICT_LINES: dict[str, Verdict] = {
    f"{_VERDICT_PREFIX}{verdict.value}": verdict for verdict in Verdict
}

#: 판정 줄이 확정되기 전에 붙들 수 있는 최대 길이. 무한정 자라는 경로가 없다는 증거다.
MAX_VERDICT_LINE_CHARS = max(len(line) for line in _VERDICT_LINES)

# 인용 마커. 답변 본문에서 `[12]` 같은 형태만 마커로 본다.
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

    문맥 블록은 검색 결과가 **이미 들고 있는 값으로만** 만든다 — 번호, 파일명, 원문 위치,
    본문. 파일명과 위치를 넣는 이유는 둘이다: 모델이 "어느 문서에서 왔는가"를 답변 문장에
    녹일 수 있고, 마커가 가리키는 대상이 사람이 읽어도 확인 가능해진다.

    **근거가 0건이면 만들지 않는다.** 문맥이 빈 프롬프트에서 모델이 쓸 수 있는 재료는
    학습된 지식뿐이라, 그 경로는 애초에 생성기를 부르지 않는 것으로 닫혀 있다(서비스가
    `no_evidence` 로 끝낸다). 여기서 예외를 던지는 것은 그 결정을 **구조로** 만드는
    일이다 — 배선이 잘못돼 빈 문맥이 여기까지 오면 조용히 환각 위험을 여는 대신 터진다.
    """
    if not sources:
        raise ValueError("근거 없이 프롬프트를 만들지 않는다")

    context = "\n\n".join(
        f"[{marker}] {chunk.filename} ({_describe(chunk)})\n{chunk.text}"
        for marker, chunk in enumerate(sources, start=1)
    )
    return f"{_INSTRUCTIONS}\n\n<근거>\n{context}\n</근거>\n\n<질문>\n{question}\n</질문>\n"


def _describe(chunk: ScoredChunk) -> str:
    """근거 하나의 원문 위치를 사람이 읽는 한 조각으로.

    PDF 는 쪽만 적는다. 그때의 문자 오프셋은 **그 쪽 안의** 오프셋이라(수집이 문서 전역
    오프셋을 만들지 않는다) 문서에서 그 자리를 찾는 데 쓸 수 없고, 적어 두면 읽는 사람이
    문서 전체 기준이라고 오해한다.
    """
    if chunk.location.page is not None:
        return f"{chunk.location.page}쪽"
    return f"문자 {chunk.location.char_start}–{chunk.location.char_end}"


# ── 출력 파싱 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedAnswer:
    """모델 출력 하나를 형식 규칙대로 읽은 결과.

    **정책을 담지 않는다.** 판정이 `INSUFFICIENT` 일 때 마커를 무시할지, 본문이 비었을 때
    재시도할지는 서비스의 몫이다. 여기 있는 것은 "출력에 무엇이 적혀 있었는가"뿐이라
    같은 파서가 두 정책 아래에서 똑같이 동작한다.
    """

    verdict: Verdict
    body: str
    markers: tuple[int, ...] = ()
    dropped_markers: int = 0
    #: 판정 줄이 실제로 있었는가. 없으면 `verdict` 는 `ANSWERABLE` 로 **본 것**이다.
    #: 형식 위반은 응답이 아니라 로그와 프롬프트 회귀 테스트로 잡는다 — 그 경고를
    #: 띄우는 것은 로깅 규약을 아는 서비스이고, `core/` 는 사실만 넘긴다.
    verdict_line_present: bool = True

    @property
    def has_body(self) -> bool:
        """본문이 실질적으로 있는가 — 공백뿐인 출력은 없는 것으로 본다."""
        return bool(self.body.strip())


def parse_answer(raw: str, source_count: int) -> ParsedAnswer:
    """완성된 모델 출력에서 판정·본문·검증된 마커를 뽑는다.

    **판정 줄이 없어도 본문을 버리지 않는다.** 그 본문은 이미 근거만 주어진 프롬프트에서
    나온 것이라 형식 위반이 곧 환각은 아니고, 마커 검증은 그대로 돌아 출처 보증도 그대로다.
    그때는 `ANSWERABLE` 로 보고 `verdict_line_present` 로 그 사실을 알린다.

    `source_count` 는 근거 개수다. 이 범위(`1..source_count`) 밖을 가리키는 마커는 **없는
    근거를 가리키는 인용**, 즉 가장 그럴듯한 형태의 환각이라 버리고 개수를 센다.
    """
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
    """첫 줄을 판정으로 읽고 나머지를 본문으로 돌려준다. 판정 줄이 없으면 전체가 본문.

    판정 줄 뒤의 앞쪽 공백은 걷어낸다 — 모델이 판정과 본문 사이에 빈 줄을 넣는 것이
    흔한데, 그 개행이 답변 첫 글자 앞에 남으면 소비자 화면 맨 위에 빈 줄이 생긴다.
    **반대로 판정 줄이 없을 때는 한 글자도 건드리지 않는다** — 그때는 출력 전체가 본문이고,
    스펙이 "어느 부분도 버려져서는 안 된다"고 못 박은 자리다.
    """
    head, _, rest = raw.partition("\n")
    verdict = _VERDICT_LINES.get(head)
    if verdict is None:
        return None, raw
    return verdict, rest.lstrip()


def _validate_markers(body: str, source_count: int) -> tuple[tuple[int, ...], int]:
    """본문의 마커를 등장 순서로 모으고, 중복을 지우고, 범위 밖을 버린다.

    **중복 제거가 범위 검증보다 먼저다.** 같은 존재하지 않는 번호를 세 번 쓴 답변의
    버려진 마커 수는 `3` 이 아니라 `1` 이어야 한다 — 세는 대상이 "잘못 가리킨 근거"이지
    "잘못 적은 글자"가 아니기 때문이다. 뒤집으면 그 수가 답변 길이에 비례해 흔들려
    프롬프트 열화를 재는 신호로 쓸 수 없다.
    """
    seen = list(dict.fromkeys(int(marker) for marker in _MARKER.findall(body)))
    kept = tuple(marker for marker in seen if 1 <= marker <= source_count)
    return kept, len(seen) - len(kept)


# ── 스트리밍용 판정 줄 분리 ──────────────────────────────────────────────


class VerdictSplitter:
    """도착하는 조각에서 판정 줄만 걷어내고 본문은 그대로 흘려보내는 상태 기계.

    **`services/` 가 아니라 `core/` 에 있는 이유**는 위 두 함수와 같다 — 출력 형식이
    무엇을 뜻하는지는 도메인 지식이고, 순수하게 두어야 비동기·페이크 없이 문자열 단언으로
    회귀를 고정할 수 있다. 이 change 에서 가장 틀리기 쉬운 로직인데 서비스에 두면 그
    테스트가 페이크 생성기와 이벤트 시퀀스 위에 서게 된다.

    실측이 이 부품을 필수로 만들었다. 판정 줄 하나가 델타 **일곱 조각**에 걸쳐 온다 —
    `VER`·`DICT`·`:`·`' ANSW'`·`ER`·`ABLE`·`\\n`.

    **규칙은 둘이다.**

    1. 개행이 오면 첫 줄을 판정으로 읽는다. 형식이 맞으면 그 뒤가 본문, 안 맞으면 첫
       줄까지 통째로 본문이다.
    2. 누적 문자열이 판정 줄 어느 쪽의 접두사도 아니게 되는 순간 판정 없음으로 확정하고
       **즉시 전부 내보낸다.**

    두 번째 규칙이 없으면 판정 줄을 쓰지 않은 출력에서 첫 줄 전체(짧은 답이면 답변 전부)를
    붙들게 된다 — 하필 형식을 어긴 회차에서만 스트리밍이 죽는다. 이 규칙 덕에 붙들리는
    양이 `MAX_VERDICT_LINE_CHARS` 로 고정된다.

    확정된 뒤의 조각은 **글자 그대로** 나간다. 다시 모으지도, 합치지도, 쪼개지도 않는다.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._verdict: Verdict | None = None
        self._settled = False
        self._had_verdict_line = False
        # 판정 줄 **뒤**의 첫 글자를 아직 못 봤는가. 이 동안만 앞쪽 공백을 걷어낸다 —
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

        빈 목록은 "아직 판정을 기다리는 중"이거나 "이 조각은 전부 판정 줄이었다"는 뜻이다.
        빈 문자열은 절대 담기지 않는다 — 담으면 내용 없는 `answer` 이벤트가 나간다.
        """
        if self._settled:
            return self._emit(chunk)
        self._buffer += chunk
        return self._settle()

    def finish(self) -> list[str]:
        """스트림이 끝났음을 알리고 남은 버퍼를 정리한다.

        개행 없이 끝나는 출력이 있다 — 판정 줄만 내고 끝난 회차(본문 없음, 생성 실패로
        분류된다)와 짧은 한 줄 답변이 그렇다. `feed` 만으로는 그 둘을 가를 수 없어서
        종료를 알리는 호출이 따로 필요하다. 이것이 없으면 마지막 조각이 버퍼에 갇힌다.
        """
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
