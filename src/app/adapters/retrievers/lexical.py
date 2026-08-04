"""어휘 retriever — 어휘 색인을 retriever 계약 뒤로 감싼다.

하한은 색인 안의 변별력 판정이라 여기에 임계값이 없다 (`ARCHITECTURE.md` 「어휘 색인」).
질의 임베딩을 부르지 않는 것이 계약이다 — 두 목록이 전처리를 공유하면 그 결함이 양쪽에
같은 방향으로 실려 융합이 아무것도 교정하지 못한다."""

from collections.abc import Sequence

from app.adapters.protocols import LexicalIndex
from app.core.documents import StoredIndexVersion
from app.core.retrieval import RetrievedChunk


class LexicalRetriever:
    """어휘 색인에 질의를 넘긴다. `native_score` 는 부호를 뒤집은 BM25 값이다."""

    def __init__(self, index: LexicalIndex) -> None:
        self._index = index

    async def retrieve(
        self,
        query: str,
        *,
        depth: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[RetrievedChunk]:
        """대상 삼중항 안에서 드문 토큰이 겹치는 청크만 최대 `depth` 개."""
        return await self._index.search(query, top_k=depth, versions=versions)
