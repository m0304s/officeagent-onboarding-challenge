"""문서 수집 오케스트레이션.

> **현재 붙어 있는 구간은 `파서 선택 → 파싱 → 청킹` 까지다.** 임베딩·벡터 저장·레지스트리
> 커밋은 어댑터가 아직 없어 연결되지 않았다. 그 단계들이 들어오면 이 파일에
> `ingest()` 가 추가되어 아래 `extract_chunks()` 를 첫 구간으로 호출한다 — 지금 있는
> `extract_chunks()` 는 그때 이름이 바뀌지 않는 경계다.

계층 규칙: 이 모듈은 어댑터의 **프로토콜**만 알고 구현체를 모른다. 파서 레지스트리를
주입받으므로 PDF 파서를 통째로 갈아끼워도 이 파일은 바뀌지 않는다.
"""

import asyncio
import logging
from dataclasses import dataclass

from app.adapters.parsers import ParserRegistry
from app.core.chunking import ChunkStrategy, get_splitter
from app.core.documents import (
    DocumentFormat,
    TextChunk,
    derive_document_id,
    derive_revision,
)
from app.core.exceptions import EmptyDocument, NoExtractableText

logger = logging.getLogger(__name__)


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


class IngestionService:
    """업로드된 바이트를 검색 단위로 만든다."""

    def __init__(
        self,
        parsers: ParserRegistry,
        *,
        chunk_strategy: ChunkStrategy,
        chunk_size: int,
        chunk_overlap: int,
    ) -> None:
        self._parsers = parsers
        # 전략은 기동 시점에 함수로 해석해 둔다. 업로드마다 조회하면 등록 누락이
        # 첫 업로드에서야 드러난다.
        self._split = get_splitter(chunk_strategy)
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    async def extract_chunks(self, filename: str, data: bytes) -> ExtractionResult:
        """파일명과 바이트에서 청크까지 만든다.

        미지원 포맷은 `UnsupportedDocumentFormat`, 내용이 없으면 `EmptyDocument`,
        쪽은 있는데 텍스트 레이어가 없으면 `NoExtractableText` 로 끝난다.
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

    @staticmethod
    def _no_text_error(filename: str, page_count: int | None) -> Exception:
        """"쪽은 있는데 텍스트가 없다"와 "내용 자체가 없다"를 가른다.

        뭉개면 클라이언트는 OCR 이 필요한 것인지 파일이 잘못된 것인지 구분할 수 없다.
        """
        if page_count:
            logger.info("텍스트 레이어가 없는 PDF: %s (%d쪽)", filename, page_count)
            return NoExtractableText(
                "문서에 텍스트 레이어가 없습니다. "
                "이 서비스는 이미지 인식(OCR)을 수행하지 않습니다",
                page_count=page_count,
            )
        return EmptyDocument("문서에 내용이 없습니다")
