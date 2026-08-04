"""`POST /search` — 응답 봉투·출처·요청 거부·저장소 장애.

유사도 하한을 `0.0` 으로 낮춘 클라이언트를 쓴다. 해시 벡터의 점수에는 의미가 없어,
봉투를 재는 테스트가 운영 기본값에 걸려 빈 결과를 받으면 검증이 아니라 잡음이 된다.
"""

import pytest

from app.config import RetrieverSettings
from tests.api_harness import LONG_KOREAN, upload
from tests.pdf_fixtures import make_pdf
from tests.stubs import FakeEmbedder, StubLexicalIndex, StubVectorStore

DATA = LONG_KOREAN.encode("utf-8")

#: 평범한 한국어 질문. 문자 수 상한(기본 1000자) 안쪽이라, 이것이 거부되면 가드가 정상
#: 입력을 막고 있다는 뜻이다.
NORMAL_QUERY = "교육비는 얼마까지 지원되나요?"


@pytest.fixture
def searchable_settings(settings):
    """하한만 낮춘 설정. 나머지는 기본 픽스처 그대로다."""
    return settings.model_copy(update={"retrieval_min_score": 0.0})


@pytest.fixture
async def client(make_client, searchable_settings):
    async with make_client(settings=searchable_settings) as c:
        yield c


async def ingest(client, filename: str = "policy.txt", data: bytes = DATA) -> dict:
    """업로드하고 본문을 돌려준다. 실패는 여기서 드러난다 — 뒤 단언이 엉뚱하게 깨지지 않게."""
    response = await client.post("/documents", **upload(filename, data))
    assert response.status_code in (200, 201), response.text
    return response.json()


async def search(client, query: str = NORMAL_QUERY, **body) -> tuple[int, dict]:
    response = await client.post("/search", json={"query": query, **body})
    return response.status_code, response.json()


# ── 성공 경로 (6.6) ──────────────────────────────────────────────────────


async def test_a_query_answers_with_the_four_field_envelope(client):
    await ingest(client)

    status, body = await search(client)

    assert status == 200
    assert set(body) == {"query", "top_k", "results", "count", "retrievers"}
    assert body["query"] == NORMAL_QUERY
    assert body["count"] == len(body["results"])
    assert body["results"], "수집된 문서가 있는데 근거가 하나도 나오지 않았다"


async def test_the_response_carries_no_answer_field(client):
    """검색은 답변을 만들지 않는다 — 거절 문구도 답변 필드도 응답에 없다."""
    await ingest(client)

    _, body = await search(client)

    assert "answer" not in body


async def test_fusion_scores_land_in_the_unit_interval_in_descending_order(client):
    """점수는 클수록 상위이고 `0` 이 나올 수 없다.

    `0` 은 어느 목록에도 없는 항목이라는 뜻이라, 정규화 분모가 입력 집합과 어긋난 신호다."""
    await ingest(client)

    _, body = await search(client)

    scores = [result["score"] for result in body["results"]]
    assert len(scores) > 1, "정렬을 재려면 결과가 둘 이상이어야 한다"
    assert all(0 < score <= 1 for score in scores)
    assert scores == sorted(scores, reverse=True)


async def test_a_requested_top_k_beats_the_configured_default(client, searchable_settings):
    await ingest(client)

    _, body = await search(client, top_k=2)

    assert searchable_settings.retrieval_top_k > 2, "기본값과 같으면 덮어썼는지 알 수 없다"
    assert body["top_k"] == 2
    assert len(body["results"]) <= 2


async def test_nothing_collected_is_an_empty_result_not_an_error(client, vector_store):
    status, body = await search(client)

    assert status == 200
    assert body == {
        "query": NORMAL_QUERY,
        "top_k": 5,
        "results": [],
        "count": 0,
        # 아무도 돌지 않았으니 기여 목록도 비어 있다 — 실패와 구별되는 것은 상태 코드다.
        "retrievers": [],
    }
    assert vector_store.queries == [], "대상이 없는데 저장소에 질의가 갔다"


# ── 기여 내역 (7.3) ──────────────────────────────────────────────────────
# 없으면 어휘 색인이 빠진 배포와 정상 배포를 HTTP 로 구별할 수 없다 — 둘 다 `200` 이다.


async def test_both_retrievers_appear_in_the_contributing_list(client):
    await ingest(client)

    _, body = await search(client)

    assert set(body["retrievers"]) == {"dense", "lexical"}


async def test_every_result_says_who_put_it_there(client):
    """내역이 비면 그 결과는 어느 목록에도 없던 것이 된다 — 융합이 만들 수 없는 상태다."""
    await ingest(client)

    _, body = await search(client)

    assert body["results"]
    for result in body["results"]:
        contributions = result["contributions"]
        assert contributions, f"기여 내역이 빈 결과가 있다 — {result['chunk_index']}"
        assert {item["retriever"] for item in contributions} <= set(body["retrievers"])
        for item in contributions:
            assert item["rank"] >= 1
            assert isinstance(item["native_score"], float)


async def test_a_failed_optional_retriever_never_claims_a_contribution(
    make_client, searchable_settings
):
    """선택 retriever 의 실패는 `200` 으로 넘어가되 이름이 빠진 것으로 드러난다.

    조용히 넘기면 하이브리드가 꺼진 것을 아무도 모르고, 세우면 가용성이 떨어진다."""
    broken = StubLexicalIndex(fail_search=True)

    async with make_client(settings=searchable_settings, lexical_index=broken) as client:
        await ingest(client)
        status, body = await search(client)

    assert status == 200
    assert body["results"], "밀집 retriever 는 살아 있는데 결과가 비었다"
    assert body["retrievers"] == ["dense"]
    assert all(
        item["retriever"] != "lexical"
        for result in body["results"]
        for item in result["contributions"]
    )


async def test_turning_the_lexical_retriever_off_removes_it_from_both_lists(
    make_client, searchable_settings
):
    """코드를 바꾸지 않고 설정만으로 조합이 바뀐다 — 그 사실이 응답에 드러난다."""
    dense_only = searchable_settings.model_copy(
        update={"retrievers": [RetrieverSettings(name="dense", required=True)]}
    )

    async with make_client(settings=dense_only) as client:
        await ingest(client)
        status, body = await search(client)

    assert status == 200
    assert body["retrievers"] == ["dense"]
    assert all(
        item["retriever"] == "dense"
        for result in body["results"]
        for item in result["contributions"]
    )


# ── 출처 (6.7) ───────────────────────────────────────────────────────────


async def test_a_text_result_points_at_a_span_of_the_original(client):
    """오프셋이 원문의 어느 구간인지 말해야 한다 — 본문이 그 구간과 같은지로 확인한다."""
    await ingest(client)

    _, body = await search(client)

    original = DATA.decode("utf-8")
    for result in body["results"]:
        assert result["page"] is None, "텍스트 문서에 쪽 번호가 붙었다"
        assert result["char_start"] < result["char_end"]
        assert result["text"] == original[result["char_start"] : result["char_end"]]


async def test_a_pdf_result_points_at_a_page(client):
    pages = ["교육비는 연 200만원까지 지원합니다.", "재택근무는 주 2회까지 가능합니다."]
    await ingest(client, "policy.pdf", make_pdf(pages))

    _, body = await search(client)

    assert body["results"]
    for result in body["results"]:
        assert 1 <= result["page"] <= len(pages)


async def test_a_result_agrees_with_the_document_detail(client):
    created = await ingest(client)

    _, body = await search(client)
    detail = (await client.get(f"/documents/{created['document_id']}")).json()

    top = body["results"][0]
    assert top["document_id"] == detail["document_id"]
    assert top["filename"] == detail["filename"]
    assert top["format"] == detail["format"]
    assert top["revision"] == detail["revision"]
    assert 0 <= top["chunk_index"] < detail["chunk_count"]


# ── 요청 거부 (6.8) ──────────────────────────────────────────────────────
# 거부가 임베딩도 질의도 유발하지 않는 것은 호출 부재로만 확인되어 두 기록을 함께 본다.


def assert_nothing_was_computed(embedder: FakeEmbedder, store: StubVectorStore) -> None:
    assert embedder.queries == [], "거부된 질의가 임베딩되었다"
    assert store.queries == [], "거부된 질의가 저장소에 갔다"


@pytest.mark.parametrize("query", ["", "   \n\t  "])
async def test_a_query_without_content_is_rejected(client, embedder, vector_store, query):
    """공백만 있는 질의는 길이 검증에 걸리지 않는다 — 그래서 도메인 코드로 구분한다."""
    status, body = await search(client, query)

    assert status == 422
    assert body["error"]["code"] == "empty_query"
    assert_nothing_was_computed(embedder, vector_store)


async def test_a_query_over_the_character_ceiling_is_rejected(
    client, embedder, vector_store, searchable_settings
):
    """두 상한이 모두 응답에 있다. 어느 쪽에 걸렸든 같은 모양이라야 파서가 하나다."""
    ceiling = searchable_settings.retrieval_max_query_chars

    status, body = await search(client, "가" * (ceiling + 1))

    assert status == 422
    assert body["error"]["code"] == "query_too_long"
    assert body["error"]["max_query_chars"] == ceiling
    assert body["error"]["max_query_tokens"] == embedder.max_input_tokens
    assert_nothing_was_computed(embedder, vector_store)


async def test_a_query_within_the_characters_but_over_the_input_window_is_rejected(
    make_client, searchable_settings, vector_store
):
    """조용한 절단을 막는 진짜 가드다.

    자르고 검색하면 뒷부분이 반영되지 않는데 사용자는 전부 반영됐다고 믿는다."""
    narrow = FakeEmbedder(max_input_tokens=10, chars_per_token=2)

    async with make_client(settings=searchable_settings, embedder=narrow) as client:
        query = "가" * 100  # 50 토큰 — 문자 상한(1000)은 통과하고 입력 창(10)은 넘는다
        assert len(query) <= searchable_settings.retrieval_max_query_chars
        status, body = await search(client, query)

    assert status == 422
    assert body["error"]["code"] == "query_too_long"
    # 토큰 쪽에 걸렸어도 두 상한이 다 있다 — 걸린 쪽만 실으면 같은 코드에 항목이
    # 둘인 응답이 되어 소비자가 분기를 들어야 한다.
    assert body["error"]["max_query_tokens"] == narrow.max_input_tokens
    assert body["error"]["max_query_chars"] == searchable_settings.retrieval_max_query_chars
    assert_nothing_was_computed(narrow, vector_store)


async def test_an_ordinary_question_is_not_rejected_by_the_token_guard(client):
    """가드가 정상 질문을 거부하면 상한이 아니라 서비스가 고장 난 것이다."""
    await ingest(client)

    status, _ = await search(client, NORMAL_QUERY)

    assert status == 200


@pytest.mark.parametrize("top_k", [0, -1])
async def test_a_top_k_below_one_is_a_framework_validation_error(
    client, embedder, vector_store, top_k
):
    """하한은 어느 배포에서나 `1` 이라 요청 스키마에 속한다 — 프레임워크가 끝낸다.

    고정해 두지 않으면 검증이 핸들러 안으로 옮겨졌을 때 조용히 깨진다."""
    status, body = await search(client, top_k=top_k)

    assert status == 422
    assert body["error"]["code"] == "validation_error"
    assert_nothing_was_computed(embedder, vector_store)


async def test_a_top_k_over_the_ceiling_exposes_the_applied_ceiling(
    client, embedder, vector_store, searchable_settings
):
    """상한은 배포 설정이라 응답이 알려 주지 않으면 클라이언트는 이분 탐색해야 한다."""
    ceiling = searchable_settings.retrieval_max_top_k

    status, body = await search(client, top_k=ceiling + 1)

    assert status == 422
    assert body["error"]["code"] == "invalid_top_k"
    assert body["error"]["max_top_k"] == ceiling
    assert_nothing_was_computed(embedder, vector_store)


# ── 저장소 장애 (6.9) ────────────────────────────────────────────────────


async def test_a_store_failure_is_a_503_and_never_an_empty_result(make_client, searchable_settings):
    """빈 결과와 저장소 장애는 다음 단계가 해야 할 일이 다르다.

    뭉개면 저장소가 죽은 동안 "근거를 찾지 못했습니다"가 나가고 아무도 눈치채지 못한다."""
    broken = StubVectorStore(fail_query=True)

    async with make_client(settings=searchable_settings, vector_store=broken) as client:
        await ingest(client)  # 쓰기는 되고 질의만 실패한다
        status, body = await search(client)

    assert status == 503
    assert body["error"]["code"] == "storage_unavailable"
    assert "results" not in body, "저장소 장애가 빈 결과로 위장되었다"
    # 봉투에 `code` 와 `message` 밖에 없다. 메시지도 어댑터가 자기 경계에서 우리 문구로
    # 바꿔 던진 것이라 라이브러리 사정이 여기까지 오지 않는다.
    assert set(body["error"]) == {"code", "message"}
    assert "Traceback" not in str(body)
