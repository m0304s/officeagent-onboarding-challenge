"""문서 수집 오케스트레이션.

```
파서 선택 → 파싱(오프로드) → 청킹 → 토큰 가드 → 배치 임베딩·저장 → 커밋 → 이전 청크 정리
```

순서가 이 파일의 전부다. **새로 쓰고 → 커밋 → 지우기**이며, 반대 순서(먼저 지우고 새로
쓰기)를 쓰면 중간 실패 시 문서가 통째로 사라진다. 답할 수 있던 질문에 답할 수 없게 되는
것이, 잔여 청크가 잠시 남는 것보다 훨씬 나쁘다.

계층 규칙: 이 모듈은 어댑터의 **프로토콜**만 알고 구현체를 모른다. 넷 전부 생성자로
주입받으므로 PDF 파서·임베딩 모델·벡터 스토어를 갈아끼워도 이 파일은 바뀌지 않는다.
"""

import asyncio
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import anyio

from app.adapters.parsers import ParserRegistry
from app.adapters.protocols import DocumentRegistry, Embedder, VectorStore
from app.core.chunking import ChunkStrategy, get_splitter, resplit
from app.core.documents import (
    Chunk,
    Document,
    DocumentFormat,
    IndexStatus,
    IngestionStatus,
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

# 토큰 가드가 재분할할 때 내려갈 수 있는 문자 크기의 바닥.
#
# 이보다 작게 쪼개도 토큰 상한을 못 맞추는 텍스트는 사실상 없다(32자가 512토큰을
# 넘으려면 문자당 16토큰이어야 한다). 바닥을 두지 않으면 병적인 입력에서 루프가
# 끝나지 않는다.
_MIN_RESPLIT_SIZE = 32


@dataclass(frozen=True)
class ExtractionResult:
    """추출·분할까지 끝난 상태.

    아직 임베딩되지도 저장되지도 않았다. `revision` 을 여기서 계산해 두는 이유는
    원본 바이트가 이 지점 이후로 필요 없어지기 때문이다 — 뒤 단계까지 바이트를
    들고 다니면 메모리가 배치 크기가 아니라 문서 크기에 묶인다.
    """

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

        # 색인 서명은 **주입받는다.** 여전히 기동 시점에 한 번 고정되는 상수지만,
        # 유도하는 곳이 배선으로 올라갔다. 검색도 같은 값으로 필터하는데 두 서비스가
        # 각자 유도하면 재료 목록이 둘이 되고, 한쪽만 고쳐진 순간 수집이 쓰는 서명과
        # 검색이 찾는 서명이 어긋난다 — 각자 자기 기준으로는 옳아서 어디에도 오류가
        # 남지 않는다.
        self.index_signature = index_signature

        # 수집 동시성 총량. 상한에 걸린 요청은 실패하지 않고 대기한다.
        # **같은 문서의 직렬화는 이것이 담당하지 않는다** — 총량만 제한할 뿐 같은
        # 문서 두 건을 그대로 통과시킨다. 그건 아래 `_document_locks` 의 몫이다.
        self._limiter = anyio.CapacityLimiter(concurrency)

        # `document_id` 단위 잠금. 프로세스 로컬이며 워커 1 프로세스 전제 위에서만
        # 성립한다(다중 워커 경계는 ARCHITECTURE.md 참조). 문서 수만큼 항목이 남지만
        # 수십 건 규모라 회수하지 않는다 — 회수하려면 "잠금이 비었는가"를 원자적으로
        # 확인해야 하고, 그 자체가 새로운 경합 지점이 된다.
        self._document_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ── 추출·분할 (저장소를 건드리지 않는 구간) ─────────────────────────

    async def extract_chunks(self, filename: str, data: bytes) -> ExtractionResult:
        """파일명과 바이트에서 청크까지 만든다.

        미지원 포맷은 `UnsupportedDocumentFormat`, 내용이 없으면 `EmptyDocument`,
        쪽은 있는데 텍스트 레이어가 없으면 `NoExtractableText` 로 끝난다. 이 구간의
        실패는 저장소를 손대기 **전**이므로 되돌릴 것이 없다.
        """
        document_format, parser = self._parsers.resolve(filename)

        # **여기가 오프로드 지점이다.** 파싱은 CPU 바운드이고 문서 크기에 비례한다.
        # 이벤트 루프에서 그냥 돌리면 20 MiB PDF 하나가 헬스 응답까지 멈춰 세운다.
        # 파서 프로토콜을 동기로 선언한 이유가 이것이다 — 호출부가 오프로드를
        # 의식할 수밖에 없게 만든다.
        extracted = await asyncio.to_thread(parser.parse, data)

        if not extracted.has_text:
            raise self._no_text_error(filename, extracted.page_count)

        chunks = self._split(extracted.segments, self._chunk_size, self._chunk_overlap)
        if not chunks:
            # 세그먼트는 있는데 청크가 0개면 분할 쪽 결함이다. 청크 0개인 문서를
            # 저장하면 검색되지 않는 문서가 목록에만 남는다.
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
        """업로드 한 건을 끝까지 처리한다.

        같은 문서에 대한 요청은 직렬화된다. 잠금 구간은 **레지스트리 조회부터 이전
        청크 정리 완료까지**다 — 조회와 커밋 사이가 열려 있으면 두 요청의
        `get → 저장 → 커밋 → 삭제` 가 인터리빙되어, 마지막 커밋이 아닌 쪽의 청크가
        잔여물이 되거나 삭제가 방금 쓴 새 청크를 지운다.
        """
        # 포맷 판정을 가장 먼저 한다. 미지원 파일이 잠금과 동시성 상한을 점유할 이유가
        # 없고, 무엇보다 파일명이 비어 있으면 `derive_document_id` 자체가 성립하지
        # 않는다 — 그 경우도 "확장자가 없다"와 같은 사건이므로 같은 예외로 끝나야 한다.
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
        """수집된 문서 전체. 최근에 수집된 것이 앞이다.

        `stale` 문서도 포함한다. 청크가 없어 검색되지 않는 문서라도 목록에서 사라지면
        무엇을 다시 올려야 하는지 알 방법이 없어진다.

        정렬을 어댑터에 맡기지 않는 이유는 순서가 계약이기 때문이다. 레지스트리 구현이
        바뀌었다고 응답 순서가 바뀌면 그건 구현 세부가 API 로 샌 것이다.
        """
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

            # **청크를 먼저 지운다.** 순서를 뒤집으면 레지스트리에서 사라진 뒤 청크
            # 삭제가 실패했을 때 "삭제 성공 + 잔여 청크"가 관측될 수 있다. 이쪽
            # 순서에서는 청크 삭제가 실패하면 응답 자체가 실패하므로 그 상태가 없다.
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

        **기동을 실패시키지 않는다.** 정리에 실패해도 서비스는 떠야 한다 — 평가자가
        설정을 한 번 만져 본 순간 "한 줄 실행"이 깨지면 안 된다.
        """
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

        # **규칙 2 — 색인 구성이 바뀐 문서.**
        #
        # `unchanged` 단축은 재업로드가 있을 때만 작동한다. 모델을 바꾸고 아무도
        # 아무것도 올리지 않으면 구 구성 벡터가 그대로 살아서 검색되고 아무도 그
        # 사실을 모른다. 원본 바이트를 보관하지 않으므로 자동 재색인은 불가능하다 —
        # 지우고 `stale` 로 드러내는 것이 최선이다.
        #
        # **지우고 나서 커밋한다.** 반대로 하면 커밋 후 죽었을 때 `chunk_count: 0`
        # 인데 청크가 남아 있는, 아무도 지우지 않는 상태가 된다.
        for document in documents:
            if document.index_signature == self.index_signature:
                continue
            # 서명은 그대로 둔다 — 그래야 재업로드가 `unchanged` 단축에 걸리지 않고
            # 재색인으로 이어진다. 대신 이미 처리된 문서는 다시 쓰지 않는다. 매 기동마다
            # 같은 삭제와 커밋을 반복하면 아무것도 바뀌지 않은 기동이 쓰기를 만든다.
            stale.append(document.document_id)
            if document.index_status is IndexStatus.STALE and document.chunk_count == 0:
                continue
            removed += await self._store.delete_document(document.document_id)
            await self._registry.commit(
                replace(document, chunk_count=0, index_status=IndexStatus.STALE)
            )

        # **규칙 1 — 크래시 백스톱.**
        #
        # 정합성 수단이 아니다. 요청 안의 되돌리기가 닿지 못하는 실패(프로세스 강제
        # 종료, 되돌리기 자체의 실패)가 남긴 잔여물만 회수한다.
        current_versions = {
            (document.document_id, document.revision, document.index_signature)
            for document in documents
            if document.index_signature == self.index_signature
        }
        for version in stored:
            if (version.document_id, version.revision, version.index_signature) in current_versions:
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
            status=self._status_for(current, revision),
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

        청크 전체를 한 번에 넘기지 않는 이유는 **메모리가 문서 크기가 아니라 배치
        크기에 비례해야** 하기 때문이다. 업로드 상한(20 MiB) 안의 문서도 청크가 수만
        개일 수 있고, 한 번에 인코딩하면 결과 벡터만 수십 MB 에 모델 내부 버퍼가 더해진다.

        배치마다 인코딩과 쓰기가 각각 스레드풀을 거치므로, 그 지점에서 이벤트 루프가
        다른 요청으로 넘어간다. 임베딩 단계도 다른 요청을 굶기지 않는다.
        """
        for batch in _batches(chunks, self._batch_size):
            vectors = await self._embedder.embed_documents([chunk.text for chunk in batch])
            await self._store.add_chunks(
                batch, vectors, filename=filename, document_format=document_format
            )

    async def _roll_back(self, document_id: str, revision: str) -> None:
        """실패한 교체가 남긴 새 리비전 청크를 같은 요청 안에서 되돌린다.

        되돌리기를 어댑터가 아니라 여기 두는 이유: 벡터 스토어의 배치 쓰기는
        트랜잭션을 보장하지 않으므로, 프로토콜에 "`add_chunks` 는 원자적이어야 한다"고
        적어도 구현이 지킬 수단이 없다. 지킬 수 없는 약속을 계약에 적는 대신, 교체
        순서를 아는 곳이 되돌리기도 책임진다. 배치로 나뉘면서 부분 기록의 폭이 커졌으므로
        이 되돌리기는 선택이 아니다.

        **되돌리기 실패는 응답을 바꾸지 않는다.** 잔여 청크는 레지스트리가 가리키지
        않아 검색 대상이 아니고, 다음 기동의 정리가 회수한다.
        """
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

        **여기서 실패해도 교체는 이미 성립했다.** 이전 청크가 잔여물로 남지만 검색은
        현재 `(revision, index_signature)` 로 필터하므로 결과에 섞이지 않는다. 요청을
        실패시키면 이미 성립한 교체를 되돌릴 수 없으면서 호출자만 혼란스러워진다.
        """
        if current is None:
            return
        if (current.revision, current.index_signature) == (revision, self.index_signature):
            # 같은 세대를 다시 쓴 경우다(청크 id 가 같아 이미 덮어썼다). 이 조건으로
            # 걸러내지 않으면 아래 삭제가 **방금 쓴 청크**를 지운다.
            return

        # 지우는 축을 하나만 고른다. 내용이 바뀌었으면 이전 `revision` 전체를 —
        # 그 리비전의 서명이 무엇이든 — 지우고, 내용이 같으면(재색인) 이전 서명
        # 전체를 지운다. 정확한 짝만 지우면 다른 축에 남은 이전 세대가 살아남는다.
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
        """단축 조건은 `revision` 과 `index_signature` **둘 다**다.

        `revision` 만 보면 모델이나 청킹을 바꾼 뒤 같은 파일을 다시 올려도 아무 일이
        일어나지 않는다. 구 구성으로 만든 벡터가 그대로 남고 사용자는 재색인했다고 믿는다.
        """
        return (
            current is not None
            and current.revision == revision
            and current.index_signature == self.index_signature
            # 기동 정리는 `stale` 문서의 서명을 그대로 두므로 위 조건에서 이미
            # 걸러지지만, 청크가 0개인 문서를 "변경 없음"이라고 답하는 일은 어떤
            # 경로로도 일어나면 안 된다.
            and current.index_status is IndexStatus.INDEXED
        )

    @staticmethod
    def _status_for(current: Document | None, revision: str) -> IngestionStatus:
        if current is None:
            return IngestionStatus.CREATED
        if current.revision != revision:
            return IngestionStatus.REPLACED
        # 내용은 같은데 여기까지 왔다 = 색인 구성이 달라졌거나 `stale` 이다.
        return IngestionStatus.REINDEXED

    @staticmethod
    def _no_text_error(filename: str, page_count: int | None) -> Exception:
        """쪽은 있는데 텍스트가 없는 경우와 내용 자체가 없는 경우를 가른다.

        뭉개면 클라이언트는 OCR 이 필요한 것인지 파일이 잘못된 것인지 구분할 수 없다.
        """
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

        청크 크기는 **문자 기준**이라(계층 규칙상 `core/` 가 토크나이저를 알 수 없다)
        토큰 수를 보장하지 못한다. 상한을 넘는 청크를 그대로 임베딩에 넘기면 뒷부분이
        **조용히 잘린다** — 잘린 텍스트는 벡터에 반영되지 않으면서 청크 본문에는 남아,
        저장은 됐는데 검색되지 않는 텍스트가 생긴다.

        토큰 계산은 토크나이저를 아는 어댑터가, 재분할은 라이브러리를 모르는 `core/`
        가 맡는다.
        """
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
            pieces = resplit(chunk, size=size, overlap=self._overlap_for(size))
            if all(self._embedder.count_document_tokens(piece.text) <= limit for piece in pieces):
                logger.info(
                    "토큰 상한을 넘는 청크를 다시 쪼갰습니다",
                    extra={"resplit_size": size, "resplit_pieces": len(pieces)},
                )
                return pieces

        # 바닥까지 내려가도 못 맞춘 경우. 여기서 요청을 실패시키면 병적인 텍스트 한
        # 조각이 문서 전체의 수집을 막는다. 잘릴 수 있다는 사실을 로그로 드러내고
        # 진행한다 — 조용히 잘리는 것과 달리 이건 남는다.
        logger.warning(
            "재분할로도 토큰 상한을 맞추지 못했습니다 — 임베딩에서 잘릴 수 있습니다",
            extra={"resplit_size": size, "resplit_pieces": len(pieces)},
        )
        return pieces

    def _overlap_for(self, size: int) -> int:
        """줄어든 청크 크기에 맞춘 겹침. 겹침이 크기 이상이면 분할이 전진하지 않는다."""
        return max(1, min(self._chunk_overlap, size - 1))

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
