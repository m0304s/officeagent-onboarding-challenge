"""`/qa` 와 캐시가 만나는 자리 — 히트가 미스와 같은 응답인가, 무엇을 아끼는가.

아끼는 것을 호출 부재로 잰다. 캐시가 조용히 꺼져도 응답은 멀쩡하므로, 대역의 호출 기록이
없으면 이 층은 아무것도 확인하지 못한다. 하네스의 캐시는 `cached=True` 로만 켜진다.
"""

from dataclasses import replace

import pytest

from app.core.answers import FinishReason
from app.core.exceptions import LlmGenerationFailed
from tests.api_harness import upload
from tests.qa_harness import (
    QUESTION,
    VERDICT_ANSWERABLE,
    VERDICT_INSUFFICIENT,
    answers_of,
    done_of,
    make_qa_harness,
    names,
    parse_sse,
    sources_of,
)
from tests.retrieval_harness import GUIDE, POLICY
from tests.stubs import (
    FakeReranker,
    GenerationTurn,
    ScriptedGenerator,
    StubResponseCache,
    SynonymEmbedder,
)

ANSWER = "교육비는 연 200만원까지 지원됩니다 [1]."

#: 뜻이 같은 두 질의를 한 벡터로 묶는다. 해시 페이크로는 "글자는 다르고 뜻은 같은" 쌍을
#: 만들 수 없어, 유사 매치 층을 요청 경로에서 재려면 이 주입이 필요하다.
SIMILAR = "교육비 얼마까지 지원되나요?"
SYNONYMS = {QUESTION: "교육비 지원", SIMILAR: "교육비 지원"}


def answering(*, delay: float = 0.0) -> GenerationTurn:
    return GenerationTurn(chunks=(VERDICT_ANSWERABLE, ANSWER), delay=delay)


async def cached_harness(*turns, **kwargs):
    """문서 하나를 올린 채 캐시를 켠 하네스."""
    harness = make_qa_harness(*(turns or (answering(),)), cached=True, **kwargs)
    await harness.ingest("policy.txt", POLICY)
    return harness


# ── 히트가 무엇을 아끼는가 ───────────────────────────────────────────────


async def test_second_identical_question_does_not_call_the_generator():
    """같은 질문의 두 번째 요청은 생성기를 부르지 않는다 (`response-cache`)."""
    harness = await cached_harness()

    first = await harness.ask()
    second = await harness.ask()

    assert harness.generator.calls == 1
    assert done_of(second).answer.text == done_of(first).answer.text


async def test_a_hit_does_not_run_retrieval_again():
    """캐시가 아끼는 것은 생성 비용만이 아니다 — 임베딩·벡터 질의·융합이 전부 빠진다."""
    harness = await cached_harness()
    await harness.ask()
    harness.retrieval.store.queries.clear()
    harness.retrieval.embedder.queries.clear()

    await harness.ask()

    assert harness.retrieval.store.queries == []
    assert harness.retrieval.embedder.queries == [], "정확 매치는 질의 임베딩도 만들지 않는다"


async def test_a_similar_question_hits_without_the_generator():
    """정확 매치가 실패해도 뜻이 충분히 가까우면 캐시로 답한다."""
    harness = await cached_harness(embedder=SynonymEmbedder(SYNONYMS))
    await harness.ask(QUESTION)

    events = await harness.ask(SIMILAR)

    assert harness.generator.calls == 1
    assert done_of(events).cache.layer.value == "semantic"


async def test_a_different_question_still_reaches_the_generator():
    """임계값 미달이 히트가 되면 캐시가 틀린 답을 내는 층이 된다."""
    harness = await cached_harness(answering(), answering())
    await harness.ask(QUESTION)

    await harness.ask("연차는 며칠인가요?")

    assert harness.generator.calls == 2


# ── 히트의 응답 계약 ─────────────────────────────────────────────────────


async def test_a_hit_has_the_same_event_sequence_as_a_miss():
    """히트 전용 응답 형식이나 별도 엔드포인트를 두지 않는다 (`answer-generation`)."""
    harness = await cached_harness()

    miss = await harness.ask()
    hit = await harness.ask()

    assert names(hit) == names(miss) == ["sources", "answer", "done"]


async def test_a_hit_carries_the_sources_it_was_generated_from():
    """근거가 있었던 질문의 히트에서 근거 목록이 비면 화면이 히트와 미스를 갈라 그려야 한다."""
    harness = await cached_harness()
    miss = await harness.ask()

    hit = await harness.ask()

    assert sources_of(hit).results == sources_of(miss).results
    assert sources_of(hit).results, "근거가 있었던 질문이다"
    assert sources_of(hit).top_k == sources_of(miss).top_k
    assert sources_of(hit).target_documents == sources_of(miss).target_documents


async def test_joined_answer_pieces_equal_the_final_answer_on_a_hit():
    """조각을 이어 붙인 것이 `done.answer` 와 같다는 불변식은 히트에서도 성립한다."""
    harness = await cached_harness()
    await harness.ask()

    hit = await harness.ask()

    assert "".join(answers_of(hit)) == done_of(hit).answer.text


async def test_a_hit_replays_the_body_as_a_single_piece():
    """히트에는 조각이 도착하는 사건이 없다 — 경계를 재생하면 없던 진행을 연출하는 일이다."""
    harness = await cached_harness(GenerationTurn(chunks=(VERDICT_ANSWERABLE, "앞", "뒤 [1]")))
    miss = await harness.ask()

    hit = await harness.ask()

    assert len(answers_of(miss)) == 2
    assert len(answers_of(hit)) == 1


async def test_a_hit_keeps_the_citations_of_the_first_request():
    harness = await cached_harness()
    miss = await harness.ask()

    hit = await harness.ask()

    assert done_of(hit).answer.citations == done_of(miss).answer.citations
    assert done_of(hit).answer.citations, "인용이 있는 답변이다"


async def test_no_evidence_hit_emits_no_answer_event():
    """본문이 빈 종료의 히트에는 `answer` 가 없어야 한다 — 미스에서도 그 경로는 안 낸다."""
    harness = make_qa_harness(cached=True)  # 문서를 올리지 않는다

    miss = await harness.ask()
    hit = await harness.ask()

    assert names(miss) == names(hit) == ["sources", "done"]
    assert done_of(hit).answer.finish_reason is FinishReason.NO_EVIDENCE


# ── 응답이 밝히는 것 ─────────────────────────────────────────────────────


async def test_a_miss_says_it_is_a_miss():
    """필드가 없는 것과 `false` 인 것은 다른 뜻이다 (`response-cache`)."""
    harness = await cached_harness()

    done = done_of(await harness.ask())

    assert done.cache.hit is False
    assert done.cache.layer is None and done.cache.similarity is None


async def test_the_two_hit_layers_are_distinguishable():
    """유사 매치 쪽에만 유사도 값이 실린다."""
    harness = await cached_harness(embedder=SynonymEmbedder(SYNONYMS))
    await harness.ask(QUESTION)

    exact = done_of(await harness.ask(QUESTION))
    semantic = done_of(await harness.ask(SIMILAR))

    assert exact.cache.layer.value == "exact" and exact.cache.similarity is None
    assert semantic.cache.layer.value == "semantic"
    assert semantic.cache.similarity == pytest.approx(1.0)


async def test_elapsed_ms_is_this_requests_time_not_a_cached_copy():
    """캐시된 값을 그대로 실으면 히트가 원래 생성에 걸린 시간을 보고하게 된다.

    시각을 재지 않는다 — 시작 시각을 옮긴 컨텍스트로 그 성질을 순서 없이 고정한다."""
    harness = await cached_harness()
    await harness.ask()

    context = await harness.service.prepare(QUESTION, request_id="req-test")
    aged = replace(context, started_at=context.started_at - 5.0)
    events = [event async for event in harness.service.stream(aged)]

    assert done_of(events).cache.hit
    assert done_of(events).elapsed_ms >= 5_000


# ── 답하지 못한 종료도 캐시된다 ──────────────────────────────────────────


async def test_no_evidence_is_cached_and_skips_retrieval():
    """다시 물어도 결과가 같은데 검색과 생성을 다시 할 이유가 없다."""
    harness = make_qa_harness(cached=True)
    await harness.ask()
    harness.retrieval.store.queries.clear()

    hit = await harness.ask()

    assert done_of(hit).cache.hit
    assert done_of(hit).answer.finish_reason is FinishReason.NO_EVIDENCE
    assert harness.retrieval.store.queries == []


async def test_insufficient_evidence_is_cached_and_skips_retrieval():
    """근거는 있었으나 답할 수 없다고 판정된 종료도 같다."""
    harness = await cached_harness(
        GenerationTurn(chunks=(VERDICT_INSUFFICIENT, "문서에서 확인할 수 없습니다."))
    )
    await harness.ask()
    harness.retrieval.store.queries.clear()

    hit = await harness.ask()

    assert done_of(hit).cache.hit
    assert done_of(hit).answer.finish_reason is FinishReason.INSUFFICIENT_EVIDENCE
    assert harness.retrieval.store.queries == []
    assert harness.generator.calls == 1


async def test_a_failed_stream_is_not_cached():
    """실패를 캐시하면 일시적 장애가 TTL 동안 굳는다."""
    harness = await cached_harness(
        GenerationTurn(raises=LlmGenerationFailed("주입된 실패")),
        answering(),
        max_attempts=1,
    )
    failed = await harness.ask()
    assert names(failed) == ["sources", "error"]

    recovered = await harness.ask()

    assert names(recovered) == ["sources", "answer", "done"]
    assert not done_of(recovered).cache.hit


# ── 키 재료가 어디에서 오는가 ────────────────────────────────────────────


async def test_an_omitted_top_k_and_the_default_top_k_share_one_entry():
    """`None` 이 키에 들어가면 여기서 깨진다 (design 결정 14)."""
    harness = await cached_harness(top_k=5)
    await harness.ask(top_k=None)

    hit = await harness.ask(top_k=5)

    assert done_of(hit).cache.hit
    assert harness.generator.calls == 1


async def test_changing_the_configured_top_k_does_not_reuse_the_entry():
    """설정 기본값을 바꾸면 옛 항목이 새 기본값의 답인 척 남지 않는다."""
    harness = await cached_harness(top_k=5)
    await harness.ask(top_k=None)

    # 저장소도 캐시도 그대로 둔 채 설정만 바꾼 재기동이다.
    revised = make_qa_harness(
        answering(), retrieval=harness.retrieval, cache=harness.cache, top_k=3
    )
    second = await revised.ask(top_k=None)

    assert not done_of(second).cache.hit
    assert revised.generator.calls == 1


async def test_retrieval_and_cache_share_one_index_signature():
    """캐시가 서명을 따로 받으면 세 번째 유도 지점이 생긴다 (design 결정 14)."""
    harness = await cached_harness()

    assert harness.service._retrieval.index_signature == harness.retrieval.index_signature


async def test_turning_the_reranker_on_does_not_reuse_the_entry():
    """켠 배포가 끈 배포의 답을 그대로 내면, 응답의 `sources` 가 그 답변이 실제로 본
    근거가 아니게 된다 (`response-cache`)."""
    harness = await cached_harness()
    await harness.ask()

    # 저장소도 캐시도 그대로 둔 채 리랭커만 켠 재기동이다.
    reranked = make_qa_harness(
        answering(),
        retrieval=harness.retrieval,
        cache=harness.cache,
        reranker=FakeReranker(),
    )
    second = await reranked.ask()

    assert not done_of(second).cache.hit
    assert reranked.generator.calls == 1


async def test_a_different_reranker_model_does_not_reuse_the_entry():
    """서명이 켜짐/꺼짐 두 상태뿐이면 모델 교체가 캐시에 가려 관측되지 않는다."""
    harness = await cached_harness(reranker=FakeReranker(name="first"))
    await harness.ask()

    replaced = make_qa_harness(
        answering(),
        retrieval=harness.retrieval,
        cache=harness.cache,
        reranker=FakeReranker(name="second"),
    )
    second = await replaced.ask()

    assert not done_of(second).cache.hit
    assert replaced.generator.calls == 1


async def test_a_hit_replays_the_ranking_signal_it_was_generated_under():
    """리랭킹 점수만 왕복하면 히트가 그 점수를 든 채 융합 순서라고 말하게 된다."""
    harness = await cached_harness(reranker=FakeReranker())
    miss = await harness.ask()

    hit = await harness.ask()

    assert done_of(hit).cache.hit
    assert sources_of(miss).ordered_by == "rerank", "리랭커를 배선했는데 융합 순서로 끝났다"
    assert sources_of(hit).ordered_by == sources_of(miss).ordered_by
    assert sources_of(hit).reranker == sources_of(miss).reranker
    assert sources_of(hit).results == sources_of(miss).results
    assert all(source.rerank_score is not None for source in sources_of(hit).results)


async def test_the_reranker_being_off_leaves_the_existing_cache_behaviour_alone():
    """끈 구성에서 정확 매치·유사 매치·무효화가 리랭킹 도입 이전과 같아야 한다.

    빈 서명이 요청마다 다른 값이면 히트가 아예 나지 않는다 — 여기서 먼저 깨진다."""
    harness = await cached_harness(answering(), answering(), embedder=SynonymEmbedder(SYNONYMS))
    await harness.ask(QUESTION)

    exact = await harness.ask(QUESTION)
    semantic = await harness.ask(SIMILAR)
    await harness.ingest("policy.txt", POLICY + "\n교육비 한도가 300만원으로 올랐습니다.\n")
    after_change = await harness.ask(QUESTION)

    assert done_of(exact).cache.layer.value == "exact"
    assert done_of(semantic).cache.layer.value == "semantic"
    assert not done_of(after_change).cache.hit, "문서가 바뀌었는데 옛 답변이 나갔다"
    assert sources_of(exact).ordered_by == "fusion"
    assert sources_of(exact).reranker is None


# ── 캐시를 껐을 때 ───────────────────────────────────────────────────────


async def test_a_disabled_cache_never_hits():
    """캐시를 끈 상태에서 같은 질문을 두 번 보내면 둘 다 미스여야 한다 (`response-cache`).

    캐시를 끄는 이유가 대개 "배제하고 원인을 보겠다"라, 히트가 나면 배제가 성립하지 않는다."""
    harness = make_qa_harness(answering(), answering())  # cached=False 가 기본이다
    await harness.ingest("policy.txt", POLICY)

    first = await harness.ask()
    second = await harness.ask()

    assert harness.generator.calls == 2
    assert not done_of(first).cache.hit and not done_of(second).cache.hit


async def test_a_dead_cache_store_still_answers():
    """캐시 저장소에 닿지 못해도 `/qa` 는 정상 동작해야 한다 (`response-cache`)."""
    harness = make_qa_harness(answering(), answering(), cache=StubResponseCache(fail=True))
    await harness.ingest("policy.txt", POLICY)

    first = await harness.ask()
    second = await harness.ask()

    assert names(first) == names(second) == ["sources", "answer", "done"]
    assert not done_of(second).cache.hit
    assert harness.generator.calls == 2


# ── HTTP 경계 ────────────────────────────────────────────────────────────


async def ingest_over_http(client, filename: str, text: str) -> None:
    response = await client.post("/documents", **upload(filename, text.encode("utf-8")))
    assert response.status_code in (200, 201), response.text


async def ask_over_http(client, question: str = QUESTION) -> dict:
    response = await client.post("/qa", json={"question": question})
    assert response.status_code == 200, response.text
    return parse_sse(response.text).only("done")


@pytest.fixture
def generator():
    """판정 줄과 본문 한 조각. 재시도가 필요한 경로는 이 파일에 없다."""
    return ScriptedGenerator(turns=(answering(),))


@pytest.fixture
def cache_settings(settings):
    """하한만 낮춘 설정. 페이크 임베더의 점수에는 의미가 없어 기본값이면 근거가 사라진다."""
    return settings.model_copy(update={"retrieval_min_score": 0.0})


async def test_qa_survives_an_unreachable_cache_store(make_client, cache_settings, generator):
    """닿을 수 없는 주소로 배선된 캐시가 `/qa` 를 `5xx` 로 만들지 않는다.

    기본 테스트 설정의 `cache_url` 이 실제로 닿지 않아, 배포에서 캐시가 죽은 것과 같다."""
    async with make_client(settings=cache_settings, generator=generator) as client:
        await ingest_over_http(client, "policy.txt", POLICY)

        done = await ask_over_http(client)

        assert done["cache_hit"] is False
        assert done["cache_layer"] is None and done["cache_similarity"] is None


async def test_qa_never_hits_when_the_cache_is_disabled(make_client, cache_settings, generator):
    """비활성 배선에서 같은 질문을 두 번 보내도 둘 다 미스여야 한다."""
    disabled = cache_settings.model_copy(update={"cache_enabled": False})
    async with make_client(settings=disabled, generator=generator) as client:
        await ingest_over_http(client, "guide.md", GUIDE)

        first = await ask_over_http(client)
        second = await ask_over_http(client)

    assert first["cache_hit"] is False and second["cache_hit"] is False


async def test_done_payload_keeps_its_existing_fields(make_client, cache_settings, generator):
    """캐시 필드를 더하면서 기존 필드의 의미를 바꾸지 않는다 (design 결정 11)."""
    async with make_client(settings=cache_settings, generator=generator) as client:
        await ingest_over_http(client, "policy.txt", POLICY)

        done = await ask_over_http(client)

    assert set(done) == {
        "finish_reason",
        "answer",
        "citations",
        "dropped_markers",
        "elapsed_ms",
        "cache_hit",
        "cache_layer",
        "cache_similarity",
    }
