"""테스트용 어댑터 대역.

어댑터가 전부 프로토콜 뒤에 있어 대역 주입으로 의존성 상태를 결정론적으로 만든다. 실제
컨테이너를 죽여 상태를 만들면 느리고 불안정하며, 외부 서비스 없이 도는 요구도 깨진다.
"""

import asyncio
import hashlib
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from app.core.documents import (
    Chunk,
    Document,
    DocumentFormat,
    ExtractedDocument,
    StoredIndexVersion,
    TextSegment,
)
from app.core.exceptions import StorageUnavailable
from app.core.lexical import DEFAULT_TOKENIZER
from app.core.models import ProbeResult, Status
from app.core.retrieval import RetrievedChunk


class StubProbe:
    """지정한 결과를 그대로 돌려주는 프로브.

    `delay`를 주면 무응답 상황을, `raises`를 주면 프로브 자체가 터지는 상황을 만든다."""

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

    주입 손잡이가 각각 무엇을 재현하는지는 `tests/README.md` 에 있다."""

    def __init__(
        self,
        *,
        dimension: int = 8,
        signature: str | None = None,
        max_input_tokens: int = 512,
        delay: float = 0.0,
        count_delay: float = 0.0,
        chars_per_token: int = 2,
        warm_up_error: Exception | None = None,
    ) -> None:
        self.dimension = dimension
        self.max_input_tokens = max_input_tokens
        self.signature = signature or f"fake-embedder/{dimension}/l2norm/none-v1"
        self._delay = delay
        self._count_delay = count_delay
        self._chars_per_token = chars_per_token
        #: 인코딩 호출을 배치 단위로 기록한다. 배치 경계와 중복 인코딩 여부를 본다.
        self.batches: list[list[str]] = []
        #: 질의 경로 호출 기록. 경로를 바꿔 써도 결과 형식은 멀쩡해 이 기록으로만 검출된다.
        self.queries: list[str] = []
        #: 선로딩 호출 횟수. 배선이 정말로 `warm_up` 을 부르는지 확인할 수단이다.
        self.warm_ups = 0
        #: 선로딩 실패를 주입한다 — "실패해도 기동은 계속된다"를 만드는 유일한 방법.
        self.warm_up_error = warm_up_error

    async def warm_up(self) -> None:
        """올릴 것이 없으므로 하는 일도 없다 — 호출 사실만 남긴다.

        배선이 `isinstance` 로 갈라 부르면 계층 규약이 깨지므로 프로토콜에는 있어야 한다."""
        self.warm_ups += 1
        if self.warm_up_error is not None:
            raise self.warm_up_error

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        if self._delay:
            await asyncio.sleep(self._delay)
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        """문서 경로와 같은 벡터를 낸다 — 버그가 아니라 결정이다.

        비대칭으로 바꾸면 순위 단언이 기대는 성질이 사라진다 (`tests/README.md`)."""
        self.queries.append(text)
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._vector(text)

    def count_document_tokens(self, text: str) -> int:
        """문자 수에 비례하는 결정론적 토큰 수.

        `chars_per_token` 을 크게 잡으면 토큰 가드가 걸리는 상황을 실물 없이 만들 수 있다."""
        return self._count(text)

    def count_query_tokens(self, text: str) -> int:
        """문서 경로와 같은 수를 돌려준다 — 페이크에는 역할 접두사가 없다.

        인위적인 차이를 만들면 검증 대상이 페이크가 된다 (`tests/README.md`)."""
        return self._count(text)

    def _count(self, text: str) -> int:
        if self._count_delay:
            time.sleep(self._count_delay)  # 블로킹. 스레드풀이 아니면 루프가 멈춘다
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


class FakeReranker:
    """주입한 규칙으로 후보를 채점하는 리랭커.

    기본 규칙이 토큰 겹침인 이유는 `tests/README.md` 에 있다."""

    def __init__(
        self,
        *,
        name: str = "fake-reranker",
        signature: str | None = None,
        scorer: Callable[[str, str], float] | None = None,
        delay: float = 0.0,
        error: Exception | None = None,
        warm_up_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.signature = signature or f"{name}/none-v1"
        self._scorer = scorer or _token_overlap
        self._delay = delay
        #: 채점 실패를 주입한다 — 축소 경로를 만드는 유일한 방법이다.
        self.error = error
        self.warm_up_error = warm_up_error
        #: 호출 기록. 리랭킹이 요청당 한 번인지, 무엇을 넘겼는지를 본다.
        self.calls: list[tuple[str, list[str]]] = []
        self.warm_ups = 0

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        self.calls.append((query, list(documents)))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self.error is not None:
            raise self.error
        return [self._scorer(query, document) for document in documents]

    async def warm_up(self) -> None:
        self.warm_ups += 1
        if self.warm_up_error is not None:
            raise self.warm_up_error


def _token_overlap(query: str, document: str) -> float:
    """질의와 후보가 공유하는 토큰 수. 결정론적이고 순서를 실제로 흔든다."""
    shared = set(DEFAULT_TOKENIZER.tokenize(query)) & set(DEFAULT_TOKENIZER.tokenize(document))
    return float(len(shared))


class SynonymEmbedder(FakeEmbedder):
    """지정한 질의들을 한 벡터로 묶는 임베더.

    해시 페이크로는 만들 수 없는 쌍이라 묶임 자체를 주입한다 (`tests/README.md`)."""

    def __init__(self, synonyms: dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._synonyms = synonyms

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(self._synonyms.get(text, text))


class StubParser:
    """지정한 세그먼트를 돌려주는 파서.

    `delay` 가 블로킹이라 파서를 루프에서 직접 호출하면 그 사실이 드러난다."""

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
    """인메모리 벡터 스토어. 실패를 주입할 수 있다는 점이 존재 이유다.

    손잡이 넷과 질의가 완전 탐색인 이유는 `tests/README.md` 에 있다."""

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
        #: 질의 호출 기록. "거부된 요청은 저장소에 닿지 않는다"가 호출 부재로만 확인되어,
        #: 이 기록이 없으면 그 단언을 쓸 수 없다.
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
    ) -> list[RetrievedChunk]:
        self.queries.append((top_k, tuple(versions)))
        if self._query_delay:
            await asyncio.to_thread(time.sleep, self._query_delay)
        if self.fail_query:
            raise StorageUnavailable("주입된 질의 실패")
        # 빈 목록은 대상 없음이다. 실물 어댑터와 같은 판정이라야 대역으로 통과한
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
            RetrievedChunk(
                document_id=record["chunk"].document_id,
                revision=record["chunk"].revision,
                index_signature=record["chunk"].index_signature,
                chunk_index=record["chunk"].chunk_index,
                text=record["chunk"].text,
                location=record["chunk"].location,
                filename=record["filename"],
                format=record["format"],
                native_score=score,
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

    실물 어댑터와 같은 정의역이라야 밀집 하한을 재는 테스트가 실물에서 뒤집히지 않는다."""
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


class StubLexicalIndex:
    """인메모리 어휘 색인. `StubVectorStore` 와 같은 손잡이를 갖는 것이 요건이다.

    한쪽에만 실패를 주입할 수 있어야 "양쪽에 청크가 0개"가 만들어진다 (`tests/README.md`)."""

    def __init__(
        self,
        *,
        fail_add_after: int | None = None,
        fail_delete: bool = False,
        fail_search: bool = False,
    ) -> None:
        self.records: dict[str, dict] = {}
        self.fail_add_after = fail_add_after
        self.fail_delete = fail_delete
        self.fail_search = fail_search
        self.add_calls = 0
        #: 질의 호출 기록. "대상이 없으면 색인을 건드리지 않는다"는 호출 부재로만 확인된다.
        self.searches: list[tuple[str, int, tuple[StoredIndexVersion, ...]]] = []

    async def add_chunks(
        self,
        chunks: Sequence[Chunk],
        *,
        filename: str,
        document_format: DocumentFormat,
    ) -> None:
        if self.fail_add_after is not None and self.add_calls >= self.fail_add_after:
            self.add_calls += 1
            raise StorageUnavailable("주입된 어휘 색인 쓰기 실패")
        self.add_calls += 1
        for chunk in chunks:
            self.records[chunk.id] = {
                "chunk": chunk,
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
            raise StorageUnavailable("주입된 어휘 색인 삭제 실패")
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

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[RetrievedChunk]:
        self.searches.append((query, top_k, tuple(versions)))
        if self.fail_search:
            raise StorageUnavailable("주입된 어휘 검색 실패")
        # 실물 어댑터와 같은 판정이라야 대역으로 통과한 테스트가 실물에서 뒤집히지 않는다.
        if not versions:
            return []
        wanted = set(DEFAULT_TOKENIZER.tokenize(query))
        targets = {
            (version.document_id, version.revision, version.index_signature) for version in versions
        }
        scored = []
        for record in self.records.values():
            chunk = record["chunk"]
            if (chunk.document_id, chunk.revision, chunk.index_signature) not in targets:
                continue
            overlap = len(wanted & set(DEFAULT_TOKENIZER.tokenize(chunk.text)))
            if overlap:
                scored.append((float(overlap), record))
        scored.sort(key=lambda item: (-item[0], item[1]["chunk"].id))
        return [
            RetrievedChunk(
                document_id=record["chunk"].document_id,
                revision=record["chunk"].revision,
                index_signature=record["chunk"].index_signature,
                chunk_index=record["chunk"].chunk_index,
                text=record["chunk"].text,
                location=record["chunk"].location,
                filename=record["filename"],
                format=record["format"],
                native_score=score,
            )
            for score, record in scored[:top_k]
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


@dataclass(frozen=True)
class GenerationTurn:
    """생성 시도 한 번이 어떻게 흘러가는지 적은 대본.

    세 손잡이와 매달리는 시도를 표현하지 않는 이유는 `tests/README.md` 에 있다."""

    chunks: tuple[str, ...] = ()
    raises: Exception | None = None
    delay: float = 0.0


@dataclass
class ScriptedGenerator:
    """대본대로 조각을 흘리고 호출 횟수를 세는 페이크 생성기.

    호출의 유무와 수는 응답에 드러나지 않아 카운터로만 관측된다 (`tests/README.md`)."""

    turns: tuple[GenerationTurn, ...] = (GenerationTurn(),)
    #: 시도 횟수. `generate` 가 불린 시점에 오른다 — 순회 시작 시점에 세면 호출해 놓고
    #: 읽지 않은 경로(있어서는 안 되는 경로)가 카운터에 잡히지 않는다.
    calls: int = 0
    #: 지금까지 내보낸 조각 수. 이벤트 하나가 몇 번째 조각에서 나왔는지를 재는 창이라,
    #: "판정이 확정되기 전에는 이벤트가 나가지 않는다"를 시각이 아니라 순서로 단언한다.
    emitted_chunks: int = 0
    #: 아직 끝나지 않은 시도 수. 취소·타임아웃 뒤에 `0` 이 아니면 그 시도가 만든 자원이
    #: 정리되지 않았다는 뜻이다 — 실물에서는 그 자리에 프로세스가 남는다.
    open_turns: int = 0
    #: 동시에 열려 있던 시도의 최대치. 동시 생성 상한은 넘지 않았다는 사실로만
    #: 관측되는데, 상한에 걸린 요청이 실패가 아니라 대기라서 성패로는 드러나지 않는다.
    peak_open_turns: int = 0
    prompts: list[str] = field(default_factory=list)
    #: 시도마다 받은 시간 상한. 서비스가 정책(횟수·백오프)만 들고 상한은 그대로 어댑터에
    #: 넘기는지 확인하는 데 쓴다.
    timeouts: list[float] = field(default_factory=list)

    def generate(self, prompt: str, *, timeout_seconds: float) -> AsyncIterator[str]:
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        self.prompts.append(prompt)
        self.timeouts.append(timeout_seconds)
        return self._run(turn)

    async def _run(self, turn: GenerationTurn) -> AsyncIterator[str]:
        self.open_turns += 1
        self.peak_open_turns = max(self.peak_open_turns, self.open_turns)
        try:
            for chunk in turn.chunks:
                if turn.delay:
                    await asyncio.sleep(turn.delay)
                self.emitted_chunks += 1
                yield chunk
            if turn.raises is not None:
                raise turn.raises
        finally:
            # 취소(순회 중단)도 여기를 지난다. 실물 어댑터가 `finally` 에서 턴을 중단하고
            # 세션을 회수하는 자리와 같은 지점이라, 정리 누락이 같은 모양으로 드러난다.
            self.open_turns -= 1


class StubResponseCache:
    """인메모리 응답 캐시를 감싸 실패·지연을 주입하고 호출을 세는 대역.

    감싸는 이유는 캐시의 의미를 다시 구현하지 않기 위해서다 (`tests/README.md`)."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 60.0,
        max_entries: int = 100,
        clock: Callable[[], float] | None = None,
        fail: bool = False,
        delay: float = 0.0,
    ) -> None:
        from app.adapters.cache.memory import InMemoryResponseCache

        self._inner = InMemoryResponseCache(
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            **({"clock": clock} if clock else {}),
        )
        self.fail = fail
        self.delay = delay
        self.calls: list[str] = []

    async def lookup_exact(self, fingerprint):
        await self._enter("lookup_exact")
        return await self._inner.lookup_exact(fingerprint)

    async def count_candidates(self, scope):
        await self._enter("count_candidates")
        return await self._inner.count_candidates(scope)

    async def lookup_semantic(self, embedding, *, scope, threshold, candidates):
        await self._enter("lookup_semantic")
        return await self._inner.lookup_semantic(
            embedding, scope=scope, threshold=threshold, candidates=candidates
        )

    async def store(self, fingerprint, entry, *, scope, embedding, negative):
        await self._enter("store")
        await self._inner.store(
            fingerprint, entry, scope=scope, embedding=embedding, negative=negative
        )

    async def invalidate_document(self, document_id):
        await self._enter("invalidate_document")
        return await self._inner.invalidate_document(document_id)

    async def invalidate_negative(self):
        await self._enter("invalidate_negative")
        return await self._inner.invalidate_negative()

    async def discard(self, fingerprint):
        await self._enter("discard")
        await self._inner.discard(fingerprint)

    async def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise StorageUnavailable("주입된 캐시 실패")


class StubDocumentRegistry:
    """인메모리 문서 레지스트리 — 커밋 실패와 조회 사이의 변경을 주입한다.

    훅 둘의 발동 시점과 그 틈이 실재하는 이유는 `tests/README.md` 에 있다."""

    def __init__(
        self,
        *,
        fail_commit: bool = False,
        after_list_all: Callable[[], Awaitable[None]] | None = None,
        before_get: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.documents: dict[str, Document] = {}
        self.fail_commit = fail_commit
        self.commits = 0
        self.after_list_all = after_list_all
        self.before_get = before_get

    async def get(self, document_id: str) -> Document | None:
        await self._fire("before_get")
        return self.documents.get(document_id)

    async def list_all(self) -> list[Document]:
        documents = list(self.documents.values())
        await self._fire("after_list_all")
        return documents

    async def _fire(self, name: str) -> None:
        hook = getattr(self, name)
        if hook is None:
            return
        setattr(self, name, None)
        await hook()

    async def commit(self, document: Document) -> None:
        if self.fail_commit:
            raise StorageUnavailable("주입된 커밋 실패")
        self.commits += 1
        self.documents[document.document_id] = document

    async def delete(self, document_id: str) -> Document | None:
        return self.documents.pop(document_id, None)
