"""문서 도메인 — 값 객체와 식별자 유도 규칙.

저장된 벡터의 완전한 정체성은 `(document_id, revision, index_signature)` 셋이다.

- `document_id` — 어떤 문서인가. 파일명에서 결정론적으로 유도한다.
- `revision`    — 내용이 무엇인가. 원본 바이트 해시.
- `index_signature` — 어떤 구성으로 색인했는가. 모델·청킹 구성 해시.

셋을 나눠 두는 이유는 각각이 다른 사건을 뜻하기 때문이다. 내용이 바뀌면 `revision`이,
색인 방식이 바뀌면 `index_signature`가 달라진다. 캐시 무효화는 이 셋을 신호로 삼는다.

`core/`는 표준 라이브러리만 쓴다 — pydantic 도 쓰지 않는다.
"""

import hashlib
import json
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

# 파일명 → document_id 유도에 쓰는 고정 네임스페이스.
#
# 값이 바뀌면 같은 파일명이 다른 문서가 되어 기존 데이터와의 연결이 끊긴다. 절대 바꾸지 않는다.
DOCUMENT_NAMESPACE = uuid5(NAMESPACE_URL, "https://officeagent.example/documents")

# 업로드 파일명에 섞여 들어올 수 있는 경로 구분자. 클라이언트 OS 를 가정하지 않는다.
_PATH_SEPARATORS = ("/", "\\")


class DocumentFormat(StrEnum):
    """수집을 지원하는 문서 포맷."""

    TXT = "txt"
    MD = "md"
    PDF = "pdf"


class IndexStatus(StrEnum):
    """저장된 청크가 현재 색인 구성과 맞는가."""

    INDEXED = "indexed"
    #: 기록된 `index_signature`가 현재 구성과 달라 기동 시 청크가 제거된 상태.
    #: 원본 바이트를 보관하지 않으므로 재업로드로만 복구된다.
    STALE = "stale"


class IngestionStatus(StrEnum):
    """수집 요청 하나가 저장소에 무슨 일을 했는가.

    `REINDEXED`를 `REPLACED`와 뭉개지 않는다. 재색인은 `revision`이 그대로인 채
    일어나므로, `REPLACED`로 응답하면 `previous_revision`이 현재 `revision`과 같아져
    응답이 자기모순이 된다.
    """

    CREATED = "created"
    REPLACED = "replaced"
    REINDEXED = "reindexed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class ChunkLocation:
    """청크가 원문의 어디에서 왔는가.

    이 정보가 답변의 출처 표기 근거가 된다. 위치 없이 저장하면 검색은 되지만
    "어느 문서의 어느 부분"을 말할 수 없다.

    `page`는 PDF 만 채운다. 값이 없으면 저장 시 키 자체를 넣지 않는다 — 임베디드
    벡터 스토어가 널 메타데이터를 허용하지 않고, 센티널 값을 쓰면 소비자가 그 규약을
    알아야 하기 때문이다.
    """

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

    파서마다 자연스러운 단위가 다르다 — PDF 는 쪽, 텍스트·마크다운은 문서 전체.
    문자열 하나로 합쳐 돌려주면 페이지 정보가 그 시점에 소실되고 되살릴 방법이 없다.
    세그먼트 경계를 유지해야 청커가 "청크는 페이지를 넘지 않는다"를 지킬 수 있다.

    값 객체를 `core/`에 두고 `DocumentParser` 프로토콜만 `adapters/`에 두는 것은
    import 방향 때문이다. 청킹이 이 타입을 소비하는데, `core/`가 `adapters/`를
    import 하면 계층이 역전된다.

    `char_start`·`char_end` 오프셋은 **세그먼트 텍스트 기준**이다. txt·md 는 문서
    전체가 세그먼트 하나라 문서 오프셋과 같고, PDF 는 쪽 안의 오프셋이다 — PDF 에서
    문서 전역 오프셋은 실재하지 않는 연결 문자열을 가정해야 나오는 값이라 쓰지 않는다.
    PDF 의 주소는 `(page, char_start, char_end)`다.
    """

    text: str
    page: int | None = None

    def __post_init__(self) -> None:
        if self.page is not None and self.page < 1:
            raise ValueError("page 는 1-base 다")


@dataclass(frozen=True)
class TextChunk:
    """정체성이 아직 붙지 않은 청크 — 본문과 위치뿐.

    분할은 문서가 무엇인지 몰라도 되는 순수 연산이다. `document_id`·`revision`·
    `index_signature`를 분할 함수에 넘기면 분할이 정체성에 의존하는 것처럼 보이게 된다.
    정체성은 `identify_chunks`가 나중에 붙인다.
    """

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

        **서명이 id 에 들어가는 이유는 재색인 경로 때문이다.** 재색인은 `revision`이
        그대로인 채 일어난다. id 가 `document_id:revision:index` 뿐이면 새 청크가
        이전 청크의 id 를 그대로 덮어써, "새로 쓰고 → 커밋 → 지우기" 순서가 성립하지
        않는다. 쓰는 도중에 이미 이전 벡터가 사라지고, 실패해도 되돌릴 원본이 없다.
        """
        return (
            f"{self.document_id}"
            f":{self.revision[:12]}"
            f":{self.index_signature[:8]}"
            f":{self.chunk_index:05d}"
        )


@dataclass(frozen=True)
class Document:
    """레지스트리가 보관하는 문서 한 건의 현재 상태.

    "지금 유효한 리비전이 무엇인가"에 단일 답이 있어야 리비전 교체가 원자적이 된다.
    그 단일 답이 이 레코드다.
    """

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


def identify_chunks(
    chunks: Iterable[TextChunk],
    *,
    document_id: str,
    revision: str,
    index_signature: str,
) -> tuple[Chunk, ...]:
    """분할 결과에 정체성과 순번을 붙인다.

    `chunk_index`는 문서 안에서 0부터 빠짐없이 하나씩이다 — 배치 경계에서 청크가
    누락되거나 중복 저장되지 않았음을 이 순번으로 확인할 수 있어야 한다.
    """
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
    """`document_id` 유도 전에 파일명을 정규화한다.

    경로 제거 → 유니코드 NFC → 소문자. macOS(NFD)와 리눅스(NFC)에서 같은 한글
    파일명이 서로 다른 문서가 되는 것을 막는다.
    """
    name = filename
    for separator in _PATH_SEPARATORS:
        name = name.rsplit(separator, 1)[-1]
    return unicodedata.normalize("NFC", name.strip()).casefold()


def derive_document_id(filename: str) -> str:
    """파일명에서 문서 식별자를 결정론적으로 유도한다.

    랜덤 UUID 를 쓰지 않는 이유: "같은 문서인가"를 판정하려면 파일명 인덱스를 따로
    조회해야 하고, 그 인덱스가 곧 또 하나의 진실 원천이 된다. 결정론적 유도는 조회
    없이 같은 답을 준다.
    """
    normalized = normalize_filename(filename)
    if not normalized:
        raise ValueError("파일명이 비어 있다")
    return str(uuid5(DOCUMENT_NAMESPACE, normalized))


def derive_revision(data: bytes) -> str:
    """원본 바이트에서 리비전을 유도한다.

    내용 해시를 `document_id`로 쓰지 않는 이유: 내용이 바뀌면 다른 문서가 되어버려
    "문서를 갱신했다"를 표현할 수 없고, 그러면 캐시 무효화가 성립하지 않는다.
    무효화는 *같은 문서가 달라졌다*는 사건이기 때문이다.
    """
    return hashlib.sha256(data).hexdigest()


def derive_index_signature(
    *,
    embedder_signature: str,
    chunk_strategy: str,
    chunk_strategy_version: int,
    chunk_size: int,
    chunk_overlap: int,
) -> str:
    """색인 구성에서 서명을 유도한다.

    **여기 들어오는 것**은 결과 벡터를 바꾸는 값뿐이다. 임베더 정체성(모델 식별자·
    차원·정규화·역할 접두사 규약)은 어댑터가 `signature` 문자열 하나로 요약해 넘긴다 —
    접두사 규약을 어댑터 안에 가둔 채 서명 재료를 꺼내는 유일한 통로다.

    **들어오지 않는 것**도 계약이다. 문서 내용·파일명은 `revision`·`document_id`의
    몫이고, 임베딩 배치 크기·업로드 상한·수집 동시성 상한은 결과 벡터를 한 비트도
    바꾸지 않는다. 넣으면 성능 튜닝이 전면 재색인을 유발한다.

    서명 자체를 설정 항목으로 두지 않는 이유: 손으로 적을 수 있으면 실제 구성과
    어긋난 값을 넣을 수 있고, 그 순간 서명이 보증하려던 것이 사라진다.
    """
    if chunk_strategy_version < 1:
        raise ValueError("chunk_strategy_version 은 1 이상이어야 한다")

    # 재료를 `key=value\n` 로 이어 붙이면 값 안의 개행이 구분자와 구별되지 않는다.
    # `embedder_signature="e\nstrategy=s2", chunk_strategy="s"` 와
    # `embedder_signature="e", chunk_strategy="s2\nstrategy=s"` 가 같은 문자열이 되어
    # **서로 다른 구성이 같은 서명**을 받는다. 서명이 같으면 재색인이 일어나지 않으므로
    # 구 구성 벡터가 그대로 남는다 — 서명이 막으려던 바로 그 실패다.
    #
    # `embedder_signature` 는 어댑터가 만드는 자유 문자열이라 앞으로도 개행이 섞이지
    # 않는다고 보장할 수 없다. 값 경계가 모호하지 않은 직렬화를 쓴다.
    canonical = json.dumps(
        {
            "embedder_signature": embedder_signature,
            "chunk_strategy": chunk_strategy,
            "chunk_strategy_version": chunk_strategy_version,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
