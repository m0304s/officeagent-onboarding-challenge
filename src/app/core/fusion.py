"""RRF 융합 — 이름표가 붙은 ranked list 여러 개를 하나의 순위로 합친다.

항목이 어디서 왔는지도 retriever 가 몇 개인지도 알지 않는다. 공식과 정규화의 근거는
`openspec/changes/add-rrf-algorithm-spec/design.md` 결정 4 에 있다. 표준 라이브러리만 쓴다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

# RRF 원 논문(Cormack et al., 2009)이 실험으로 고른 값이고 업계 관례다. 우리 코퍼스로
# 다시 재려면 정답 라벨이 필요한데 그것이 없다.
DEFAULT_RRF_K = 60


class FusableItem(Protocol):
    """융합이 항목에 요구하는 전부 — 전순서를 세울 정체성 값 둘과 원래 점수.

    `native_score` 가 가는 곳은 기여 내역뿐이다. 순위 계산에 넣으면 척도가 다른 값을 섞는다."""

    document_id: str
    chunk_index: int
    native_score: float


ItemT = TypeVar("ItemT", bound=FusableItem)


@dataclass(frozen=True)
class Contribution:
    """한 항목이 어느 목록에서 몇 위로 왔고, 그 목록이 실어 보낸 점수는 얼마였는가.

    융합 뒤의 점수만으로는 "왜 3위인가"에 답할 수 없어 남긴다."""

    retriever: str
    rank: int
    native_score: float


@dataclass(frozen=True)
class FusionInput(Generic[ItemT]):
    """융합에 전달되는 목록 하나 — 이름표·가중치·순위대로 실린 항목들.

    가중치가 양수라는 것은 융합이 전제하는 조건이고, 검사는 기동 시점의 설정 검증이 한다."""

    name: str
    weight: float
    items: tuple[ItemT, ...] = ()


@dataclass(frozen=True)
class FusedItem(Generic[ItemT]):
    """융합 결과 한 줄 — 항목·정규화된 융합 점수·기여 내역.

    `score` 는 관련성의 크기가 아니라 합의의 정도다. 여기에 관련성 하한을 걸면 뜻이 어긋난다."""

    item: ItemT
    score: float
    contributions: tuple[Contribution, ...]


def fuse(
    inputs: Sequence[FusionInput[ItemT]],
    *,
    top_k: int | None = None,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[FusedItem[ItemT]]:
    """이름표 붙은 목록들을 RRF 로 합쳐 점수 내림차순 목록 하나로 돌려준다.

    `top_k` 가 `None` 이면 자르지 않는다 — 캐시에 담기는 것이 절단 전 목록이라서다."""
    # 이름표 정렬 순서로 고정한다. 부동소수점 덧셈에 결합법칙이 없어 전달 순서대로 더하면
    # 같은 목록 집합이 순서에 따라 마지막 비트가 다른 점수를 낸다.
    ordered = sorted(inputs, key=lambda source: source.name)

    # 만장일치 1위의 원점수와 항 형태·누적 순서를 맞춘다. (Σw)/(RRF_K+1) 로 접으면
    # 두 값이 1 ulp 어긋나 그 항목의 점수가 1.0 에 못 미친다.
    denominator = 0.0
    for source in ordered:
        denominator += source.weight / (rrf_k + 1)
    if denominator <= 0:
        return []

    raw: dict[tuple[str, int], float] = {}
    payloads: dict[tuple[str, int], ItemT] = {}
    credits: dict[tuple[str, int], list[Contribution]] = {}

    for source in ordered:
        counted: set[tuple[str, int]] = set()
        for rank, item in enumerate(source.items, start=1):
            key = (item.document_id, item.chunk_index)
            if key in counted:
                continue
            counted.add(key)
            raw[key] = raw.get(key, 0.0) + source.weight / (rrf_k + rank)
            payloads.setdefault(key, item)
            credits.setdefault(key, []).append(
                Contribution(retriever=source.name, rank=rank, native_score=item.native_score)
            )

    fused = [
        FusedItem(item=payloads[key], score=raw[key] / denominator, contributions=tuple(credit))
        for key, credit in credits.items()
    ]
    # 정체성 값까지 비교해야 딕셔너리 삽입 순서가 동점의 순서에 닿지 않는다.
    fused.sort(key=lambda entry: (-entry.score, entry.item.document_id, entry.item.chunk_index))
    return fused if top_k is None else fused[:top_k]
