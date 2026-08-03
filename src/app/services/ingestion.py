"""문서 수집 오케스트레이션 — 파싱 → 청킹 → 토큰 가드 → 임베딩·저장 → 커밋 → 정리.

순서가 이 파일의 전부다. 새로 쓰고 → 커밋 → 지우기이며, 뒤집으면 중간 실패에 문서가
통째로 사라진다 (`ARCHITECTURE.md` 문서 수집 파이프라인).
"""

import asyncio
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import anyio

from app.adapters.parsers import ParserRegistry
from app.adapters.protocols import DocumentRegistry, Embedder, VectorStore
from app.core.chunking import ChunkStrategy, clamp_overlap, get_splitter, resplit
from app.core.documents import (
    Chunk,
    Document,
    DocumentFormat,
    IndexStatus,
    IngestionStatus,
    StoredIndexVersion,
    TextChunk,
    derive_document_id,
    derive_revision,
    identify_chunks,
)
from app.core.exceptions import (
    DocumentNotFound,
    EmptyDocument,
    NoExtractableText,
    StorageUnavailable,
)

logger = logging.getLogger(__name__)

# 재분할이 내려갈 수 있는 바닥. 없으면 병적인 입력에서 루프가 끝나지 않는다.
_MIN_RESPLIT_SIZE = 32


@dataclass(frozen=True)
class ExtractionResult:
    """추출·분할까지 끝난 상태. 아직 임베딩되지도 저장되지도 않았다.

    `revision` 을 여기서 계산해 원본 바이트를 놓는다 — 메모리를 문서 크기에서 뗀다."""

    document_id: str
    filename: str
    format: DocumentFormat
    revision: str
    byte_size: int
    page_count: int | None
    chunks: tuple[TextChunk, ...]

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


@dataclass(frozen=True)
class IngestionResult:
    """수집 요청 하나가 저장소에 무슨 일을 했는가."""

    document: Document
    status: IngestionStatus
    #: 내용이 바뀐 교체에서만 값이 있다. 재색인은 `revision` 이 그대로라 값이 없다 —
    #: 현재 `revision` 과 같은 값을 "이전"이라고 부르면 응답이 자기모순이 된다.
    previous_revision: str | None = None
    page_count: int | None = None


@dataclass(frozen=True)
class ReconciliationReport:
    """기동 정리가 한 일."""

    stale_documents: tuple[str, ...] = ()
    removed_chunks: int = 0


class IngestionService:
    """업로드된 바이트를 검색 가능한 청크로 만들어 저장한다."""

    def __init__(
        self,
        parsers: ParserRegistry,
        embedder: Embedder,
        vector_store: VectorStore,
        registry: DocumentRegistry,
        *,
        index_signature: str,
        chunk_strategy: ChunkStrategy,
        chunk_size: int,
        chunk_overlap: int,
        embedding_batch_size: int,
        concurrency: int,
    ) -> None:
        self._parsers = parsers
        self._embedder = embedder
        self._store = vector_store
        self._registry = registry
        # 전략은 기동 시점에 함수로 해석해 둔다. 업로드마다 조회하면 등록 누락이
        # 첫 업로드에서야 드러난다.
        self._split = get_splitter(chunk_strategy)
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._batch_size = embedding_batch_size

        # 주입받는다 — 두 서비스가 각자 유도하면 한쪽만 고쳐졌을 때 어디에도 오류
        # 없이 수집이 쓰는 서명과 검색이 찾는 서명이 어긋난다.
        self.index_signature = index_signature

        # 총량만 제한한다 — 같은 문서의 직렬화는 `_document_locks` 의 몫이다.
        self._limiter = anyio.CapacityLimiter(concurrency)

        # 프로세스 로컬이라 워커 1 프로세스 전제 위에서만 성립한다. 회수하지 않는 것은
        # "잠금이 비었는가" 확인 자체가 새로운 경합 지점이기 때문이다.
        self._document_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ── 추출·분할 (저장소를 건드리지 않는 구간) ─────────────────────────

    async def extract_chunks(self, filename: str, data: bytes) -> ExtractionResult:
        """파일명과 바이트에서 청크까지 만든다.

        이 구간의 실패는 저장소를 손대기 전이라 되돌릴 것이 없다."""
        document_format, parser = self._parsers.resolve(filename)

        # 여기가 오프로드 지점이다 — 루프에서 돌리면 큰 PDF 하나가 헬스 응답까지
        # 멈춰 세운다. 파서를 동기로 선언한 이유가 이것이다.
        extracted = await asyncio.to_thread(parser.parse, data)

        if not extracted.has_text:
            raise self._no_text_error(filename, extracted.page_count)

        chunks = self._split(extracted.segments, self._chunk_size, self._chunk_overlap)
        if not chunks:
            # 분할 쪽 결함이다 — 저장하면 검색되지 않는 문서가 목록에만 남는다.
            logger.warning("추출된 텍스트가 있는데 청크가 만들어지지 않았습니다: %s", filename)
            raise EmptyDocument("문서에서 색인할 내용을 찾지 못했습니다")

        return ExtractionResult(
            document_id=derive_document_id(filename),
            filename=filename,
            format=document_format,
            revision=derive_revision(data),
            byte_size=len(data),
            page_count=extracted.page_count,
            chunks=chunks,
        )

    # ── 수집 유스케이스 ─────────────────────────────────────────────────

    async def ingest(self, filename: str, data: bytes) -> IngestionResult:
        """업로드 한 건을 끝까지 처리한다. 같은 문서 요청은 직렬화된다.

        잠금 구간이 조회부터 정리까지인 것은 삭제가 방금 쓴 청크를 지우지 않게 하려는 것이다."""
        # 미지원 파일이 잠금과 상한을 점유할 이유가 없고, 빈 파일명은
        # `derive_document_id` 자체가 성립하지 않는다.
        self._parsers.resolve(filename)

        document_id = derive_document_id(filename)
        revision = derive_revision(data)

        lock = await self._lock_for(document_id)
        async with lock:
            current = await self._registry.get(document_id)

            if self._is_unchanged(current, revision):
                logger.info(
                    "내용과 색인 구성이 모두 같아 재색인하지 않습니다",
                    extra={"document_id": document_id, "revision": revision[:12]},
                )
                return IngestionResult(document=current, status=IngestionStatus.UNCHANGED)

            # 동시성 상한은 실제로 일을 하는 구간에만 건다. 조회만 하고 끝나는
            # `unchanged` 요청이 상한을 점유하면 처리량이 이유 없이 떨어진다.
            async with self._limiter:
                return await self._index(document_id, filename, data, revision, current)

    async def list_documents(self) -> list[Document]:
        """수집된 문서 전체. 최근에 수집된 것이 앞이고 `stale` 도 포함한다.

        정렬을 어댑터에 안 맡기는 것은 순서가 계약이기 때문이다."""
        documents = await self._registry.list_all()
        return sorted(documents, key=lambda d: (-d.ingested_at.timestamp(), d.filename))

    async def get_document(self, document_id: str) -> Document:
        """문서 한 건. 없으면 `DocumentNotFound`."""
        record = await self._registry.get(document_id)
        if record is None:
            raise DocumentNotFound("수집된 적 없는 문서입니다")
        return record

    async def delete(self, document_id: str) -> Document:
        """문서와 그 청크를 전부 지운다. 없는 문서는 `DocumentNotFound`."""
        lock = await self._lock_for(document_id)
        async with lock:
            record = await self._registry.get(document_id)
            if record is None:
                raise DocumentNotFound("수집된 적 없는 문서입니다")

            # 청크를 먼저 지운다 — 뒤집으면 "삭제 성공 + 잔여 청크"가 관측될 수 있다.
            await self._store.delete_document(document_id)
            await self._registry.delete(document_id)

            logger.info(
                "문서를 삭제했습니다",
                extra={"document_id": document_id, "chunk_count": record.chunk_count},
            )
            return record

    # ── 기동 정리 ───────────────────────────────────────────────────────

    async def reconcile_storage(self) -> ReconciliationReport:
        """기동 시 벡터 스토어를 레지스트리에 맞춘다.

        정리에 실패해도 서비스는 뜬다 — 부수 작업이 "한 줄 실행"을 깨뜨리지 않게."""
        try:
            return await self._reconcile()
        except Exception as exc:
            logger.warning("기동 정리에 실패했습니다 — 기동은 계속합니다", exc_info=exc)
            return ReconciliationReport()

    async def _reconcile(self) -> ReconciliationReport:
        documents = await self._registry.list_all()
        stored = await self._store.list_stored_versions()
        stale: list[str] = []
        removed = 0

        # 규칙 2 — 색인 구성이 바뀐 문서. 원본을 보관하지 않아 자동 재색인이 불가능해
        # 지우고 `stale` 로 드러낸다. 지우고 나서 커밋해야 고아 청크가 안 남는다.
        for document in documents:
            if document.matches_index(self.index_signature):
                continue
            # 서명을 그대로 둬야 재업로드가 `unchanged` 단축에 안 걸린다. 이미 처리된
            # 문서를 다시 쓰지 않는 것은 빈 기동이 쓰기를 만들지 않게 하려는 것이다.
            stale.append(document.document_id)
            if document.index_status is IndexStatus.STALE and document.chunk_count == 0:
                continue
            removed += await self._store.delete_document(document.document_id)
            await self._registry.commit(
                replace(document, chunk_count=0, index_status=IndexStatus.STALE)
            )

        # 규칙 1 — 크래시 백스톱. 요청 안의 되돌리기가 닿지 못한 잔여물만 회수한다.
        current_versions = {
            StoredIndexVersion.of(document)
            for document in documents
            if document.matches_index(self.index_signature)
        }
        for version in stored:
            if version in current_versions:
                continue
            removed += await self._store.delete_document(
                version.document_id,
                revision=version.revision,
                index_signature=version.index_signature,
            )

        if stale:
            logger.warning(
                "색인 구성이 바뀌어 청크를 제거했습니다 — 다시 업로드해야 검색됩니다",
                extra={"stale_document_ids": stale, "index_signature": self.index_signature},
            )
        if removed:
            logger.info("기동 정리로 청크를 제거했습니다", extra={"removed_chunks": removed})
        return ReconciliationReport(stale_documents=tuple(stale), removed_chunks=removed)

    # ── 색인 (저장소를 바꾸는 구간) ─────────────────────────────────────

    async def _index(
        self,
        document_id: str,
        filename: str,
        data: bytes,
        revision: str,
        current: Document | None,
    ) -> IngestionResult:
        extraction = await self.extract_chunks(filename, data)

        # 토큰 계산은 토크나이저 호출이라 블로킹이다. 청크 수만큼 반복되므로
        # 한 번에 묶어 스레드풀로 내보낸다.
        guarded = await asyncio.to_thread(self._guard_tokens, extraction.chunks)
        chunks = identify_chunks(
            guarded,
            document_id=document_id,
            revision=revision,
            index_signature=self.index_signature,
        )

        document = Document(
            document_id=document_id,
            filename=filename,
            format=extraction.format,
            revision=revision,
            index_signature=self.index_signature,
            chunk_count=len(chunks),
            byte_size=len(data),
            ingested_at=datetime.now(UTC),
            index_status=IndexStatus.INDEXED,
        )

        try:
            await self._embed_and_store(chunks, filename, extraction.format)
            await self._registry.commit(document)  # ← 이 순간 교체가 확정된다
        except Exception as exc:
            logger.warning(
                "새 리비전을 저장하지 못했습니다",
                exc_info=exc,
                extra={"document_id": document_id, "revision": revision[:12]},
            )
            await self._roll_back(document_id, revision)
            raise StorageUnavailable("문서를 저장하지 못했습니다") from exc

        await self._purge_previous(current, revision)

        result = IngestionResult(
            document=document,
            status=IngestionStatus.of(current, revision),
            previous_revision=(
                current.revision if current and current.revision != revision else None
            ),
            page_count=extraction.page_count,
        )
        logger.info(
            "문서를 색인했습니다",
            extra={
                "document_id": document_id,
                "revision": revision[:12],
                "index_signature": self.index_signature,
                "chunk_count": len(chunks),
                "ingestion_status": result.status.value,
            },
        )
        return result

    async def _embed_and_store(
        self, chunks: Sequence[Chunk], filename: str, document_format: DocumentFormat
    ) -> None:
        """배치 단위로 인코딩하고 배치마다 쓴다.

        메모리를 배치 크기에 묶고, 배치 경계마다 루프가 다른 요청으로 넘어가게 한다."""
        for batch in _batches(chunks, self._batch_size):
            vectors = await self._embedder.embed_documents([chunk.text for chunk in batch])
            await self._store.add_chunks(
                batch, vectors, filename=filename, document_format=document_format
            )

    async def _roll_back(self, document_id: str, revision: str) -> None:
        """실패한 교체가 남긴 새 리비전 청크를 같은 요청 안에서 되돌린다.

        어댑터가 아니라 여기 있는 것은 배치 쓰기에 트랜잭션이 없기 때문이다."""
        try:
            removed = await self._store.delete_document(
                document_id, revision=revision, index_signature=self.index_signature
            )
        except Exception as exc:
            logger.warning(
                "되돌리기에 실패했습니다 — 잔여 청크는 다음 기동의 정리가 회수합니다",
                exc_info=exc,
                extra={"document_id": document_id, "revision": revision[:12]},
            )
            return
        if removed:
            logger.info(
                "실패한 교체의 새 리비전 청크를 되돌렸습니다",
                extra={"document_id": document_id, "removed_chunks": removed},
            )

    async def _purge_previous(self, current: Document | None, revision: str) -> None:
        """교체가 확정된 뒤 이전 세대의 청크를 지운다.

        여기서 실패해도 교체는 이미 성립했고, 검색이 현재 값으로 필터해 섞이지 않는다."""
        if current is None:
            return
        if (current.revision, current.index_signature) == (revision, self.index_signature):
            # 같은 세대를 다시 쓴 경우다 — 안 걸러내면 아래 삭제가 방금 쓴 청크를 지운다.
            return

        # 축을 하나만 고른다 — 정확한 짝만 지우면 다른 축의 이전 세대가 살아남는다.
        if current.revision != revision:
            selector = {"revision": current.revision}
        else:
            selector = {"index_signature": current.index_signature}

        try:
            removed = await self._store.delete_document(current.document_id, **selector)
        except Exception as exc:
            logger.warning(
                "이전 청크 정리에 실패했습니다 — 교체 자체는 이미 성립했습니다",
                exc_info=exc,
                extra={"document_id": current.document_id},
            )
            return
        if removed:
            logger.info(
                "이전 세대의 청크를 정리했습니다",
                extra={"document_id": current.document_id, "removed_chunks": removed},
            )

    # ── 판정 ────────────────────────────────────────────────────────────

    def _is_unchanged(self, current: Document | None, revision: str) -> bool:
        """색인을 아예 시작하지 않아도 되는가.

        판정은 레코드가 하고 여기 남는 것은 저장 경로에 진입하지 않는다는 결정뿐이다."""
        return current is not None and current.is_up_to_date(
            revision=revision, index_signature=self.index_signature
        )

    @staticmethod
    def _no_text_error(filename: str, page_count: int | None) -> Exception:
        """텍스트 레이어 부재와 내용 부재를 가른다 — 뭉개면 OCR 필요를 알 수 없다."""
        if page_count:
            logger.info("텍스트 레이어가 없는 PDF: %s (%d쪽)", filename, page_count)
            return NoExtractableText(
                "문서에 텍스트 레이어가 없습니다. 이 서비스는 이미지 인식(OCR)을 수행하지 않습니다",
                page_count=page_count,
            )
        return EmptyDocument("문서에 내용이 없습니다")

    # ── 토큰 가드 ───────────────────────────────────────────────────────

    def _guard_tokens(self, chunks: Sequence[TextChunk]) -> tuple[TextChunk, ...]:
        """블로킹. 스레드풀에서만 호출한다.

        크기가 문자 기준이라 토큰 수를 보장하지 못하고, 넘기면 뒷부분이 조용히 잘린다."""
        limit = self._embedder.max_input_tokens
        guarded: list[TextChunk] = []
        for chunk in chunks:
            if self._embedder.count_document_tokens(chunk.text) <= limit:
                guarded.append(chunk)
                continue
            guarded.extend(self._shrink(chunk, limit))
        return tuple(guarded)

    def _shrink(self, chunk: TextChunk, limit: int) -> tuple[TextChunk, ...]:
        """상한을 넘는 청크를 문자 크기를 반씩 줄여가며 다시 쪼갠다."""
        size = self._chunk_size
        pieces: tuple[TextChunk, ...] = (chunk,)
        while size > _MIN_RESPLIT_SIZE:
            size = max(_MIN_RESPLIT_SIZE, size // 2)
            pieces = resplit(
                chunk, size=size, overlap=clamp_overlap(size=size, preferred=self._chunk_overlap)
            )
            if all(self._embedder.count_document_tokens(piece.text) <= limit for piece in pieces):
                logger.info(
                    "토큰 상한을 넘는 청크를 다시 쪼갰습니다",
                    extra={"resplit_size": size, "resplit_pieces": len(pieces)},
                )
                return pieces

        # 실패시키면 병적인 텍스트 한 조각이 문서 전체의 수집을 막는다. 로그로
        # 드러내고 진행한다 — 조용히 잘리는 것과 달리 이건 남는다.
        logger.warning(
            "재분할로도 토큰 상한을 맞추지 못했습니다 — 임베딩에서 잘릴 수 있습니다",
            extra={"resplit_size": size, "resplit_pieces": len(pieces)},
        )
        return pieces

    # ── 문서 단위 잠금 ──────────────────────────────────────────────────

    async def _lock_for(self, document_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._document_locks.get(document_id)
            if lock is None:
                lock = self._document_locks[document_id] = asyncio.Lock()
            return lock


def _batches(items: Sequence[Chunk], size: int) -> Iterator[Sequence[Chunk]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
