"""밀집 retriever — 질의 임베딩 + 벡터 스토어 질의.

코사인 하한을 이 안에서 건다. 융합 뒤에 걸면 척도가 사라진 점수에 임계값을 걸게 된다
(`openspec/changes/add-rrf-algorithm-spec/design.md` 결정 3).
"""

from collections.abc import Sequence

from app.adapters.protocols import Embedder, VectorStore
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

    async def retrieve(
        self,
        query: str,
        *,
        depth: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[RetrievedChunk]:
        """대상 삼중항 안에서 코사인 하한을 넘긴 청크만 최대 `depth` 개."""
        # 대상이 없으면 임베딩 비용조차 만들지 않는다.
        if not versions:
            return []

        # 질의용 경로여야 한다. 문서용으로 계산해도 오류는 안 나지만 점수 분포가 이동해
        # 계측으로 정한 하한이 조용히 무효가 된다.
        embedding = await self._embedder.embed_query(query)

        found = await self._store.query(embedding, top_k=depth, versions=versions)
        return [chunk for chunk in found if chunk.native_score >= self._floor]
