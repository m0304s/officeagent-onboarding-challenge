"""리랭킹 재정렬 — 융합 결과에 크로스인코더 점수를 얹어 순서를 다시 세운다.

점수가 어디서 왔는지도 모델이 무엇인지도 알지 않는다. 정렬 규칙과 깊이 밖 후보의 처분은
`openspec/changes/add-cross-encoder/design.md` 결정 4 에 있다. 표준 라이브러리만 쓴다.
"""

import math
from collections.abc import Sequence
from dataclasses import replace

from app.core.retrieval import ScoredChunk


def targets(chunks: Sequence[ScoredChunk], *, depth: int) -> tuple[ScoredChunk, ...]:
    """리랭커에 넘길 후보 — 융합 상위 `depth` 개.

    절단 규칙이 한 곳뿐이어야 `reorder` 가 같은 경계를 본다."""
    return tuple(chunks[:depth])


def reorder(
    chunks: Sequence[ScoredChunk], scores: Sequence[float], *, depth: int
) -> list[ScoredChunk]:
    """`targets` 가 고른 후보를 점수 내림차순으로, 나머지는 융합 순서 그대로 뒤에.

    걸러내지 않는다 — 결과 집합은 입력과 같고 바뀌는 것은 순서뿐이다."""
    head = targets(chunks, depth=depth)
    _reject_unusable(head, scores)

    # 융합과 같은 전순서다. 한쪽만 안정적이면 "같은 질의는 같은 결과" 가 유지되지 않는다.
    ranked = sorted(
        zip(head, scores, strict=True),
        key=lambda pair: (-pair[1], pair[0].document_id, pair[0].chunk_index),
    )
    reranked = [replace(chunk, rerank_score=score) for chunk, score in ranked]
    # 깊이 밖은 버리지 않고 뒤에 붙인다 — 재검증이 앞쪽을 떨어뜨리면 이 자리가 K 를 채운다.
    return reranked + list(chunks[depth:])


def _reject_unusable(head: Sequence[ScoredChunk], scores: Sequence[float]) -> None:
    """길이가 어긋나거나 NaN 이 섞이면 여기서 끊는다.

    둘 다 조용한 오답이 되는 자리다 — 남의 점수가 실리거나 순서가 임의가 된다."""
    if len(scores) != len(head):
        raise ValueError(
            f"리랭킹 점수 개수가 후보 수와 다르다 — 후보 {len(head)}, 점수 {len(scores)}"
        )
    if any(math.isnan(score) for score in scores):
        raise ValueError("리랭킹 점수에 NaN 이 있다")
