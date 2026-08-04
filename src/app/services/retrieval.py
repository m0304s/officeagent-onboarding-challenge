"""검색 오케스트레이션 — 대상 집합 → retriever 팬아웃 → 융합 → 재검증 → 상위 K.

대상 집합이 팬아웃보다 앞이고 요청당 한 번인 것이 계약이다. 답변은 만들지 않는다
(`ARCHITECTURE.md` 검색 파이프라인).
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.adapters.protocols import DocumentRegistry, Reranker, Retriever
from app.core.documents import Document, StoredIndexVersion
from app.core.exceptions import AppError, ConfigurationError, StorageUnavailable
from app.core.fusion import DEFAULT_RRF_K, FusedItem, FusionInput, fuse
from app.core.reranking import ORDERED_BY_FUSION, ORDERED_BY_RERANK, reorder, targets
from app.core.retrieval import RetrievedChunk, ScoredChunk

logger = logging.getLogger(__name__)

#: 리랭킹 없이 도는 구성이 통과하는 값. 실제 깊이·상한은 배선이 설정에서 넣는다.
DEFAULT_RERANK_CANDIDATES = 30
DEFAULT_RERANK_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RetrieverBinding:
    """활성 retriever 하나 — 구성값과 구현체를 묶는다.

    이름·가중치가 구현체 밖에 있는 이유는 구현체가 자기 비중을 알 이유가 없기 때문이다."""

    name: str
    weight: float
    candidate_depth: int
    required: bool
    retriever: Retriever


@dataclass(frozen=True)
class RetrievalResult:
    """검색 한 건이 무엇을 돌려주는가.

    `target_documents` 를 함께 싣는 이유는 빈 결과의 원인이 그 값으로 갈리기 때문이다."""

    query: str
    top_k: int
    chunks: tuple[ScoredChunk, ...]
    target_documents: int
    #: 이번 검색에서 융합에 목록을 실어 보낸 retriever 이름들. 실패한 것은 빠진다
    retrievers: tuple[str, ...] = ()
    #: 이번 검색에서 실제로 순서를 정한 리랭커 이름. 축소됐거나 꺼져 있으면 `None`
    reranker: str | None = None
    #: 리랭커에 실제로 넘긴 후보 수. 로그가 깊이와 융합 결과 크기를 함께 읽는 자리다
    reranked_candidates: int = 0

    @property
    def count(self) -> int:
        return len(self.chunks)

    @property
    def ordered_by(self) -> str:
        """무엇이 순서를 정했는가. 이름과 따로 두면 둘이 어긋난 응답이 나갈 수 있다."""
        return ORDERED_BY_RERANK if self.reranker else ORDERED_BY_FUSION

    @property
    def top_score(self) -> float | None:
        """1위의 융합 점수. 유사도가 아니라 retriever 들의 합의 정도다."""
        return self.chunks[0].score if self.chunks else None


class RetrievalService:
    """질의 하나를 활성 retriever 전부에 보내고 융합 결과 상위 K개를 돌려준다."""

    def __init__(
        self,
        retrievers: Sequence[RetrieverBinding],
        registry: DocumentRegistry,
        *,
        index_signature: str,
        top_k: int,
        rrf_k: int = DEFAULT_RRF_K,
        reranker: Reranker | None = None,
        rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
        rerank_timeout_seconds: float = DEFAULT_RERANK_TIMEOUT_SECONDS,
    ) -> None:
        if not retrievers:
            raise ConfigurationError("활성 retriever 가 하나도 없습니다")
        self._bindings = tuple(retrievers)
        self._registry = registry
        # 선택 의존성이다 — `None` 이면 리랭킹 단계 자체가 없고 결과는 융합 순서 그대로다.
        self._reranker = reranker
        self._rerank_candidates = rerank_candidates
        self._rerank_timeout_seconds = rerank_timeout_seconds
        # 수집이 쓴 것과 같은 값이어야 해 배선이 한 번 유도해 넣는다 — 각자 유도하면
        # 한쪽만 고쳐진 순간 방금 올린 문서가 오류 없이 검색되지 않는다.
        self._index_signature = index_signature
        self._top_k = top_k
        self._rrf_k = rrf_k

    @property
    def index_signature(self) -> str:
        """이 서비스가 묶여 있는 색인 세대.

        캐시가 배선에서 따로 받으면 유도 지점이 셋이 되어 조용히 어긋난다 (design 결정 14)."""
        return self._index_signature

    @property
    def rerank_signature(self) -> str:
        """이 서비스가 쓰는 리랭커의 서명. 리랭커가 없으면 빈 문자열이다.

        빈 값도 값이다 — 켜고 끈 두 구성이 캐시에서 갈리는 것이 여기서 나온다."""
        return self._reranker.signature if self._reranker else ""

    def resolve_top_k(self, top_k: int | None) -> int:
        """요청의 K 를 확정한다 — 생략하면 설정 기본값.

        규칙이 두 벌이면 `retrieval_top_k` 를 바꿨을 때 검색과 캐시가 다른 K 를 믿는다."""
        return self._top_k if top_k is None else top_k

    async def search(self, query: str, *, top_k: int | None = None) -> RetrievalResult:
        """질의 한 건. `top_k` 를 주면 설정 기본값을 대신한다.

        유효성은 API 계층이 이미 봤다 — 거부된 요청이 저장소를 건드리지 않게 한다."""
        effective_k = self.resolve_top_k(top_k)

        current = await self._current_versions()
        if not current:
            # 여기서 끊어야 문서가 하나도 없는 경우가 저장소 왕복도 임베딩도 만들지 않는다.
            return RetrievalResult(query=query, top_k=effective_k, chunks=(), target_documents=0)

        # 목록 하나를 만들어 전부에게 같은 것을 넘긴다. 각자 계산하면 레지스트리를 읽는
        # 시점이 갈려 융합 결과에 두 세대가 섞인다.
        versions = [StoredIndexVersion.of(document) for document in current.values()]

        sources = await self._fan_out(query, versions)
        fused = [_assemble(entry) for entry in fuse(sources, rrf_k=self._rrf_k)]

        # 재검증 앞이어야 재검증과 조립 사이에 모델 순전파가 끼어들지 않는다 — 그 창으로
        # 새어 나간 리비전은 어떤 무효화에도 걸리지 않는다.
        ordered, reranker, reranked = await self._rerank(query, fused)

        fresh = await self._drop_superseded(ordered, current)
        return RetrievalResult(
            query=query,
            top_k=effective_k,
            # 절단이 재검증 뒤인 것이 계약이다 — 앞에 두면 밀려난 리비전이 자리를 차지한
            # 만큼 결과가 비어 돌아온다.
            chunks=tuple(fresh[:effective_k]),
            target_documents=len(current),
            retrievers=tuple(source.name for source in sources),
            reranker=reranker,
            reranked_candidates=reranked,
        )

    # ── 리랭킹 ──────────────────────────────────────────────────────────

    async def _rerank(
        self, query: str, fused: Sequence[ScoredChunk]
    ) -> tuple[list[ScoredChunk], str | None, int]:
        """융합 상위 후보를 다시 읽어 순서를 세운다. 요청당 한 번이다.

        돌지 않았거나 축소된 경우 이름이 `None` 이라 응답이 그 사실을 밝힌다."""
        if self._reranker is None or not fused:
            return list(fused), None, 0

        head = targets(fused, depth=self._rerank_candidates)
        try:
            scores = await asyncio.wait_for(
                self._reranker.rerank(query, [chunk.text for chunk in head]),
                timeout=self._rerank_timeout_seconds,
            )
            ordered = reorder(fused, scores, depth=self._rerank_candidates)
        # 취소는 축소 대상이 아니다 — `BaseException` 이라 여기 걸리지 않는다.
        except Exception as failure:
            logger.warning(
                "리랭킹에 실패해 융합 순서로 돌려줍니다",
                exc_info=failure,
                extra={"reranker": self._reranker.name, "rerank_candidates": len(head)},
            )
            return list(fused), None, 0
        return ordered, self._reranker.name, len(head)

    # ── 팬아웃 ──────────────────────────────────────────────────────────

    async def _fan_out(
        self, query: str, versions: Sequence[StoredIndexVersion]
    ) -> list[FusionInput[RetrievedChunk]]:
        """활성 retriever 전부를 겹쳐 돌리고 성공한 목록만 융합 입력으로 만든다.

        둘 다 블로킹을 스레드로 내보내므로 지연이 합이 아니라 최댓값에 가까워진다."""
        outcomes = await asyncio.gather(
            *(
                binding.retriever.retrieve(
                    query, depth=binding.candidate_depth, versions=versions
                )
                for binding in self._bindings
            ),
            return_exceptions=True,
        )

        sources: list[FusionInput[RetrievedChunk]] = []
        for binding, outcome in zip(self._bindings, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                self._dispose_of(binding, outcome)
                continue
            sources.append(
                FusionInput(name=binding.name, weight=binding.weight, items=tuple(outcome))
            )

        if not sources:
            # 전부 선택이어도 전부 실패는 장애다. 빈 결과로 위장하면 아무도 눈치채지 못한다.
            raise StorageUnavailable("검색에 쓸 수 있는 저장소가 없습니다")
        return sources

    def _dispose_of(self, binding: RetrieverBinding, failure: BaseException) -> None:
        """실패 하나를 처분한다 — 필수는 올리고 선택은 목록에서 뺀다.

        빈 목록으로 대신 넣지 않는다. 근거는 `retrieval` 스펙의 부분 실패 요구사항이다."""
        # 취소는 처분 대상이 아니다 — `gather` 가 잡아 온 것을 그대로 올린다.
        if not isinstance(failure, Exception):
            raise failure

        if binding.required:
            if isinstance(failure, AppError):
                raise failure
            # 도메인 예외가 아니면 `503` 보증이 성립하지 않는다 — 경계에서 바꿔 준다.
            raise StorageUnavailable("필수 retriever 가 실패했습니다") from failure

        logger.warning(
            "선택 retriever 가 실패해 이번 검색에서 제외했습니다",
            exc_info=failure,
            extra={"retriever": binding.name},
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


def _assemble(entry: FusedItem[RetrievedChunk]) -> ScoredChunk:
    """융합 결과 한 줄을 응답용 값 객체로. 기여 내역이 그대로 실린다."""
    chunk = entry.item
    return ScoredChunk(
        document_id=chunk.document_id,
        revision=chunk.revision,
        index_signature=chunk.index_signature,
        chunk_index=chunk.chunk_index,
        text=chunk.text,
        location=chunk.location,
        filename=chunk.filename,
        format=chunk.format,
        score=entry.score,
        contributions=entry.contributions,
    )
