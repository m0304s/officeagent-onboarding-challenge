"""검색 오케스트레이션 — 임베딩 → 대상 집합 → 질의 → 재검증 → 하한 → 조립.

대상 집합이 저장소 질의보다 앞인 것이 계약이다. 답변은 만들지 않는다
(`ARCHITECTURE.md` 검색 파이프라인).
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.adapters.protocols import DocumentRegistry, Embedder, VectorStore
from app.core.documents import Document, StoredIndexVersion
from app.core.retrieval import ScoredChunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievalResult:
    """검색 한 건이 무엇을 돌려주는가.

    `target_documents` 를 함께 싣는 이유는 빈 결과의 원인이 그 값으로 갈리기 때문이다."""

    query: str
    top_k: int
    chunks: tuple[ScoredChunk, ...]
    target_documents: int

    @property
    def count(self) -> int:
        return len(self.chunks)

    @property
    def top_score(self) -> float | None:
        """1위 점수. `None` 인 이유는 `0.0` 이 무관해서 0점인 경우와 겹치기 때문이다."""
        return self.chunks[0].score if self.chunks else None


class RetrievalService:
    """질의 하나를 받아 지금 유효한 청크 중 가까운 것부터 최대 K개를 돌려준다."""

    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        registry: DocumentRegistry,
        *,
        index_signature: str,
        top_k: int,
        min_score: float,
    ) -> None:
        self._embedder = embedder
        self._store = vector_store
        self._registry = registry
        # 수집이 쓴 것과 같은 값이어야 해 배선이 한 번 유도해 넣는다 — 각자 유도하면
        # 한쪽만 고쳐진 순간 방금 올린 문서가 오류 없이 검색되지 않는다.
        self._index_signature = index_signature
        self._top_k = top_k
        self._min_score = min_score

    async def search(self, query: str, *, top_k: int | None = None) -> RetrievalResult:
        """질의 한 건. `top_k` 를 주면 설정 기본값을 대신한다.

        유효성은 API 계층이 이미 봤다 — 거부된 요청이 임베딩을 유발하지 않게 한다."""
        effective_k = self._top_k if top_k is None else top_k

        # 질의용 경로여야 한다. 문서용으로 계산해도 오류는 안 나지만 점수 분포가 이동해
        # 계측으로 정한 하한이 조용히 무효가 된다.
        embedding = await self._embedder.embed_query(query)

        current = await self._current_versions()
        if not current:
            # 여기서 끊어야 문서가 하나도 없는 경우가 저장소 왕복조차 만들지 않는다.
            return RetrievalResult(query=query, top_k=effective_k, chunks=(), target_documents=0)

        scored = await self._store.query(
            embedding,
            top_k=effective_k,
            versions=[StoredIndexVersion.of(document) for document in current.values()],
        )

        fresh = await self._drop_superseded(scored, current)
        kept = tuple(chunk for chunk in fresh if chunk.score >= self._min_score)

        return RetrievalResult(
            query=query,
            top_k=effective_k,
            chunks=kept,
            target_documents=len(current),
        )

    # ── 대상 집합 ───────────────────────────────────────────────────────

    async def _current_versions(self) -> dict[str, Document]:
        """지금 검색해도 되는 문서만 `document_id → 레코드` 로.

        판정을 레코드에 맡긴 이유는 여기 따로 구현하면 수집과 두 벌이 되기 때문이다."""
        documents = await self._registry.list_all()
        return {
            document.document_id: document
            for document in documents
            if document.is_searchable_under(self._index_signature)
        }

    # ── 현재성 재검증 ───────────────────────────────────────────────────

    async def _drop_superseded(
        self, scored: Sequence[ScoredChunk], filtered_with: dict[str, Document]
    ) -> list[ScoredChunk]:
        """질의 뒤에 밀려난 리비전의 결과를 버린다.

        레지스트리 읽기와 질의 사이가 잠겨 있지 않아 생기는 창을 좁힌다 (`ARCHITECTURE.md`)."""
        contributors = list(dict.fromkeys(chunk.document_id for chunk in scored))
        if not contributors:
            return list(scored)

        # 기여한 문서만 다시 읽는다 — 전체를 읽으면 결과에 없는 문서의 변경까지 기다린다.
        records = await asyncio.gather(
            *(self._registry.get(document_id) for document_id in contributors)
        )

        superseded = {
            document_id
            for document_id, record in zip(contributors, records, strict=True)
            if not self._is_still_current(record, filtered_with[document_id])
        }
        if not superseded:
            return list(scored)

        logger.info(
            "검색 도중 바뀐 문서의 결과를 버렸습니다",
            extra={"superseded_documents": sorted(superseded)},
        )
        return [chunk for chunk in scored if chunk.document_id not in superseded]

    @staticmethod
    def _is_still_current(record: Document | None, filtered_with: Document) -> bool:
        """필터에 쓴 삼중항이 지금도 그 문서의 현재 값인가.

        기준이 필터에 실제로 쓴 레코드인 것은 질문이 "그 세대가 아직 현재인가"라서다."""
        return record is not None and record.is_up_to_date(
            revision=filtered_with.revision,
            index_signature=filtered_with.index_signature,
        )
