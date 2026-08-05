"""골든셋 로더와 검색 단계 채점.

재는 것은 근거 인용문이 상위 K 청크에 왔는가 하나다. `expected_spans` 대조는 생성된
답변이 있어야 성립해 LLM 없이는 잴 수 없고, 골든셋 README 가 두 채점을 섞지 말라고 한다.
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
    """근거가 있는 문항 하나 — 질문과 그 답이 실린 인용문."""

    id: str
    question: str
    probe: str
    quote: str
    page: int

    @property
    def stage(self) -> str:
        return self.probe.split(".", 1)[0]


def load_items() -> list[GoldenItem]:
    """근거를 가진 문항만. `answerable: false` 6건은 대조할 인용문이 없어 빠진다."""
    payload = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
    return [
        GoldenItem(
            id=item["id"],
            question=item["question"],
            probe=item["probe"],
            quote=item["evidence"]["quote"],
            page=item["evidence"]["page"],
        )
        for item in payload["items"]
        if item.get("evidence")
    ]


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
