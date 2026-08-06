"""문서 도메인 — 값 객체와 식별자 유도 규칙.

벡터의 정체성은 `(document_id, revision, index_signature)` 셋이고, 셋을 나눈 이유는
각각이 다른 사건을 뜻하기 때문이다. 표준 라이브러리만 쓴다.
"""

import hashlib
import json
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

# 값이 바뀌면 같은 파일명이 다른 문서가 되어 기존 데이터와의 연결이 끊긴다.
DOCUMENT_NAMESPACE = uuid5(NAMESPACE_URL, "https://officeagent.example/documents")

# 업로드 파일명에 섞여 들어올 수 있는 경로 구분자. 클라이언트 OS 를 가정하지 않는다.
_PATH_SEPARATORS = ("/", "\\")


class DocumentFormat(StrEnum):
    """수집 대상이 될 수 있는 문서 포맷.

    여기 값이 있다고 수집되는 것은 아니다 — "포맷 → 파서"는 배선이라 레지스트리가 안다."""

    TXT = "txt"
    MD = "md"
    PDF = "pdf"

    @property
    def extension(self) -> str:
        return f".{self.value}"

    @classmethod
    def from_extension(cls, extension: str) -> "DocumentFormat | None":
        """확장자에 대응하는 포맷. 대응이 없으면 `None`.

        예외를 안 던지는 것은 "지원하지 않는다"는 판정에 파서 목록이 필요해서다."""
        return _FORMATS_BY_EXTENSION.get(extension.lower())


_FORMATS_BY_EXTENSION: dict[str, DocumentFormat] = {
    document_format.extension: document_format for document_format in DocumentFormat
}


def file_extension(filename: str) -> str:
    """파일명에서 소문자 확장자를 뽑는다. 없으면 빈 문자열.

    `.gitignore` 처럼 점으로 시작하는 이름은 확장자가 아니라 이름 전체다."""
    _, dot, suffix = normalize_filename(filename).rpartition(".")
    return f".{suffix}" if dot and _ else ""


class IndexStatus(StrEnum):
    """저장된 청크가 현재 색인 구성과 맞는가."""

    INDEXED = "indexed"
    #: 기록된 `index_signature`가 현재 구성과 달라 기동 시 청크가 제거된 상태.
    #: 원본 바이트를 보관하지 않으므로 재업로드로만 복구된다.
    STALE = "stale"


class IngestionStatus(StrEnum):
    """수집 요청 하나가 저장소에 무슨 일을 했는가.

    `REINDEXED` 를 `REPLACED` 와 뭉개면 `previous_revision` 이 현재 값과 같아진다."""

    CREATED = "created"
    REPLACED = "replaced"
    REINDEXED = "reindexed"
    UNCHANGED = "unchanged"

    @classmethod
    def of(cls, current: "Document | None", revision: str) -> "IngestionStatus":
        """이전 상태와 새 리비전으로 이번 요청의 결과를 분류한다.

        `UNCHANGED` 는 여기서 안 나온다 — 색인 경로 진입 전에 끝나는 판정이다."""
        if current is None:
            return cls.CREATED
        if current.revision != revision:
            return cls.REPLACED
        # 내용은 같은데 여기까지 왔다 = 색인 구성이 달라졌거나 `stale` 이다.
        return cls.REINDEXED


@dataclass(frozen=True)
class ChunkLocation:
    """청크가 원문의 어디에서 왔는가 — 출처 표기의 근거다.

    `page` 는 PDF 만 채우고, 없으면 저장 시 키 자체를 넣지 않는다."""

    char_start: int
    char_end: int
    page: int | None = None

    def __post_init__(self) -> None:
        if self.char_start < 0:
            raise ValueError("char_start 는 0 이상이어야 한다")
        if self.char_end <= self.char_start:
            raise ValueError("char_end 는 char_start 보다 커야 한다")
        if self.page is not None and self.page < 1:
            raise ValueError("page 는 1-base 다")


@dataclass(frozen=True)
class TextSegment:
    """파서가 돌려주는 추출 단위 하나.

    경계를 유지해야 청커가 "청크는 페이지를 넘지 않는다"를 지킬 수 있다."""

    text: str
    page: int | None = None

    def __post_init__(self) -> None:
        if self.page is not None and self.page < 1:
            raise ValueError("page 는 1-base 다")


@dataclass(frozen=True)
class ExtractedDocument:
    """파서 한 번의 결과 전체.

    `page_count` 를 따로 드는 이유는 스캔본 PDF 와 빈 문서를 가르기 위해서다."""

    segments: tuple[TextSegment, ...] = ()
    page_count: int | None = None

    def __post_init__(self) -> None:
        if self.page_count is not None and self.page_count < 0:
            raise ValueError("page_count 는 0 이상이어야 한다")

    @property
    def has_text(self) -> bool:
        return bool(self.segments)


@dataclass(frozen=True)
class TextChunk:
    """정체성이 아직 붙지 않은 청크 — 본문과 위치뿐.

    분할은 문서가 무엇인지 몰라도 되는 순수 연산이라 정체성을 나중에 붙인다."""

    text: str
    location: ChunkLocation


@dataclass(frozen=True)
class Chunk:
    """검색 단위 하나."""

    document_id: str
    revision: str
    index_signature: str
    chunk_index: int
    text: str
    location: ChunkLocation

    def __post_init__(self) -> None:
        if self.chunk_index < 0:
            raise ValueError("chunk_index 는 0 이상이어야 한다")
        if not self.text:
            raise ValueError("빈 청크는 저장하지 않는다")

    @property
    def id(self) -> str:
        """벡터 스토어에 쓰이는 안정적인 청크 식별자.

        서명이 들어가는 것은 없으면 새 청크가 이전 것을 덮어쓰기 때문이다."""
        return (
            f"{self.document_id}"
            f":{self.revision[:12]}"
            f":{self.index_signature[:8]}"
            f":{self.chunk_index:05d}"
        )


@dataclass(frozen=True)
class StoredIndexVersion:
    """저장소에 실제로 청크가 들어 있는 `(문서, 리비전, 서명)` 조합 하나.

    레지스트리가 "유효한 것"이라면 이것은 "남아 있는 것"이고, 차이가 기동 정리 대상이다."""

    document_id: str
    revision: str
    index_signature: str

    @property
    def key(self) -> str:
        """세 축을 한 값으로 — 저장소가 대상 집합을 조건 하나로 좁힐 수 있게 한다.

        축마다 조건을 만들면 필터의 크기가 문서 수에 비례해 매 질의마다 조립·평가된다."""
        return f"{self.document_id}:{self.revision}:{self.index_signature}"

    @classmethod
    def of(cls, source: "Document | Chunk") -> "StoredIndexVersion":
        """레코드나 청크가 가리키는 조합.

        손으로 옮기면 축을 빠뜨려도 타입이 멀쩡하다 — 리비전이 빠지면 지운 문장이 검색된다."""
        return cls(
            document_id=source.document_id,
            revision=source.revision,
            index_signature=source.index_signature,
        )


@dataclass(frozen=True)
class Document:
    """레지스트리가 보관하는 문서 한 건의 현재 상태.

    단일 답이 있어야 리비전 교체가 원자적이 된다."""

    document_id: str
    filename: str
    format: DocumentFormat
    revision: str
    index_signature: str
    chunk_count: int
    byte_size: int
    ingested_at: datetime
    index_status: IndexStatus = IndexStatus.INDEXED

    def __post_init__(self) -> None:
        if self.chunk_count < 0:
            raise ValueError("chunk_count 는 0 이상이어야 한다")
        if self.byte_size < 0:
            raise ValueError("byte_size 는 0 이상이어야 한다")

    # 수집과 검색이 각자 구현하면 한쪽만 고쳐졌을 때 각자 자기 기준으로는 옳은 채로
    # 어긋난다. 값을 통일해도 규칙이 여러 벌이면 같은 자리가 다시 열린다.

    def matches_index(self, index_signature: str) -> bool:
        """이 문서의 청크가 주어진 색인 구성으로 만들어졌는가.

        서명이 다르면 벡터가 다른 의미 공간에 있다."""
        return self.index_signature == index_signature

    def is_searchable_under(self, index_signature: str) -> bool:
        """지금 검색 대상인가 — 구성이 맞고 청크가 실제로 있어야 한다.

        `stale` 은 서명을 그대로 두므로 서명만 봐서는 걸러지지 않는다."""
        return self.matches_index(index_signature) and self.index_status is IndexStatus.INDEXED

    def is_up_to_date(self, *, revision: str, index_signature: str) -> bool:
        """이 내용을 이 구성으로 이미 색인해 두었는가 — 세 축을 모두 본다.

        `revision` 만 보면 구성을 바꾼 뒤 재업로드가 아무 일도 하지 않는다."""
        return self.revision == revision and self.is_searchable_under(index_signature)


def identify_chunks(
    chunks: Iterable[TextChunk],
    *,
    document_id: str,
    revision: str,
    index_signature: str,
) -> tuple[Chunk, ...]:
    """분할 결과에 정체성과 순번을 붙인다.

    순번이 0부터 빠짐없이여야 배치 경계의 누락·중복을 확인할 수 있다."""
    return tuple(
        Chunk(
            document_id=document_id,
            revision=revision,
            index_signature=index_signature,
            chunk_index=index,
            text=chunk.text,
            location=chunk.location,
        )
        for index, chunk in enumerate(chunks)
    )


def normalize_filename(filename: str) -> str:
    """경로 제거 → NFC → 소문자.

    macOS(NFD)와 리눅스(NFC)에서 같은 한글 파일명이 다른 문서가 되는 것을 막는다."""
    name = filename
    for separator in _PATH_SEPARATORS:
        name = name.rsplit(separator, 1)[-1]
    return unicodedata.normalize("NFC", name.strip()).casefold()


def derive_document_id(filename: str) -> str:
    """파일명에서 문서 식별자를 결정론적으로 유도한다.

    랜덤 UUID 는 파일명 인덱스를 요구하고, 그 인덱스가 또 하나의 진실 원천이 된다."""
    normalized = normalize_filename(filename)
    if not normalized:
        raise ValueError("파일명이 비어 있다")
    return str(uuid5(DOCUMENT_NAMESPACE, normalized))


def derive_revision(data: bytes) -> str:
    """원본 바이트에서 리비전을 유도한다.

    이것을 `document_id` 로 쓰면 "같은 문서가 달라졌다"를 표현할 수 없어 무효화가 깨진다."""
    return hashlib.sha256(data).hexdigest()


def derive_index_signature(
    *,
    embedder_signature: str,
    chunk_strategy: str,
    chunk_strategy_version: int,
    chunk_size: int,
    chunk_overlap: int,
    tokenizer_signature: str,
    pdf_extraction_signature: str,
) -> str:
    """색인 구성에서 서명을 유도한다 — 저장물을 바꾸는 값만 들어온다.

    성능 값과 검색 시점 설정을 넣으면 튜닝이 전면 재색인을 유발한다 (`ARCHITECTURE.md`)."""
    if chunk_strategy_version < 1:
        raise ValueError("chunk_strategy_version 은 1 이상이어야 한다")

    # 값 경계가 모호한 직렬화를 쓰면 서로 다른 구성이 같은 서명을 받아, 구 구성
    # 벡터가 남는다 — 서명이 막으려던 실패다.
    canonical = json.dumps(
        {
            "embedder_signature": embedder_signature,
            "chunk_strategy": chunk_strategy,
            "chunk_strategy_version": chunk_strategy_version,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            # 토큰화가 달라지면 어휘 색인의 내용이 달라진다. 서명을 따로 두지 않는 근거는
            # `document-ingestion` 델타의 「색인 서명은 저장 시점의 색인 구성에서」에 있다.
            "tokenizer_signature": tokenizer_signature,
            # PDF 추출 방식은 청크의 본문 자체를 바꾸는데 `revision` 은 바이트 해시라
            # 그대로다. 포맷별로 쪼개지 않는 근거도 같은 델타에 있다.
            "pdf_extraction_signature": pdf_extraction_signature,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
