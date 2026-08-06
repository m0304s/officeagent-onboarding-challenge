"""밀집 retriever — 질의 임베딩 + 벡터 스토어 질의.

코사인 하한을 이 안에서 판정한다 — 자르지는 않고 표시만 한다. 융합 뒤에 판정하면 척도가
사라진 점수에 임계값을 걸게 된다 (`openspec/changes/add-rrf-algorithm-spec/design.md` 결정 3).
"""

from collections.abc import Sequence
from dataclasses import replace

from app.adapters.protocols import Embedder, VectorStore
from app.core.cache import normalize_query
from app.core.documents import StoredIndexVersion
from app.core.retrieval import RetrievedChunk


class DenseRetriever:
    """질의를 벡터로 만들어 가까운 청크를 찾는다. `native_score` 는 코사인 유사도다."""

    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        *,
        min_score: float,
    ) -> None:
        self._embedder = embedder
        self._store = vector_store
        self._floor = min_score

    async def query_vector(self, query: str) -> list[float]:
        """질의 벡터 하나. 질의용 경로여야 한다 — 문서용은 점수 분포를 옮겨 하한을 무효로 만든다.

        입력이 정확 매치 캐시 키와 같은 정규화 결과인 것은, 갈리면 캐시 상태가 검색을 바꿔서다."""
        return await self._embedder.embed_query(normalize_query(query))

    async def retrieve(
        self,
        query: str,
        *,
        depth: int,
        versions: Sequence[StoredIndexVersion],
        embedding: Sequence[float] | None = None,
    ) -> list[RetrievedChunk]:
        """대상 삼중항 안에서 가까운 청크 최대 `depth` 개. 하한은 표시로만 남는다."""
        # 대상이 없으면 임베딩 비용조차 만들지 않는다.
        if not versions:
            return []

        if embedding is None:
            embedding = await self.query_vector(query)

        found = await self._store.query(embedding, top_k=depth, versions=versions)
        # 하한이 목록을 자르지 않는 근거는 `retrieval` 스펙의 채택 규칙에 있다.
        return [replace(chunk, gate_passed=chunk.native_score >= self._floor) for chunk in found]
