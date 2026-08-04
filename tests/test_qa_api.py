"""`POST /qa` — HTTP 로 내려와야만 드러나는 넷.

SSE 형식, 스트림 밖 실패의 상태 코드, 근거의 도착 순서, 하트비트. 뒤의 둘은
`ASGITransport` 로 관측되지 않아 `AsgiStream` 이 프로토콜을 직접 쓴다.
"""

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from tests.api_harness import LONG_KOREAN, upload
from tests.qa_harness import (
    QUESTION,
    VERDICT_ANSWERABLE,
    AsgiStream,
    parse_sse,
)
from tests.stubs import FakeEmbedder, GenerationTurn, ScriptedGenerator, StubVectorStore

DATA = LONG_KOREAN.encode("utf-8")

ANSWER_BODY = "교육비는 연 200만원까지 지원됩니다 [1]."


def scripted(*chunks: str, delay: float = 0.0) -> ScriptedGenerator:
    """조각 하나하나를 대본으로 받는 생성기. 시도가 여럿 필요한 경로는 여기 없다."""
    return ScriptedGenerator(turns=(GenerationTurn(chunks=chunks, delay=delay),))


@pytest.fixture
def qa_settings(settings):
    """하한만 낮춘 설정.

    페이크 점수에는 의미가 없어 운영 기본값이면 모든 스트림이 `no_evidence` 가 된다."""
    return settings.model_copy(update={"retrieval_min_score": 0.0})


@pytest.fixture
def generator() -> ScriptedGenerator:
    return scripted(VERDICT_ANSWERABLE, ANSWER_BODY)


@pytest.fixture
async def client(make_client, qa_settings, generator):
    async with make_client(settings=qa_settings, generator=generator) as c:
        yield c


async def ingest(client, filename: str = "policy.txt", data: bytes = DATA) -> dict:
    response = await client.post("/documents", **upload(filename, data))
    assert response.status_code in (200, 201), response.text
    return response.json()


async def ask(client, question: str = QUESTION, **body):
    return await client.post("/qa", json={"question": question, **body})


@asynccontextmanager
async def app_with_document(make_app, **overrides):
    """문서 한 건이 이미 수집된 앱. 스트림을 직접 모는 테스트가 쓴다."""
    app = make_app(**overrides)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await ingest(client)
        yield app


# ── 성공 경로 (5.7) ──────────────────────────────────────────────────────


async def test_a_question_answers_with_an_event_stream(client):
    await ingest(client)

    response = await ask(client)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


async def test_the_event_names_arrive_in_the_contracted_order(client, generator):
    await ingest(client)

    stream = parse_sse((await ask(client)).text)

    assert stream.names[0] == "sources"
    assert stream.names[-1] == "done"
    assert stream.names.count("answer") >= 1
    assert "error" not in stream.names
    assert generator.calls == 1


async def test_the_stream_closes_with_exactly_one_terminal_event(client):
    await ingest(client)

    stream = parse_sse((await ask(client)).text)

    assert stream.names.count("done") + stream.names.count("error") == 1


async def test_the_sources_event_is_shaped_like_the_search_response(client):
    """같은 질문의 두 경로가 같은 근거를 같은 모양으로 낸다.

    모양이 갈리면 소비자가 근거를 읽는 코드를 두 벌 들게 된다."""
    await ingest(client)
    search = (await client.post("/search", json={"query": QUESTION})).json()

    sources = parse_sse((await ask(client)).text).only("sources")

    assert sources["count"] == search["count"]
    assert sources["top_k"] == search["top_k"]
    assert sources["results"] == search["results"]
    assert sources["target_documents"] >= 1


async def test_answer_chunks_join_into_the_done_answer(client):
    """조각을 이어 붙인 것이 최종본이라는 불변식은 전송 형식을 지나서도 성립해야 한다."""
    await ingest(client)

    stream = parse_sse((await ask(client)).text)
    done = stream.only("done")

    assert "".join(event["text"] for event in stream.all_of("answer")) == done["answer"]
    assert done["finish_reason"] == "stop"
    assert VERDICT_ANSWERABLE.strip() not in done["answer"]


async def test_a_citation_repeats_the_source_it_points_at(client):
    """인용은 그 스트림이 이미 보여 준 근거를 가리킬 뿐 새 사실을 만들지 않는다."""
    await ingest(client)

    stream = parse_sse((await ask(client)).text)
    citation = stream.only("done")["citations"][0]
    source = stream.only("sources")["results"][citation["marker"] - 1]

    assert citation["document_id"] == source["document_id"]
    assert citation["revision"] == source["revision"]
    assert citation["chunk_index"] == source["chunk_index"]
    assert citation["score"] == source["score"]


async def test_a_question_without_documents_ends_in_no_evidence(client, generator):
    """근거가 0건이면 생성기를 부르지 않는다 — 그 사실은 호출 횟수로만 관측된다."""
    stream = parse_sse((await ask(client)).text)

    assert stream.names == ["sources", "done"]
    assert stream.only("sources")["count"] == 0
    assert stream.only("done")["finish_reason"] == "no_evidence"
    assert stream.only("done")["answer"] == ""
    assert generator.calls == 0


async def test_the_request_can_specify_top_k(client):
    await ingest(client)

    sources = parse_sse((await ask(client, top_k=2)).text).only("sources")

    assert sources["top_k"] == 2
    assert len(sources["results"]) <= 2


# ── 스트림 밖 실패 (5.8) ─────────────────────────────────────────────────
# 스트림 안으로 들어가면 관측되는 것이 본문 없는 200 이 되어 어떤 시나리오로도 안 잡힌다.


def assert_rejected_outside_the_stream(response, code: str, generator, embedder=None) -> None:
    assert response.status_code in (422, 503), response.text
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.json()["error"]["code"] == code
    assert generator.calls == 0, "거부된 요청이 생성기를 불렀다"
    if embedder is not None:
        assert embedder.queries == [], "거부된 요청이 임베딩을 계산했다"


async def test_an_empty_question_is_rejected_before_the_stream_opens(client, generator, embedder):
    response = await ask(client, "   ")

    assert_rejected_outside_the_stream(response, "empty_query", generator, embedder)


async def test_a_question_beyond_the_character_ceiling_is_rejected(
    client, qa_settings, generator, embedder
):
    response = await ask(client, "가" * (qa_settings.retrieval_max_query_chars + 1))

    assert_rejected_outside_the_stream(response, "query_too_long", generator, embedder)


async def test_a_question_beyond_the_embedding_window_is_rejected(
    make_client, qa_settings, generator
):
    """문자 수는 통과하지만 토크나이저 창을 넘는 질문. 두 상한이 따로 걸린다."""
    narrow = FakeEmbedder(max_input_tokens=4, chars_per_token=1)

    async with make_client(settings=qa_settings, embedder=narrow, generator=generator) as client:
        response = await ask(client, "교육비 지원 한도는 얼마인가요")

    assert_rejected_outside_the_stream(response, "query_too_long", generator)


async def test_a_top_k_beyond_the_ceiling_is_rejected(client, qa_settings, generator, embedder):
    response = await ask(client, top_k=qa_settings.retrieval_max_top_k + 1)

    assert_rejected_outside_the_stream(response, "invalid_top_k", generator, embedder)


async def test_a_top_k_below_one_is_a_validation_error(client, generator, embedder):
    """하한은 요청 모델이 막는다 — `/search` 와 같은 코드로 끝나야 한다."""
    response = await ask(client, top_k=0)

    assert_rejected_outside_the_stream(response, "validation_error", generator, embedder)


async def test_a_store_failure_is_a_503_outside_the_stream(make_client, qa_settings, generator):
    """저장소 장애가 `/search`(503)와 `/qa`(200 + error)에서 갈리면 모니터링이 규칙을 둘 든다."""
    store = StubVectorStore(fail_query=True)

    async with make_client(
        settings=qa_settings, vector_store=store, generator=generator
    ) as client:
        await ingest(client)
        response = await ask(client)

    assert_rejected_outside_the_stream(response, "storage_unavailable", generator)
    # `/search` 와 같은 봉투다 — 스택 트레이스도 저장소 라이브러리의 내부 사정도 실리지
    # 않는다. 메시지는 어댑터가 자기 경계에서 우리 문구로 바꿔 던진 것이다.
    assert set(response.json()["error"]) == {"code", "message"}
    assert "Traceback" not in response.text


# ── 도착 순서와 하트비트 (5.9) ───────────────────────────────────────────


async def test_the_sources_event_arrives_before_generation_produces_anything(
    make_app, qa_settings
):
    """`sources` 선행이 스트리밍으로 실제로 앞당겨지는 것의 전부다.

    이벤트 목록은 끝난 뒤의 사진이라, 전부 모아 보내도 똑같이 보인다."""
    generator = scripted(VERDICT_ANSWERABLE, ANSWER_BODY, delay=0.05)

    async with app_with_document(make_app, settings=qa_settings, generator=generator) as app:
        async with AsgiStream(app, "/qa", {"question": QUESTION}) as stream:
            await stream.start()
            first = await stream.next_chunk()

            assert stream.status_code == 200
            assert first is not None and first.startswith("event: sources")
            assert generator.emitted_chunks == 0, "생성이 이미 조각을 냈다 — 순서가 확인되지 않았다"

            body = first + await stream.rest()

    assert parse_sse(body).names[-1] == "done"


async def test_a_quiet_stream_keeps_alive_without_emitting_events(make_app, qa_settings):
    """유지 신호는 주석이라 이벤트 목록에 나타나지 않는다.

    이벤트로 내보내면 어휘가 다섯이 되어 계약이 깨지는데, 화면에는 아무 차이도 없다."""
    heartbeating = qa_settings.model_copy(update={"qa_sse_heartbeat_seconds": 0.01})
    generator = scripted(VERDICT_ANSWERABLE, ANSWER_BODY, delay=0.08)

    async with app_with_document(make_app, settings=heartbeating, generator=generator) as app:
        async with AsgiStream(app, "/qa", {"question": QUESTION}) as stream:
            await stream.start()
            body = await stream.rest()

    parsed = parse_sse(body)
    assert parsed.comments >= 1, "조용한 구간이 하트비트 간격보다 길었는데 신호가 없었다"
    assert parsed.names == ["sources", "answer", "done"]


# ── 클라이언트 종료 (5.5) ────────────────────────────────────────────────


async def test_disconnecting_stops_generation_and_leaves_nothing_open(make_app, qa_settings):
    """정리하지 않으면 취소된 요청 하나가 프로세스 하나씩을 남긴다.

    답이 마음에 안 들어 새 질문을 보내는 것은 흔한 조작이라, 이 누수는 정상 사용에서 쌓인다."""
    generator = scripted(VERDICT_ANSWERABLE, "앞부분", "뒷부분", "꼬리", delay=0.05)

    async with app_with_document(make_app, settings=qa_settings, generator=generator) as app:
        async with AsgiStream(app, "/qa", {"question": QUESTION}) as stream:
            await stream.start()
            while (chunk := await stream.next_chunk()) is not None:
                if chunk.startswith("event: answer"):
                    break

            await stream.disconnect()

    assert generator.emitted_chunks < 4, "스트림이 이미 끝나 이 테스트는 아무것도 확인하지 못했다"
    assert generator.open_turns == 0, "끊긴 요청이 시도를 열어 둔 채 남겼다"
