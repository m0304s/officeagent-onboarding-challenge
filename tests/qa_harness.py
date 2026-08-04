"""답변 생성 테스트 하네스.

검색 하네스(`retrieval_harness.py`) 위에 **페이크 생성기 하나만** 얹는다. 근거는 실제
수집·검색 경로로 만든다 — 근거 목록을 손으로 지어내면 `sources` 이벤트가 `/search` 와 같은
모양인지, 인용 값이 그 근거와 일치하는지가 테스트의 조작으로 항상 참이 된다.

**시간을 재지 않는다.** 재시도 백오프도 조각 도착 순서도 시각이 아니라 순서로 단언한다 —
백오프는 `sleeps` 로 관측하고(실제로 자지 않는다), 조각과 이벤트의 관계는
`ScriptedGenerator.emitted_chunks` 로 본다. 벽시계에 기대는 단언은 느린 CI 에서 흔들리고,
흔들리는 테스트는 결국 꺼진다.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI

from app.adapters.protocols import Embedder
from app.core.answers import Citation
from app.services.qa import (
    AnswerEvent,
    DoneEvent,
    ErrorEvent,
    QaEvent,
    QaService,
    SourcesEvent,
)
from tests.retrieval_harness import Harness, make_harness
from tests.stubs import GenerationTurn, ScriptedGenerator, StubResponseCache

#: 근거가 붙는 평범한 질문. 페이크 임베더의 벡터에는 의미가 없으므로 문장 자체는 검색
#: 결과를 바꾸지 않는다 — 하한을 0으로 두어 "문서가 있으면 근거가 있다"만 성립시킨다.
QUESTION = "교육비는 얼마까지 지원되나요?"

VERDICT_ANSWERABLE = "VERDICT: ANSWERABLE\n"
VERDICT_INSUFFICIENT = "VERDICT: INSUFFICIENT\n"


@dataclass
class QaHarness:
    """같은 대역 위에 선 수집·검색·답변 생성."""

    retrieval: Harness
    generator: ScriptedGenerator
    service: QaService
    #: 캐시 저장소 대역. 기본 배선은 `NullResponseCache` 라 이 자리가 비어 있다 —
    #: 캐시를 켠 하네스만 실물 의미를 가진 대역을 든다.
    cache: StubResponseCache | None = None
    #: 재시도 대기 목록. 실제로 자지 않고 여기에 초 단위 값만 쌓인다.
    sleeps: list[float] = field(default_factory=list)

    async def ingest(self, filename: str, text: str) -> None:
        await self.retrieval.ingest(filename, text)

    async def ingest_bytes(self, filename: str, data: bytes) -> None:
        """PDF 처럼 텍스트가 아닌 문서. 인용의 원문 위치가 포맷에 따라 갈리는지 볼 때 쓴다."""
        await self.retrieval.ingestion.ingest(filename, data)

    async def ask(self, question: str = QUESTION, *, top_k: int | None = None) -> list[QaEvent]:
        """질문 하나를 끝까지 읽어 이벤트 목록으로."""
        context = await self.service.prepare(question, top_k=top_k, request_id="req-test")
        return [event async for event in self.service.stream(context)]

    async def ask_watching_chunks(self, question: str = QUESTION) -> list[tuple[QaEvent, int]]:
        """이벤트마다 **그 시점까지 생성기가 내보낸 조각 수**를 함께 기록한다.

        "판정이 확정되기 전에는 `answer` 이벤트가 나가지 않는다"는 개수만으로는 확인되지
        않는다 — 이벤트 수가 맞아도 첫 조각에서 이미 하나 나갔을 수 있다. 이벤트가 몇 번째
        조각에서 나왔는지를 보면 그 성질이 순서로 고정된다.
        """
        context = await self.service.prepare(question, request_id="req-test")
        return [
            (event, self.generator.emitted_chunks) async for event in self.service.stream(context)
        ]


def make_qa_harness(
    *turns: GenerationTurn,
    monkeypatch: pytest.MonkeyPatch | None = None,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 1.0,
    timeout_seconds: float = 30.0,
    concurrency: int = 2,
    top_k: int = 5,
    min_score: float = 0.0,
    cached: bool = False,
    cache: StubResponseCache | None = None,
    retrieval: Harness | None = None,
    embedder: Embedder | None = None,
    semantic_threshold: float = 0.93,
    operation_timeout_seconds: float = 0.2,
    breaker_failures: int = 3,
    breaker_cooldown_seconds: float = 30.0,
    clock: Callable[[], float] | None = None,
) -> QaHarness:
    """대본을 받아 하네스 하나를 만든다.

    `monkeypatch` 를 주면 재시도 대기를 **기록만 하고 실제로 자지 않는다.** 백오프가 늘어난다는
    단언은 값 자체를 보면 되는데, 그걸 확인하려고 테스트가 실제로 3초를 자야 할 이유가 없다.

    **캐시는 기본으로 꺼져 있다.** 배포 기본값은 켜짐이지만, 이 하네스를 쓰는 테스트
    대부분은 "생성기가 몇 번 불렸는가"로 정책을 재는데 캐시가 켜져 있으면 같은 질문의 두
    번째 요청이 생성기를 부르지 않아 그 단언이 통째로 뜻을 잃는다. 캐시를 재는 테스트만
    `cached=True` 로 켠다 — 그 상태가 곧 `cache_enabled=false` 배선이기도 하다.
    """
    # 저장소를 그대로 두고 설정만 바꾼 서비스를 세울 수 있어야 한다 — `retrieval_top_k`
    # 변경이 캐시 항목을 어떻게 가르는지는 같은 저장소 위에서만 재진다.
    storage = retrieval or make_harness(
        top_k=top_k,
        min_score=min_score,
        embedder=embedder,
        cache=cache or (StubResponseCache(clock=clock) if cached else None),
        semantic_threshold=semantic_threshold,
        operation_timeout_seconds=operation_timeout_seconds,
        breaker_failures=breaker_failures,
        breaker_cooldown_seconds=breaker_cooldown_seconds,
        clock=clock,
    )
    searching = storage.searching_with(top_k=top_k) if retrieval else storage.retrieval
    generator = ScriptedGenerator(turns=turns or (GenerationTurn(),))
    harness = QaHarness(
        retrieval=storage,
        generator=generator,
        cache=storage.cache,
        service=QaService(
            searching,
            generator,
            # 수집이 무효화에 쓰는 것과 같은 인스턴스다 — 나누면 무효화가 다른 캐시를 지운다.
            storage.cache_service,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            concurrency=concurrency,
        ),
    )
    if monkeypatch is not None:

        async def record(seconds: float) -> None:
            harness.sleeps.append(seconds)

        monkeypatch.setattr("app.services.qa.asyncio.sleep", record)
    return harness


# ── 이벤트 목록을 읽는 창 ────────────────────────────────────────────────


def names(events: list[QaEvent]) -> list[str]:
    return [event.name.value for event in events]


def answers_of(events: list[QaEvent]) -> list[str]:
    return [event.text for event in events if isinstance(event, AnswerEvent)]


def sources_of(events: list[QaEvent]) -> SourcesEvent:
    return _only(events, SourcesEvent)


def done_of(events: list[QaEvent]) -> DoneEvent:
    return _only(events, DoneEvent)


def error_of(events: list[QaEvent]) -> ErrorEvent:
    return _only(events, ErrorEvent)


def markers_of(citations: tuple[Citation, ...]) -> list[int]:
    return [citation.marker for citation in citations]


def _only(events: list[QaEvent], kind: type) -> QaEvent:
    matched = [event for event in events if isinstance(event, kind)]
    assert len(matched) == 1, f"{kind.__name__} 이 {len(matched)}개다: {names(events)}"
    return matched[0]


# ── SSE 응답을 읽는 창 ───────────────────────────────────────────────────
#
# 시퀀스 단언이 전부 이 위에 선다. 주석을 **따로 세는 것**이 요점이다 — 하트비트가
# 이벤트로 해석되면 계약(이벤트 넷, 종료 하나)이 조용히 깨지는데, 파서가 그것을 이벤트로
# 세어 버리면 그 사고가 테스트를 통과한다.


@dataclass(frozen=True)
class SseEvent:
    """SSE 프레임 하나 — 이름과 JSON 데이터."""

    name: str
    data: dict[str, Any]


@dataclass(frozen=True)
class SseStream:
    """파싱된 응답 하나. 주석은 이벤트가 아니라 개수로만 남는다."""

    events: tuple[SseEvent, ...]
    comments: int

    @property
    def names(self) -> list[str]:
        return [event.name for event in self.events]

    def only(self, name: str) -> dict[str, Any]:
        matched = [event.data for event in self.events if event.name == name]
        assert len(matched) == 1, f"{name} 이 {len(matched)}개다: {self.names}"
        return matched[0]

    def all_of(self, name: str) -> list[dict[str, Any]]:
        return [event.data for event in self.events if event.name == name]


def parse_sse(payload: str) -> SseStream:
    """`event:`/`data:`/빈 줄로 나뉜 응답 본문을 이벤트 목록으로.

    `:` 로 시작하는 줄은 주석이라 이벤트가 아니다 — 세기만 하고 목록에 넣지 않는다."""
    events: list[SseEvent] = []
    comments = 0
    name: str | None = None
    data: list[str] = []

    for line in payload.split("\n"):
        if line.startswith(":"):
            comments += 1
            continue
        if not line:
            if name is not None:
                events.append(SseEvent(name=name, data=json.loads("\n".join(data))))
            name, data = None, []
            continue
        field_name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field_name == "event":
            name = value
        elif field_name == "data":
            data.append(value)

    assert name is None, f"종료되지 않은 프레임이 남았다: {name}"
    return SseStream(tuple(events), comments)


# ── 스트림을 조각 단위로 받는 창 ─────────────────────────────────────────


class AsgiStream:
    """ASGI 앱을 직접 몰아 응답 조각을 도착 순서대로 받는다.

    `httpx.ASGITransport` 를 쓰지 않는 이유가 이 클래스의 존재 이유다 — 그쪽은 앱이 **끝난
    뒤에** 응답을 돌려주므로 "근거가 생성보다 먼저 도착한다"와 "클라이언트가 끊으면 생성이
    멈춘다"가 둘 다 관측되지 않는다. 전자는 순서가 사라져서, 후자는 끊을 수가 없어서다.
    """

    def __init__(self, app: FastAPI, path: str, payload: dict[str, Any]) -> None:
        self._app = app
        self._body = json.dumps(payload).encode()
        self._scope = _http_scope(path, len(self._body))
        self._messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._disconnected = asyncio.Event()
        self._request_sent = False
        self._done = False
        self._task: asyncio.Task[None] | None = None
        #: `http.response.start` 의 상태 코드와 헤더. `start()` 가 채운다.
        self.status_code: int | None = None
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> "AsgiStream":
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    async def start(self) -> None:
        """응답 헤더가 나올 때까지 기다린다."""
        message = await asyncio.wait_for(self._messages.get(), timeout=5.0)
        assert message["type"] == "http.response.start", message
        self.status_code = message["status"]
        self.headers = {
            key.decode().lower(): value.decode() for key, value in message.get("headers", ())
        }

    async def next_chunk(self) -> str | None:
        """비어 있지 않은 다음 조각 하나. 스트림이 끝났으면 `None`."""
        while not self._done:
            message = await asyncio.wait_for(self._messages.get(), timeout=5.0)
            assert message["type"] == "http.response.body", message
            body = message.get("body", b"")
            self._done = not message.get("more_body", False)
            if body:
                return body.decode()
        return None

    async def rest(self) -> str:
        """남은 조각을 전부 모은 본문."""
        parts: list[str] = []
        while (chunk := await self.next_chunk()) is not None:
            parts.append(chunk)
        return "".join(parts)

    async def disconnect(self) -> None:
        """연결을 끊고 앱이 정리를 마칠 때까지 기다린다.

        기다리지 않으면 "자원이 남지 않았다"는 단언이 정리보다 먼저 돈다."""
        self._disconnected.set()
        if self._task is not None:
            await asyncio.wait({self._task}, timeout=5.0)
            self._task.cancel()

    async def _receive(self) -> dict[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        await self._messages.put(message)


def _http_scope(path: str, content_length: int) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(content_length).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
