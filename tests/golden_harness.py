"""골든셋 로더와 채점 앞 두 층 — 검색 게이트와 결정적 문자열 검사.

셋째 층(LLM-Judge)은 `tests/llm_judge.py` 에 있다. 게이트를 통과하지 못한 문항을 생성
채점에서 빼는 것이 골든셋 README 의 규칙이다 — 섞으면 두 지표가 함께 움직인다.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.retrieval import ScoredChunk

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"
GOLDEN_SET = GOLDEN_DIR / "insurance-golden-set.json"
SOURCE_PDF = Path(__file__).resolve().parents[1] / "sample-docs" / "insurance-summary.pdf"

_WHITESPACE = re.compile(r"\s+")


def squash(text: str) -> str:
    """공백을 전부 지운다. 이 PDF 는 추출에서 어절 공백이 소실돼 양쪽을 같은 모양으로 만든다."""
    return _WHITESPACE.sub("", text)


@dataclass(frozen=True)
class GoldenItem:
    """문항 하나 — 압박할 층(`probe`)과 층별 대조 재료."""

    id: str
    question: str
    probe: str
    answerable: bool
    reference_answer: str
    expected_spans: tuple[str, ...]
    must_not_contain: tuple[str, ...]
    #: 근거 인용문과 그 쪽. `answerable: false` 6건은 대조할 인용문이 없어 비어 있다.
    quote: str | None = None
    page: int | None = None

    @property
    def stage(self) -> str:
        return self.probe.split(".", 1)[0]


def load_items() -> list[GoldenItem]:
    """근거를 가진 문항만. `answerable: false` 6건은 대조할 인용문이 없어 빠진다."""
    return [item for item in _load_all() if item.quote]


def load_unanswerable() -> list[GoldenItem]:
    """근거가 없는 것이 정답인 문항. 검색 게이트를 거치지 않고 생성만 채점한다."""
    return [item for item in _load_all() if not item.answerable]


def _load_all() -> list[GoldenItem]:
    payload = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
    return [
        GoldenItem(
            id=item["id"],
            question=item["question"],
            probe=item["probe"],
            answerable=bool(item["answerable"]),
            reference_answer=item["reference_answer"],
            expected_spans=tuple(item["expected_spans"]),
            must_not_contain=tuple(item["must_not_contain"]),
            quote=(item["evidence"] or {}).get("quote"),
            page=(item["evidence"] or {}).get("page"),
        )
        for item in payload["items"]
    ]


def spans_hold(item: GoldenItem, answer: str) -> bool:
    """결정적 문자열 검사 — 기대 조각이 전부 있고 금지 조각이 하나도 없는가."""
    text = squash(answer)
    if any(squash(span) not in text for span in item.expected_spans):
        return False
    return all(squash(span) not in text for span in item.must_not_contain)


def rank_of(chunks: Sequence[ScoredChunk], quote: str) -> int | None:
    """인용문을 담은 첫 청크의 1-base 순위. 어디에도 없으면 `None`."""
    needle = squash(quote)
    for position, chunk in enumerate(chunks, start=1):
        if needle in squash(chunk.text):
            return position
    return None


@dataclass(frozen=True)
class Scores:
    """한 구성이 골든셋 전체에서 받은 점수."""

    ranks: dict[str, int | None]

    @property
    def measured(self) -> int:
        """근거를 찾은 문항 수. 못 찾은 문항은 순위가 없어 MRR 에 0으로 든다."""
        return sum(1 for rank in self.ranks.values() if rank is not None)

    def recall_at(self, k: int) -> int:
        return sum(1 for rank in self.ranks.values() if rank is not None and rank <= k)

    @property
    def mrr(self) -> float:
        """평균 역순위. 1위면 1.0, 못 찾으면 0 — 순위가 위로 올라오면 커진다."""
        if not self.ranks:
            return 0.0
        return sum(1 / rank for rank in self.ranks.values() if rank) / len(self.ranks)
