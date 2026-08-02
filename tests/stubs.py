"""테스트용 어댑터 대역.

어댑터가 전부 프로토콜 뒤에 있으므로 대역을 주입해 의존성 상태를 결정론적으로 만들 수
있다. 실제 컨테이너를 죽여가며 상태를 만들면 느리고 불안정하며, 무엇보다 외부 서비스
없이 스위트가 돌아야 한다.
"""

import asyncio
import hashlib
import math
import time
from collections.abc import Sequence

from app.core.documents import (
    Chunk,
    Document,
    DocumentFormat,
    ExtractedDocument,
    StoredIndexVersion,
    TextSegment,
)
from app.core.exceptions import StorageUnavailable
from app.core.models import ProbeResult, Status
from app.core.retrieval import ScoredChunk


class StubProbe:
    """지정한 결과를 그대로 돌려주는 프로브.

    `delay`를 주면 무응답 상황을, `raises`를 주면 프로브 자체가 터지는 상황을 만든다.
    """

    def __init__(
        self,
        name: str,
        status: Status = Status.OK,
        detail: str | None = None,
        delay: float = 0.0,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self._status = status
        self._detail = detail
        self._delay = delay
        self._raises = raises

    async def check(self) -> ProbeResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return ProbeResult(name=self.name, status=self._status, detail=self._detail)


class FakeEmbedder:
    """텍스트 해시로 결정론적 벡터를 만드는 임베더.

    수집 테스트가 검증하는 것은 **저장 결과의 모양**이다 — 청크 수, 위치 정보, 리비전
    교체, 오류 경로. 벡터의 *의미*는 하나도 검증하지 않으므로 실제 모델을 돌려도 단언이
    강해지지 않는다. 반면 비용은 크다. 가중치가 캐시되지 않은 환경에서 `pytest` 한 줄이
    수백 MB 다운로드에 묶인다.

    `signature`·`dimension`을 생성 시점에 지정할 수 있어야 하는 이유는 따로 있다.
    "모델 정체성이 바뀌면 색인 서명이 바뀌고 재색인이 강제된다"를 **실제 모델 없이**
    재현하는 유일한 수단이다. 실물 모델로 이 상황을 만들려면 서로 다른 모델 둘을
    받아야 한다.

    `delay`는 인코딩이 오래 걸리는 상황을 만든다 — 배치 사이에 이벤트 루프로 양보하지
    않으면 헬스 응답이 그만큼 늦어지는지 보는 데 쓴다.
    """

    def __init__(
        self,
        *,
        dimension: int = 8,
        signature: str | None = None,
        max_input_tokens: int = 512,
        delay: float = 0.0,
        chars_per_token: int = 2,
        warm_up_error: Exception | None = None,
    ) -> None:
        self.dimension = dimension
        self.max_input_tokens = max_input_tokens
        self.signature = signature or f"fake-embedder/{dimension}/l2norm/none-v1"
        self._delay = delay
        self._chars_per_token = chars_per_token
        #: 인코딩 호출을 배치 단위로 기록한다. 배치 경계와 중복 인코딩 여부를 본다.
        self.batches: list[list[str]] = []
        #: 선로딩 호출 횟수. 배선이 정말로 `warm_up` 을 부르는지 확인할 수단이다.
        self.warm_ups = 0
        #: 선로딩 실패를 주입한다 — "실패해도 기동은 계속된다"를 만드는 유일한 방법.
        self.warm_up_error = warm_up_error

    async def warm_up(self) -> None:
        """올릴 것이 없으므로 하는 일도 없다 — 호출 사실만 남긴다.

        no-op 이어도 프로토콜에 있어야 한다. 배선이 `isinstance` 로 구체 어댑터를
        확인해 부르면 계층 규약이 깨지므로, 모든 임베더가 이 요청을 받을 수 있어야 한다.
        """
        self.warm_ups += 1
        if self.warm_up_error is not None:
            raise self.warm_up_error

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        if self._delay:
            await asyncio.sleep(self._delay)
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._vector(text)

    def count_document_tokens(self, text: str) -> int:
        """문자 수에 비례하는 결정론적 토큰 수.

        `chars_per_token`을 크게 잡으면 토큰 가드가 걸리는 상황을 실제 토크나이저
        없이 만들 수 있다.
        """
        return self._count(text)

    def count_query_tokens(self, text: str) -> int:
        """문서 경로와 **같은 수**를 돌려준다.

        페이크에는 역할 접두사가 없으므로 흉내 낼 차이도 없다. 두 경로의 계산이
        실제로 갈리는지는 접두사를 아는 실물 어댑터에서 확인한다 — 여기서 인위적인
        차이를 만들면 검증 대상이 페이크가 되고, 정작 확인하려던 성질은 여전히
        실물에서 미확인으로 남는다. 벡터를 대칭으로 남긴 이유와 같다.
        """
        return self._count(text)

    def _count(self, text: str) -> int:
        return max(1, math.ceil(len(text) / self._chars_per_token))

    def _vector(self, text: str) -> list[float]:
        values: list[float] = []
        block = 0
        while len(values) < self.dimension:
            digest = hashlib.sha256(f"{block}:{text}".encode()).digest()
            values.extend(byte / 255.0 - 0.5 for byte in digest)
            block += 1
        values = values[: self.dimension]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]


class StubParser:
    """지정한 세그먼트를 돌려주는 파서.

    `delay`를 주면 텍스트 추출이 오래 걸리는 문서를 만든다 — **블로킹 지연**이라
    누군가 파서를 이벤트 루프에서 직접 호출하면 그 사실이 드러난다.
    """

    def __init__(
        self,
        *,
        formats: frozenset[DocumentFormat] = frozenset({DocumentFormat.TXT}),
        text: str = "본문입니다.",
        page_count: int | None = None,
        delay: float = 0.0,
        raises: Exception | None = None,
    ) -> None:
        self.formats = formats
        self._text = text
        self._page_count = page_count
        self._delay = delay
        self._raises = raises
        self.calls = 0

    def parse(self, data: bytes) -> ExtractedDocument:
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)  # 블로킹. 스레드풀이 아니면 루프가 멈춘다
        if self._raises is not None:
            raise self._raises
        return ExtractedDocument(
            segments=(TextSegment(text=self._text),), page_count=self._page_count
        )


class StubVectorStore:
    """인메모리 벡터 스토어.

    실패를 **주입할 수 있다**는 점이 존재 이유다. 리비전 교체의 되돌리기와 "되돌리기
    까지 실패한" 경로는 저장이 실제로 실패해야 검증되는데, 실물 스토어로 그 상태를
    만들려면 디스크를 망가뜨려야 한다.

    - `fail_add_after`: 이 횟수만큼 성공한 뒤 `add_chunks` 가 실패한다. `0`이면 처음부터
      실패하고, `1`이면 배치 하나를 쓴 뒤 실패해 **부분 기록** 상태가 된다.
    - `fail_delete`: `delete_document` 가 항상 실패한다. 되돌리기까지 실패하는 경로다.
    - `fail_query`: `query` 가 항상 실패한다. 저장소 장애가 빈 결과로 위장되지 않는지 본다.
    - `query_delay`: 질의가 오래 걸리는 상황. **블로킹 지연을 스레드풀로 내보낸다** —
      실물 어댑터가 그러하듯 오프로드 책임이 구현체에 있으므로, 대역도 같은 자리에서
      같은 방식으로 처리해야 그 위 계층(서비스·라우터)의 블로킹만 남아 드러난다.

    **질의는 완전 탐색 코사인이다.** 근사 검색(HNSW)의 흔들림이 없어야 검색 품질
    테스트가 임베딩의 성질만 재게 된다. 실물 Chroma 와의 계약(필터 표현식·메타데이터
    왕복)은 `test_vector_store.py` 가 따로 덮는다.
    """

    def __init__(
        self,
        *,
        fail_add_after: int | None = None,
        fail_delete: bool = False,
        fail_query: bool = False,
        query_delay: float = 0.0,
    ) -> None:
        self.records: dict[str, dict] = {}
        self.fail_add_after = fail_add_after
        self.fail_delete = fail_delete
        self.fail_query = fail_query
        self.add_calls = 0
        self._query_delay = query_delay
        #: 배치 크기를 호출 순서대로 기록한다. 배치 경계를 확인하는 데 쓴다.
        self.batch_sizes: list[int] = []
        #: 질의 호출을 `(top_k, 대상 삼중항)` 으로 기록한다. "거부된 요청은 저장소에
        #: 닿지 않는다"와 "대상이 없으면 저장소를 건드리지 않는다"는 호출 **부재**로만
        #: 확인되므로, 기록이 없으면 그 단언을 쓸 수 없다.
        self.queries: list[tuple[int, tuple[StoredIndexVersion, ...]]] = []

    async def add_chunks(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        *,
        filename: str,
        document_format: DocumentFormat,
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("청크 수와 벡터 수가 다르다")
        if self.fail_add_after is not None and self.add_calls >= self.fail_add_after:
            self.add_calls += 1
            raise StorageUnavailable("주입된 쓰기 실패")
        self.add_calls += 1
        self.batch_sizes.append(len(chunks))
        for chunk, vector in zip(chunks, embeddings, strict=True):
            self.records[chunk.id] = {
                "chunk": chunk,
                "embedding": list(vector),
                "filename": filename,
                "format": document_format,
            }

    async def delete_document(
        self,
        document_id: str,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int:
        if self.fail_delete:
            raise StorageUnavailable("주입된 삭제 실패")
        matched = [
            chunk_id
            for chunk_id, record in self.records.items()
            if _matches(record["chunk"], document_id, revision, index_signature)
        ]
        for chunk_id in matched:
            del self.records[chunk_id]
        return len(matched)

    async def count_chunks(
        self,
        document_id: str | None = None,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int:
        return len(
            [
                record
                for record in self.records.values()
                if _matches(record["chunk"], document_id, revision, index_signature)
            ]
        )

    async def list_stored_versions(self) -> list[StoredIndexVersion]:
        versions = {
            (
                record["chunk"].document_id,
                record["chunk"].revision,
                record["chunk"].index_signature,
            )
            for record in self.records.values()
        }
        return [StoredIndexVersion(*version) for version in sorted(versions)]

    async def query(
        self,
        embedding: Sequence[float],
        *,
        top_k: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[ScoredChunk]:
        self.queries.append((top_k, tuple(versions)))
        if self._query_delay:
            await asyncio.to_thread(time.sleep, self._query_delay)
        if self.fail_query:
            raise StorageUnavailable("주입된 질의 실패")
        # 빈 목록은 **대상 없음**이다. 실물 어댑터와 같은 판정이라야 대역으로 통과한
        # 테스트가 실물에서 뒤집히지 않는다.
        if not versions:
            return []
        targets = {
            (version.document_id, version.revision, version.index_signature) for version in versions
        }
        scored = [
            (_cosine(embedding, record["embedding"]), record)
            for record in self.records.values()
            if (
                record["chunk"].document_id,
                record["chunk"].revision,
                record["chunk"].index_signature,
            )
            in targets
        ]
        # 점수가 같은 청크의 순서까지 고정한다 — "같은 질의는 같은 결과"가 dict 순서에
        # 기대면 저장 순서가 바뀌는 순간 거짓이 된다.
        scored.sort(key=lambda item: (-item[0], item[1]["chunk"].id))
        return [
            ScoredChunk(
                document_id=record["chunk"].document_id,
                revision=record["chunk"].revision,
                index_signature=record["chunk"].index_signature,
                chunk_index=record["chunk"].chunk_index,
                text=record["chunk"].text,
                location=record["chunk"].location,
                filename=record["filename"],
                format=record["format"],
                score=score,
            )
            for score, record in scored[:top_k]
        ]

    # ── 테스트가 들여다보는 창 ──────────────────────────────────────────

    def embeddings_of(self, document_id: str) -> list[list[float]]:
        """청크 순번 순서로 정렬한 벡터. 청크마다 벡터가 실제로 붙었는지 보는 데 쓴다."""
        return [
            record["embedding"]
            for record in sorted(
                (r for r in self.records.values() if r["chunk"].document_id == document_id),
                key=lambda record: record["chunk"].chunk_index,
            )
        ]

    def chunks_of(self, document_id: str) -> list[Chunk]:
        """저장 순서가 아니라 청크 순번으로 정렬해 돌려준다."""
        return sorted(
            (
                record["chunk"]
                for record in self.records.values()
                if record["chunk"].document_id == document_id
            ),
            key=lambda chunk: chunk.chunk_index,
        )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """코사인 유사도를 `[0, 1]` 로 잘라 돌려준다.

    실물 어댑터가 거리를 유사도로 바꾸며 클램프하는 것과 같은 정의역이다 — 대역이
    범위를 벗어난 점수를 내면 `ScoredChunk` 가 거절해 테스트가 대역 탓으로 깨진다.
    """
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    if not norm:
        return 0.0
    return min(1.0, max(0.0, dot / norm))


def _matches(
    chunk: Chunk,
    document_id: str | None,
    revision: str | None,
    index_signature: str | None,
) -> bool:
    return (
        (document_id is None or chunk.document_id == document_id)
        and (revision is None or chunk.revision == revision)
        and (index_signature is None or chunk.index_signature == index_signature)
    )


class StubDocumentRegistry:
    """인메모리 문서 레지스트리.

    SQLite 구현의 성질(트랜잭션·영속성·스키마 생성)은 `test_registry.py` 가 실물로
    덮는다. 서비스 테스트가 이 대역을 쓰는 이유는 둘이다 — 임시 파일 없이 돌고,
    **커밋 실패를 주입**할 수 있다. 교체 순서에서 커밋은 "확정되는 순간"이라, 그 지점의
    실패는 저장 실패와 다른 경로다.
    """

    def __init__(self, *, fail_commit: bool = False) -> None:
        self.documents: dict[str, Document] = {}
        self.fail_commit = fail_commit
        self.commits = 0

    async def get(self, document_id: str) -> Document | None:
        return self.documents.get(document_id)

    async def list_all(self) -> list[Document]:
        return list(self.documents.values())

    async def commit(self, document: Document) -> None:
        if self.fail_commit:
            raise StorageUnavailable("주입된 커밋 실패")
        self.commits += 1
        self.documents[document.document_id] = document

    async def delete(self, document_id: str) -> Document | None:
        return self.documents.pop(document_id, None)
