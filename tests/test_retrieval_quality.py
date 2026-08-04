"""검색 품질 — 임베딩 실물로. 여기서만 임베딩이 의미를 잡는지가 검증된다.

나머지 검색 테스트는 해시 벡터로 구조를 재는데, 그 층은 1위가 무엇이든 정상으로 본다.
저장소는 스텁이고 문서는 `sample-docs/` 실물이다 (근거는 `tests/README.md`).
"""

import asyncio
from pathlib import Path

import pytest

from app.adapters.embedding import SentenceTransformerEmbedder
from app.adapters.lexical.sqlite import SqliteLexicalIndex
from app.config import Settings
from tests.conftest import EMBEDDING_MODEL, needs_weights
from tests.retrieval_harness import make_harness

pytestmark = needs_weights

SAMPLE_DOCS = Path(__file__).resolve().parents[1] / "sample-docs"
POLICY = "company-policy.txt"
GUIDE = "development-guide.md"

#: 배포되는 밀집 하한. 여러 테스트가 "그 값에서 무엇이 살아남는가"를 묻는다.
SHIPPED_FLOOR = Settings.model_fields["retrieval_min_score"].default

#: `(질의, 기대 문서, 1위 본문에 있어야 할 값)`. 파일명만 보면 "정책 문서 어딘가"까지라,
#: 답이 실린 그 절이 왔는지를 본문 조각으로 함께 못 박는다.
RELEVANT = [
    ("교육비는 얼마까지 지원되나요?", POLICY, "200만원"),
    ("재택근무는 일주일에 몇 번 할 수 있나요?", POLICY, "주 2회"),
    ("PR을 머지하려면 승인이 몇 명 필요한가요?", GUIDE, "최소 1명"),
    ("P1 장애가 나면 몇 분 안에 원인을 파악해야 하나요?", GUIDE, "30분"),
]

#: 두 문서 어디에도 근거가 없는 질의.
UNRELATED = "오늘 서울 날씨 어때?"

#: `(문서에 그대로 적힌 식별자, 그것이 있는 파일)`. 문맥 없이 이것만 묻는 질의가
#: 하이브리드가 구제하는 자리다 — 배포 하한에서 밀집 단독은 셋 다 근거 0건이다.
IDENTIFIERS = [("P3", GUIDE), ("hotfix", GUIDE), ("develop", GUIDE)]

#: 기본값(600자)이면 두 문서가 청크 넷이 되어 상위 5가 전부를 반환한다 — 포함 단언이
#: 무조건 참이 되므로 절 단위로 쪼갠다.
CHUNK_SIZE = 200
CHUNK_OVERLAP = 40


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """실물 임베딩과 실물 어휘 색인을 꽂은 하네스. 문서는 `sample-docs/` 그대로다.

    모듈 스코프인 이유와 청크 크기를 배포 기본값으로 두는 이유는 `tests/README.md` 에 있다."""
    built = make_harness(
        embedder=SentenceTransformerEmbedder(EMBEDDING_MODEL),
        lexical_index=SqliteLexicalIndex(tmp_path_factory.mktemp("lexical") / "index.sqlite3"),
        size=CHUNK_SIZE,
        overlap=CHUNK_OVERLAP,
        min_score=0.0,
        retrievers=("dense", "lexical"),
        required=("dense",),
    )

    async def index() -> None:
        for filename in (POLICY, GUIDE):
            await built.ingest(filename, (SAMPLE_DOCS / filename).read_text(encoding="utf-8"))

    asyncio.run(index())
    return built


def test_both_sample_documents_split_into_several_chunks(harness):
    """전제 확인 — 청크가 문서당 하나면 순위 단언이 "문서 둘 중 하나 고르기"로 퇴화한다.

    이게 깨지면 아래 실패들은 검색 품질이 아니라 청킹 구성의 문제다."""
    indexed = harness.registry.documents
    per_document = [len(harness.store.chunks_of(document_id)) for document_id in indexed]

    assert {document.filename for document in indexed.values()} == {POLICY, GUIDE}
    assert all(count >= 3 for count in per_document), f"청크가 너무 적다 — {per_document}"


@pytest.mark.parametrize(
    ("query", "expected_file", "expected_text"),
    RELEVANT,
    ids=["교육비", "재택근무", "코드리뷰", "장애대응"],
)
async def test_a_query_gets_its_own_document_first(harness, query, expected_file, expected_text):
    """하이브리드 구성에서도 질의가 자기 주제의 청크를 1위로 받는다 — 융합의 회귀 기준.

    샘플이 작아 "포함되는가"로 단언하면 검색이 망가져도 통과한다."""
    results = (await harness.retrieval.search(query)).chunks

    assert results, "실물 임베딩으로 근거가 하나도 나오지 않았다"
    top = results[0]
    assert top.filename == expected_file, (
        f"1위가 {top.filename} 이다 — 기대는 {expected_file}\n"
        f"상위: {[(chunk.filename, round(chunk.score, 3)) for chunk in results]}"
    )
    assert expected_text in top.text, f"1위 청크에 {expected_text!r} 이 없다 — {top.text!r}"


async def test_an_unrelated_query_scores_below_every_relevant_one(harness):
    """무관 질의의 최고 코사인이 관련 질의들의 최저 최고점보다 낮아야 한다.

    융합 점수가 아니라 밀집 원점수를 본다 — 점수 상수를 박으면 모델이 아니라 그 상수를 재게 된다."""
    relevant_tops = [await _top_score(harness, query) for query, _, _ in RELEVANT]
    unrelated_top = await _top_score(harness, UNRELATED)

    assert all(score is not None for score in relevant_tops)
    assert unrelated_top is None or unrelated_top < min(relevant_tops), (
        f"무관 질의가 {unrelated_top} 점을 받았다 — 관련 질의 최저 최고점 {min(relevant_tops)}"
    )


async def test_the_shipped_similarity_floor_actually_drops_the_unrelated_query(harness):
    """배포 기본값으로도 무관 질의가 근거를 얻지 못하는지 — 계측값과 실제 설정의 대조다.

    문서에 적힌 근거와 코드의 기본값이 어긋나면 그것은 허위 기재다."""
    searching = harness.searching_with(min_score=SHIPPED_FLOOR)

    unrelated = await searching.search(UNRELATED)
    relevant = [await searching.search(query) for query, _, _ in RELEVANT]

    assert unrelated.count == 0, f"하한 {SHIPPED_FLOOR} 에서도 무관 질의가 근거를 얻었다"
    assert all(result.count for result in relevant), (
        f"하한 {SHIPPED_FLOOR} 이 관련 질의의 근거까지 지웠다 — {[r.count for r in relevant]}"
    )


@pytest.mark.parametrize(("identifier", "expected_file"), IDENTIFIERS)
async def test_a_bare_identifier_is_found_only_when_the_lexical_retriever_is_on(
    harness, identifier, expected_file
):
    """문맥 없는 식별자 질의 — 배포 하한에서 밀집 단독은 근거 0건, 하이브리드는 찾는다.

    실측 코사인이 셋 다 하한 아래라, 문자열이 그대로 있는데도 빈 목록이 된다."""
    hybrid = harness.searching_with(min_score=SHIPPED_FLOOR)
    dense_only = harness.searching_with(min_score=SHIPPED_FLOOR, retrievers=("dense",))

    alone = await dense_only.search(identifier)
    together = await hybrid.search(identifier)

    assert alone.count == 0, (
        f"밀집 단독이 {identifier!r} 를 찾았다 — 이 테스트가 구제 사례로 삼은 전제가 "
        f"깨졌으므로 임베딩 모델이나 하한 {SHIPPED_FLOOR} 을 다시 재야 한다"
    )
    assert together.count, f"하이브리드도 {identifier!r} 를 못 찾았다"
    top = together.chunks[0]
    assert top.filename == expected_file
    assert identifier in top.text, f"1위 청크에 {identifier!r} 이 없다 — {top.text!r}"
    assert any(credit.retriever == "lexical" for credit in top.contributions), (
        f"어휘 retriever 가 {identifier!r} 의 1위에 기여하지 않았다 — {top.contributions}"
    )


async def test_the_same_query_twice_gives_the_same_ranking(harness):
    """결정성은 다음 change 의 캐싱이 성립하기 위한 전제다."""
    query = RELEVANT[0][0]

    first = await harness.retrieval.search(query)
    again = await harness.retrieval.search(query)

    assert [(chunk.document_id, chunk.chunk_index, chunk.score) for chunk in first.chunks] == [
        (chunk.document_id, chunk.chunk_index, chunk.score) for chunk in again.chunks
    ]


async def _top_score(harness, query: str) -> float | None:
    """1위 청크에 밀집 retriever 가 매긴 코사인. 근거가 없으면 `None`."""
    chunks = (await harness.retrieval.search(query)).chunks
    if not chunks:
        return None
    return next(
        credit.native_score
        for credit in chunks[0].contributions
        if credit.retriever == "dense"
    )
