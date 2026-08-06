"""검색 오케스트레이션 — 대상 집합 → retriever 팬아웃 → 융합 → 채택 → 상위 K 재검증.

대상 집합이 팬아웃보다 앞이고 요청당 한 번인 것이 계약이다. 답변은 만들지 않는다
(`ARCHITECTURE.md` 검색 파이프라인).
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.adapters.protocols import DocumentRegistry, Retriever
from app.core.documents import Document, StoredIndexVersion
from app.core.exceptions import AppError, ConfigurationError, StorageUnavailable
from app.core.fusion import DEFAULT_RRF_K, FusedItem, FusionInput, fuse
from app.core.retrieval import RetrievedChunk, ScoredChunk

logger = logging.getLogger(__name__)


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
    #: 이번 검색이 쓴 질의 벡터. 캐시 저장이 이것을 받아 같은 질의를 다시 인코딩하지 않는다.
    query_embedding: tuple[float, ...] | None = None

    @property
    def count(self) -> int:
        return len(self.chunks)

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
    ) -> None:
        if not retrievers:
            raise ConfigurationError("활성 retriever 가 하나도 없습니다")
        self._bindings = tuple(retrievers)
        self._registry = registry
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

    def resolve_top_k(self, top_k: int | None) -> int:
        """요청의 K 를 확정한다 — 생략하면 설정 기본값.

        규칙이 두 벌이면 `retrieval_top_k` 를 바꿨을 때 검색과 캐시가 다른 K 를 믿는다."""
        return self._top_k if top_k is None else top_k

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        query_embedding: Sequence[float] | None = None,
    ) -> RetrievalResult:
        """질의 한 건. `top_k` 는 설정 기본값을, `query_embedding` 은 자기 인코딩을 대신한다.

        유효성은 API 계층이 이미 봤다 — 거부된 요청이 저장소를 건드리지 않게 한다."""
        effective_k = self.resolve_top_k(top_k)

        current = await self._current_versions()
        if not current:
            # 여기서 끊어야 문서가 하나도 없는 경우가 저장소 왕복도 임베딩도 만들지 않는다.
            return RetrievalResult(query=query, top_k=effective_k, chunks=(), target_documents=0)

        # 목록 하나를 만들어 전부에게 같은 것을 넘긴다. 각자 계산하면 레지스트리를 읽는
        # 시점이 갈려 융합 결과에 두 세대가 섞인다.
        versions = [StoredIndexVersion.of(document) for document in current.values()]

        embedding, bindings = await self._prepare_query(query, query_embedding)
        sources = await self._fan_out(query, versions, embedding, bindings)
        # 채택은 융합 뒤·절단 전이다. 게이트를 목록에서 미리 잘라 대신하면 두 목록의
        # 교집합이 두 게이트의 교집합으로 좁아져 융합이 합의를 세지 못한다.
        admitted = _gate_verdicts(sources)
        fused = [
            _assemble(entry)
            for entry in fuse(sources, rrf_k=self._rrf_k)
            if admitted[(entry.item.document_id, entry.item.chunk_index)]
        ]

        return RetrievalResult(
            query=query,
            top_k=effective_k,
            chunks=tuple(await self._take_current(fused, current, effective_k)),
            target_documents=len(current),
            retrievers=tuple(source.name for source in sources),
            query_embedding=None if embedding is None else tuple(embedding),
        )

    # ── 질의 벡터 ───────────────────────────────────────────────────────

    async def _prepare_query(
        self, query: str, given: Sequence[float] | None
    ) -> tuple[Sequence[float] | None, tuple[RetrieverBinding, ...]]:
        """벡터를 쓰는 retriever 하나에게 질의 벡터를 한 번 만들게 한다.

        만드는 데 실패한 retriever 는 이번 팬아웃에서도 빠진다 — 벡터 없이는 어차피 죽는다."""
        if given is not None:
            return given, self._bindings

        vector: Sequence[float] | None = None
        surviving: list[RetrieverBinding] = []
        for binding in self._bindings:
            if vector is not None:
                surviving.append(binding)
                continue
            try:
                vector = await binding.retriever.query_vector(query)
            except Exception as exc:
                self._dispose_of(binding, exc)
                continue
            surviving.append(binding)
        return vector, tuple(surviving)

    # ── 팬아웃 ──────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        query: str,
        versions: Sequence[StoredIndexVersion],
        embedding: Sequence[float] | None,
        bindings: Sequence[RetrieverBinding],
    ) -> list[FusionInput[RetrievedChunk]]:
        """활성 retriever 전부를 겹쳐 돌리고 성공한 목록만 융합 입력으로 만든다.

        둘 다 블로킹을 스레드로 내보내므로 지연이 합이 아니라 최댓값에 가까워진다."""
        outcomes = await asyncio.gather(
            *(
                binding.retriever.retrieve(
                    query,
                    depth=binding.candidate_depth,
                    versions=versions,
                    embedding=embedding,
                )
                for binding in bindings
            ),
            return_exceptions=True,
        )

        sources: list[FusionInput[RetrievedChunk]] = []
        for binding, outcome in zip(bindings, outcomes, strict=True):
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

    async def _take_current(
        self, scored: Sequence[ScoredChunk], filtered_with: dict[str, Document], limit: int
    ) -> list[ScoredChunk]:
        """앞에서부터 `limit` 개를 채운다 — 창 하나를 재검증하고 버려진 만큼 다음 창을 연다.

        레지스트리 읽기와 질의 사이가 잠겨 있지 않아 생기는 창을 좁힌다 (`ARCHITECTURE.md`)."""
        verdicts: dict[str, bool] = {}
        kept: list[ScoredChunk] = []
        cursor = 0

        while len(kept) < limit and cursor < len(scored):
            window = scored[cursor : cursor + (limit - len(kept))]
            cursor += len(window)
            await self._record_verdicts(window, filtered_with, verdicts)
            kept.extend(chunk for chunk in window if verdicts[chunk.document_id])

        superseded = sorted(
            document_id for document_id, is_current in verdicts.items() if not is_current
        )
        if superseded:
            logger.info(
                "검색 도중 바뀐 문서의 결과를 버렸습니다",
                extra={"superseded_documents": superseded},
            )
        return kept

    async def _record_verdicts(
        self,
        window: Sequence[ScoredChunk],
        filtered_with: dict[str, Document],
        verdicts: dict[str, bool],
    ) -> None:
        """이 창에서 아직 판정이 없는 문서만 묻는다 — 판정은 요청 하나 안에서만 산다.

        조회 하나가 연결 하나라, 융합 목록 전체를 물으면 질의 하나가 문서 수만큼을 동시에 연다."""
        unknown = [
            document_id
            for document_id in dict.fromkeys(chunk.document_id for chunk in window)
            if document_id not in verdicts
        ]
        if not unknown:
            return

        records = await asyncio.gather(
            *(self._registry.get(document_id) for document_id in unknown)
        )
        verdicts.update(
            {
                document_id: self._is_still_current(record, filtered_with[document_id])
                for document_id, record in zip(unknown, records, strict=True)
            }
        )

    @staticmethod
    def _is_still_current(record: Document | None, filtered_with: Document) -> bool:
        """필터에 쓴 삼중항이 지금도 그 문서의 현재 값인가.

        기준이 필터에 실제로 쓴 레코드인 것은 질문이 "그 세대가 아직 현재인가"라서다."""
        return record is not None and record.is_up_to_date(
            revision=filtered_with.revision,
            index_signature=filtered_with.index_signature,
        )


def _gate_verdicts(
    sources: Sequence[FusionInput[RetrievedChunk]],
) -> dict[tuple[str, int], bool]:
    """항목마다 "자신을 실어 보낸 목록 중 하나라도 하한을 통과했는가".

    융합이 payload 를 한 벌만 남겨, 판정은 융합 밖에서 목록 간 OR 로 합친다."""
    verdicts: dict[tuple[str, int], bool] = {}
    for source in sources:
        for item in source.items:
            key = (item.document_id, item.chunk_index)
            verdicts[key] = verdicts.get(key, False) or item.gate_passed
    return verdicts


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
