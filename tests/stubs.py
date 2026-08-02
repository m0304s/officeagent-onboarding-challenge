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

    def count_tokens(self, text: str) -> int:
        """문자 수에 비례하는 결정론적 토큰 수.

        `chars_per_token`을 크게 잡으면 토큰 가드가 걸리는 상황을 실제 토크나이저
        없이 만들 수 있다.
        """
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
    """

    def __init__(self, *, fail_add_after: int | None = None, fail_delete: bool = False) -> None:
        self.records: dict[str, dict] = {}
        self.fail_add_after = fail_add_after
        self.fail_delete = fail_delete
        self.add_calls = 0
        #: 배치 크기를 호출 순서대로 기록한다. 배치 경계를 확인하는 데 쓴다.
        self.batch_sizes: list[int] = []

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
