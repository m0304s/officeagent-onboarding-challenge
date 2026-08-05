"""골든셋 생성 채점의 셋째 층 — 답변이 기준 답변과 같은 말을 하는지 판정자에게 묻는다.

판정자에게 근거 청크를 넘기지 않는다. 근거가 왔는지는 검색 게이트가 이미 판정했고,
여기서 묻는 것은 기준 답변과의 일치 하나다. 세 층의 채점 순서는 `tests/README.md` 에 있다.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.adapters.protocols import AnswerGenerator
from tests.golden_harness import GoldenItem

#: 판정 프롬프트가 바뀌면 올린다. 회차 사이의 통과 수는 같은 판정 규칙 아래에서만 비교된다.
JUDGE_PROMPT_VERSION = "judge-ko-1"


class JudgeVerdict(StrEnum):
    """판정 셋. `UNJUDGED` 는 모델이 내는 값이 아니라 파서가 형식 위반에 붙이는 값이다."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNJUDGED = "UNJUDGED"


_JUDGE_PREFIX = "JUDGE: "

_JUDGE_LINES: dict[str, JudgeVerdict] = {
    f"{_JUDGE_PREFIX}{verdict.value}": verdict
    for verdict in (JudgeVerdict.MATCH, JudgeVerdict.MISMATCH)
}

#: 형식을 어긴 출력에서 사유 자리에 남길 길이. 통째로 실으면 비교표가 읽히지 않는다.
_RAW_REASON_CHARS = 200

#: 근거 0건 종료를 판정자가 읽을 문장으로 옮긴 것. 서비스는 이때 문구를 만들지 않아
#: (`Answer.no_evidence`) 채점하는 쪽이 한 번만 옮긴다.
NO_EVIDENCE_TEXT = "제공된 문서에서 근거를 찾지 못해 답변하지 않았습니다."

_INSTRUCTIONS = f"""\
당신은 문서 질의응답 시스템의 채점자입니다. <기준 답변>과 <답변>이 같은 내용을 말하는지만 \
판정하십시오.

규칙
1. 표현·어순·문장 수의 차이는 무시합니다. 답변에 붙은 인용 번호(`[1]`)도 무시합니다. \
값과 사실이 같으면 일치입니다.
2. 기준 답변에 있는 수치·기간·조건이 빠졌거나 다르면 불일치입니다.
3. 기준 답변이 문서에 내용이 없다고 말하면, 답변도 근거가 없다고 밝혀야 일치입니다. \
답을 지어냈으면 불일치입니다.
4. 답변이 더 자세한 것만으로는 불일치가 아닙니다. 기준 답변과 어긋나는 사실을 덧붙였으면 \
불일치입니다.
5. 아래 두 문장 밖을 조회하지 마십시오 — 파일을 열거나 명령을 실행하거나 웹을 검색하지 \
않습니다. 판정에 필요한 것은 모두 아래에 있습니다.

출력 형식
첫 줄에 판정을 아래 둘 중 하나로 그대로 적습니다. 앞뒤에 다른 말을 붙이지 않습니다.

{_JUDGE_PREFIX}{JudgeVerdict.MATCH.value}
{_JUDGE_PREFIX}{JudgeVerdict.MISMATCH.value}

둘째 줄에 그렇게 판정한 이유를 한 문장으로 적습니다.\
"""


def build_judge_prompt(question: str, reference: str, answer: str) -> str:
    """질문과 두 답변으로 판정 프롬프트 하나를 만든다."""
    return (
        f"{_INSTRUCTIONS}\n\n"
        f"<질문>\n{question}\n</질문>\n\n"
        f"<기준 답변>\n{reference}\n</기준 답변>\n\n"
        f"<답변>\n{answer}\n</답변>\n"
    )


@dataclass(frozen=True)
class Judgement:
    """판정 하나와 그 사유."""

    verdict: JudgeVerdict
    reason: str


def parse_judgement(raw: str) -> Judgement:
    """첫 줄이 판정 줄이 아니면 `UNJUDGED` 다 — 형식 위반을 오답으로 세지 않기 위해서."""
    head, _, rest = raw.strip().partition("\n")
    verdict = _JUDGE_LINES.get(head.strip())
    if verdict is None:
        return Judgement(JudgeVerdict.UNJUDGED, raw.strip()[:_RAW_REASON_CHARS])
    return Judgement(verdict, rest.strip().partition("\n")[0])


async def judge(
    generator: AnswerGenerator,
    item: GoldenItem,
    answer: str,
    *,
    timeout_seconds: float,
) -> Judgement:
    """판정자를 한 턴 돌려 답변 하나를 채점한다."""
    prompt = build_judge_prompt(item.question, item.reference_answer, answer)
    pieces = [piece async for piece in generator.generate(prompt, timeout_seconds=timeout_seconds)]
    return parse_judgement("".join(pieces))


@dataclass(frozen=True)
class Graded:
    """문항 하나의 채점 결과. 층을 합산하지 않고 나란히 든다 (design 결정 13)."""

    id: str
    probe: str
    #: 검색 게이트를 통과했는가. `answerable: false` 문항은 게이트가 없어 항상 참이다.
    retrieved: bool
    #: 스트림이 무엇으로 닫혔는가. 게이트에 걸린 문항도 이 값은 갖는다 — 근거가 오지
    #: 않았을 때 시스템이 답을 지어냈는지 거절했는지가 여기서만 보인다.
    finish_reason: str | None = None
    spans_ok: bool | None = None
    judgement: Judgement | None = None

    @property
    def verdict(self) -> JudgeVerdict | None:
        return None if self.judgement is None else self.judgement.verdict

    @property
    def disagrees(self) -> bool:
        """결정적 문자열 검사와 판정이 엇갈렸는가 — 판정자를 의심할 자리다."""
        if self.spans_ok is None or self.verdict in (None, JudgeVerdict.UNJUDGED):
            return False
        return self.spans_ok is not (self.verdict is JudgeVerdict.MATCH)


@dataclass(frozen=True)
class GenerationScores:
    """한 구성이 골든셋 생성 채점에서 받은 결과."""

    graded: tuple[Graded, ...]

    @property
    def gated_out(self) -> int:
        """근거가 오지 않아 생성 채점에서 빠진 문항 수."""
        return sum(1 for row in self.graded if not row.retrieved)

    @property
    def judged(self) -> int:
        return sum(1 for row in self.graded if row.verdict is not None)

    @property
    def spans_passed(self) -> int:
        return sum(1 for row in self.graded if row.spans_ok)

    @property
    def disagreements(self) -> tuple[Graded, ...]:
        return tuple(row for row in self.graded if row.disagrees)

    def counting(self, verdict: JudgeVerdict) -> int:
        return sum(1 for row in self.graded if row.verdict is verdict)

    def finishing(self, reason: str) -> int:
        """스트림이 그 사유로 닫힌 문항 수. 게이트에 걸린 문항도 함께 센다."""
        return sum(1 for row in self.graded if row.finish_reason == reason)
