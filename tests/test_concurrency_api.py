"""수집·검색 중에도 서비스가 살아 있는가 — 오프로드와 동시성.

여기서 고정하는 것은 **HTTP 경계에서 관측되는 성질** 넷이다.

1. 수집의 블로킹 작업(텍스트 추출, 임베딩)이 진행 중이어도 다른 요청이 즉시 응답한다.
2. 서로 다른 문서는 서로를 기다리지 않는다.
3. 같은 문서에 대한 동시 요청은 직렬화되어, 두 요청의 효과가 뒤섞인 상태가 관측되지 않는다.
4. **검색의 오래 걸리는 작업(질의 임베딩, 저장소 질의, 토큰 계산)도 마찬가지다.**

서비스 계층의 같은 성질은 `test_ingestion_pipeline.py` 가 덮는다. 그쪽이 잠금과 상한의
동작을 직접 보는 데 반해, 여기서는 **라우터·의존성 배선까지 포함한 실제 경로**로 본다 —
누군가 라우터에서 블로킹 호출을 하거나 서비스를 요청마다 새로 만들면 그쪽은 통과하고
여기서만 깨진다.

시간 단언은 전부 **주입한 지연 대비 상대값**이다. 절대 시간을 쓰면 느린 CI 에서 무관한
이유로 깨지고, 그때 지연을 늘리는 것으로 무마하면 검증하려던 성질이 사라진다.
"""

import asyncio

import pytest

from tests.api_harness import LONG_KOREAN, upload
from tests.stubs import FakeEmbedder, StubParser, StubVectorStore

DATA = LONG_KOREAN.encode("utf-8")
OTHER_DATA = (LONG_KOREAN + "\n\n연차는 입사 첫해부터 15일입니다.").encode("utf-8")

#: 폴링 간격. 조건이 성립하는 순간을 놓치지 않으면서 루프를 굶기지 않을 만큼 짧게.
_TICK = 0.005


async def until(condition, *, timeout: float = 5.0) -> None:
    """조건이 참이 될 때까지 이벤트 루프를 돌린다.

    "수집이 **실제로 시작된 뒤**에 헬스를 부른다"를 보장하는 데 쓴다. 고정 시간을
    기다리면 느린 환경에서는 아직 시작 전이고, 빠른 환경에서는 이미 끝나 있어
    아무것도 확인하지 못한다.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("조건이 성립하지 않았다 — 수집이 시작되지 않았거나 이미 끝났다")
        await asyncio.sleep(_TICK)


async def measure(coroutine):
    """코루틴 하나의 소요 시간을 이벤트 루프 시계로 잰다."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await coroutine
    return result, loop.time() - started


# ── 오프로드 — 수집 중에도 헬스가 즉시 응답한다 ─────────────────────────


async def test_health_answers_while_text_extraction_is_running(make_client):
    """파서를 이벤트 루프에서 직접 호출하면 여기서 깨진다.

    `StubParser` 의 지연은 **블로킹**(`time.sleep`)이다. 누군가 `asyncio.to_thread` 를
    걷어내면 헬스 응답이 파싱이 끝날 때까지 밀린다.
    """
    delay = 0.3
    parser = StubParser(delay=delay, text=LONG_KOREAN)

    async with make_client(parsers=[parser]) as client:
        ingesting = asyncio.create_task(client.post("/documents", **upload("policy.txt", DATA)))
        await until(lambda: parser.calls > 0)  # 파싱이 실제로 시작된 뒤에 잰다

        health, elapsed = await measure(client.get("/health"))

        assert not ingesting.done(), "수집이 이미 끝나 이 테스트는 아무것도 확인하지 못했다"
        assert (await ingesting).status_code == 201

    assert health.status_code == 200
    assert elapsed < delay, f"헬스가 파싱({delay}s)에 끌려갔다 — {elapsed:.3f}s"


async def test_health_answers_while_embedding_is_running(make_client, settings):
    """임베딩 단계도 이벤트 루프를 붙잡지 않는다.

    배치가 여러 개 나오도록 구성해, 헬스가 **전체 임베딩이 끝나기 전에** 응답하는지 본다.
    배치 사이에 루프로 돌아오지 않으면 헬스가 전체 지연만큼 밀린다.
    """
    delay = 0.1
    embedder = FakeEmbedder(delay=delay)
    batched = settings.model_copy(
        update={"embedding_batch_size": 1, "chunk_size": 300, "chunk_overlap": 60}
    )

    async with make_client(settings=batched, embedder=embedder) as client:
        ingesting = asyncio.create_task(client.post("/documents", **upload("policy.txt", DATA)))
        await until(lambda: len(embedder.batches) > 0)  # 임베딩이 시작된 뒤에 잰다

        health, elapsed = await measure(client.get("/health"))

        assert not ingesting.done(), "수집이 이미 끝나 이 테스트는 아무것도 확인하지 못했다"
        response = await ingesting

    total_embedding = len(embedder.batches) * delay
    assert response.status_code == 201
    assert response.json()["chunk_count"] > 2, "배치가 하나뿐이면 배치 경계를 확인할 수 없다"
    assert health.status_code == 200
    assert elapsed < total_embedding, f"헬스가 임베딩({total_embedding:.1f}s)에 끌려갔다"


# ── 서로 다른 문서는 서로를 기다리지 않는다 ─────────────────────────────


async def test_concurrent_uploads_of_different_documents_do_not_mix(make_client, vector_store):
    """한 문서의 청크가 다른 문서의 `document_id` 로 저장되면 검색 결과가 통째로 섞인다."""
    async with make_client(parsers=[StubParser(delay=0.05, text=LONG_KOREAN)]) as client:
        first, second = await asyncio.gather(
            client.post("/documents", **upload("policy.txt", DATA)),
            client.post("/documents", **upload("guide.txt", OTHER_DATA)),
        )

    assert (first.status_code, second.status_code) == (201, 201)
    bodies = [first.json(), second.json()]
    assert bodies[0]["document_id"] != bodies[1]["document_id"]
    for body in bodies:
        stored = vector_store.chunks_of(body["document_id"])
        assert len(stored) == body["chunk_count"]
        assert {chunk.revision for chunk in stored} == {body["revision"]}


async def test_different_documents_are_not_serialized(make_client):
    """잠금이 문서별이 아니라 전역이 되면 여기서 깨진다."""
    delay = 0.2
    parsers = [StubParser(delay=delay, text=LONG_KOREAN)]

    async with make_client(parsers=parsers) as client:
        responses, elapsed = await measure(
            asyncio.gather(
                client.post("/documents", **upload("policy.txt", DATA)),
                client.post("/documents", **upload("guide.txt", OTHER_DATA)),
            )
        )

    assert [response.status_code for response in responses] == [201, 201]
    assert elapsed < 2 * delay, f"서로 다른 문서가 직렬화됐다 — {elapsed:.3f}s"


# ── 같은 문서는 직렬화된다 ──────────────────────────────────────────────


async def test_concurrent_reuploads_of_the_same_document_leave_one_revision(
    make_client, vector_store
):
    """두 요청의 `get → 저장 → 커밋 → 삭제` 가 인터리빙되면 두 리비전의 청크가 함께 남는다.

    **어느 쪽이 이기는지는 단언하지 않는다** — 비결정적이다. 규정하는 것은 최종 상태가
    두 요청을 어느 한 순서로 차례로 실행한 결과와 같다는 것뿐이다.
    """
    async with make_client(parsers=[StubParser(delay=0.05, text=LONG_KOREAN)]) as client:
        first, second = await asyncio.gather(
            client.post("/documents", **upload("policy.txt", DATA)),
            client.post("/documents", **upload("policy.txt", OTHER_DATA)),
        )
        bodies = [first.json(), second.json()]
        document_id = bodies[0]["document_id"]
        detail = (await client.get(f"/documents/{document_id}")).json()

    assert first.status_code in (200, 201)
    assert second.status_code in (200, 201)
    assert bodies[1]["document_id"] == document_id
    assert detail["revision"] in {body["revision"] for body in bodies}

    stored = vector_store.chunks_of(document_id)
    assert len(stored) == detail["chunk_count"]
    assert {chunk.revision for chunk in stored} == {detail["revision"]}, "두 리비전이 함께 남았다"


async def test_a_reupload_racing_a_delete_ends_in_one_of_the_two_serial_states(
    make_client, vector_store
):
    """뒤섞인 상태 — 삭제 성공 응답과 잔여 청크가 함께 관측되는 상태 — 가 없어야 한다."""
    async with make_client(parsers=[StubParser(delay=0.05, text=LONG_KOREAN)]) as client:
        created = (await client.post("/documents", **upload("policy.txt", DATA))).json()
        document_id = created["document_id"]

        reupload, removal = await asyncio.gather(
            client.post("/documents", **upload("policy.txt", OTHER_DATA)),
            client.delete(f"/documents/{document_id}"),
        )

        listing = (await client.get("/documents")).json()

    assert reupload.status_code in (200, 201)
    assert removal.status_code == 204
    stored = len(vector_store.chunks_of(document_id))

    if listing["count"]:
        # 삭제가 먼저 반영된 순서 — 재업로드가 문서를 다시 만들었다.
        detail = listing["documents"][0]
        assert detail["document_id"] == document_id
        assert stored == detail["chunk_count"]
    else:
        # 재업로드가 먼저 반영된 순서 — 삭제가 그 결과까지 지웠다.
        assert stored == 0


# ── 검색 중에도 헬스가 즉시 응답한다 ────────────────────────────────────
#
# 검색의 오래 걸리는 작업은 셋이다 — 토큰 계산(블로킹, 라우터가 오프로드), 질의 임베딩,
# 저장소 질의. 셋 다 같은 방식으로 확인한다: 그 작업이 **실제로 시작된 뒤** 헬스를 부르고,
# 헬스가 그 작업보다 먼저 끝나는지 본다.

QUERY = "교육비는 얼마까지 지원되나요?"


async def test_health_answers_while_a_query_is_being_embedded(make_client):
    """질의 임베딩이 진행 중이어도 다른 요청이 기다리지 않는다.

    **문서를 올리지 않는다.** 질의 임베딩은 대상 집합 확정보다 앞이라 문서 유무와
    무관하게 계산되고, 지연이 걸린 임베더로 수집까지 하면 준비 자체가 느려진다.
    """
    delay = 0.3
    embedder = FakeEmbedder(delay=delay)

    async with make_client(embedder=embedder) as client:
        searching = asyncio.create_task(client.post("/search", json={"query": QUERY}))
        await until(lambda: len(embedder.queries) > 0)  # 임베딩이 시작된 뒤에 잰다

        health, elapsed = await measure(client.get("/health"))

        assert not searching.done(), "검색이 이미 끝나 이 테스트는 아무것도 확인하지 못했다"
        assert (await searching).status_code == 200

    assert health.status_code == 200
    assert elapsed < delay, f"헬스가 질의 임베딩({delay}s)에 끌려갔다 — {elapsed:.3f}s"


async def test_health_answers_while_the_store_is_being_queried(make_client, settings):
    """저장소 질의가 진행 중이어도 다른 요청이 기다리지 않는다.

    `StubVectorStore` 의 질의 지연은 실물 어댑터와 같은 자리에서 스레드풀로 나간다.
    어댑터가 오프로드를 걷어내면 실물 경로가 여기서 깨지는 것과 같은 방식으로 깨진다.

    **문서를 먼저 올린다.** 대상 집합이 비면 서비스가 저장소를 아예 건드리지 않아
    지연이 발동하지 않는다 — 그 상태로는 테스트가 통과해도 아무것도 확인되지 않는다.
    """
    delay = 0.3
    store = StubVectorStore(query_delay=delay)
    searchable = settings.model_copy(update={"retrieval_min_score": 0.0})

    async with make_client(settings=searchable, vector_store=store) as client:
        created = await client.post("/documents", **upload("policy.txt", DATA))
        assert created.status_code == 201, created.text

        searching = asyncio.create_task(client.post("/search", json={"query": QUERY}))
        await until(lambda: len(store.queries) > 0)  # 저장소 질의가 시작된 뒤에 잰다

        health, elapsed = await measure(client.get("/health"))

        assert not searching.done(), "검색이 이미 끝나 이 테스트는 아무것도 확인하지 못했다"
        response = await searching

    assert response.status_code == 200
    assert response.json()["results"], "저장소 질의가 근거를 하나도 내지 않았다"
    assert health.status_code == 200
    assert elapsed < delay, f"헬스가 저장소 질의({delay}s)에 끌려갔다 — {elapsed:.3f}s"


async def test_health_answers_while_a_query_is_being_counted(make_client):
    """토큰 계산도 이벤트 루프를 붙잡지 않는다.

    토큰 계산은 프로토콜상 **동기**라 오프로드 책임이 라우터에 있다(수집의
    `_guard_tokens` 와 같은 자리·같은 이유). 그 `asyncio.to_thread` 를 걷어내면 긴 질의
    하나가 다른 요청을 통째로 막는데, 나머지 검색 테스트는 전부 통과한다 — 계산 결과가
    같기 때문이다. 지연이 블로킹이라야 그 차이가 드러난다.

    계산은 임베딩 **이전**이므로, 여기서 세는 동안 `queries` 는 아직 비어 있다.
    """
    delay = 0.3
    embedder = FakeEmbedder(count_delay=delay)

    async with make_client(embedder=embedder) as client:
        searching = asyncio.create_task(client.post("/search", json={"query": QUERY}))
        # 요청이 라우터에 닿아 토큰 계산에 들어갈 틈을 준다. 조건으로 걸 관측점이 없어
        # (계산은 부수 효과가 없다) 한 틱만 양보하고, 아래 `not done()` 이 실제로 계산
        # 중이었음을 확인한다.
        await asyncio.sleep(_TICK)

        health, elapsed = await measure(client.get("/health"))

        assert not searching.done(), "검색이 이미 끝나 이 테스트는 아무것도 확인하지 못했다"
        assert (await searching).status_code == 200

    assert health.status_code == 200
    assert elapsed < delay, f"헬스가 토큰 계산({delay}s)에 끌려갔다 — {elapsed:.3f}s"


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_the_concurrency_limit_does_not_reject_requests(make_client, settings, concurrency):
    """상한에 걸린 요청은 실패하지 않고 **기다린다.**

    상한을 초과 요청 거절로 구현하면 여기서 깨진다. 처리량을 제한하는 것과 요청을
    떨어뜨리는 것은 다른 일이다.
    """
    limited = settings.model_copy(update={"ingestion_concurrency": concurrency})
    parsers = [StubParser(delay=0.05, text=LONG_KOREAN)]

    async with make_client(settings=limited, parsers=parsers) as client:
        responses = await asyncio.gather(
            *(
                client.post("/documents", **upload(f"doc-{index}.txt", DATA + bytes([index])))
                for index in range(4)
            )
        )

    assert [response.status_code for response in responses] == [201] * 4
