"""검색 품질 — 임베딩 실물로.

**이 파일이 이 change 의 존재 이유다.** 나머지 검색 테스트는 페이크 임베더로 구조(필터·
순서·경계·오류 경로)를 재는데, 그 층은 "저장은 됐는데 검색이 엉뚱한 문서를 가져온다"를
끝까지 드러내지 못한다 — 해시 벡터에는 의미가 없으므로 1위가 무엇이든 정상으로 보인다.
여기서만 임베딩이 **의미를 잡는지**가 검증된다.

두 겹 중 품질 층이다.

| 층 | 검증 대상 | 임베더 | 저장소 | 실행 조건 |
|----|----------|--------|--------|----------|
| 구조 | 필터·순서·경계·오류 경로 | 결정론적 페이크 | 스텁 | 항상 |
| 품질 | 기대 문서가 1위인가 | **로컬 실물** | 스텁 | 가중치가 있을 때 |

**저장소는 실물이 아니라 스텁이다.** 검증 대상이 "임베딩이 의미를 잡는가"이지 "Chroma 가
HNSW 를 제대로 도는가"가 아니다. 스텁이 완전 탐색 코사인을 계산하면 근사 검색의 흔들림이
없어져 임베딩의 성질만 남는다. 실물 저장소와의 계약(필터 표현식·메타데이터 왕복)은
`test_vector_store.py` 가 따로 덮는다.

**문서는 `sample-docs/` 의 실제 파일이다.** 테스트 안에 문자열로 베껴 두면 리포의 문서가
바뀌어도 여기는 통과하고, 그 순간 평가자가 보는 문서와 검증되는 문서가 달라진다.

어휘 색인만은 실물 SQLite 다. 검증 대상이 BM25 순위와 희소도 하한 자체라, 대역이
토큰 교집합으로 흉내 내면 재는 것이 대역이 된다(`StubLexicalIndex` 의 docstring 이 같은
이유를 적고 있다). 벡터 쪽과 방향이 반대인 이유는 무엇을 재는지가 반대이기 때문이다 —
저장소의 근사 검색은 잡음이지만, 어휘 순위는 검증 대상 그 자체다.

가중치가 로컬에 없으면 파일 전체를 건너뛴다 — `pytest` 한 줄이 수백 MB 다운로드에 묶이면
안 된다. 컨테이너 이미지에는 가중치가 구워져 있어 그 안에서는 이 층까지 돈다.
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

#: `(질의, 기대 문서, 1위 본문에 있어야 할 값)`. spec 의 네 시나리오 그대로다.
#: 파일명만 보면 "정책 문서 어딘가"까지밖에 확인되지 않으므로, 답이 실린 **그 절**이
#: 왔는지를 본문 조각으로 함께 못 박는다.
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

#: **기본값(600자)보다 작게 잡는다.** 기본값으로는 두 문서가 청크 넷이 되어 상위 5가 전부를
#: 반환한다 — "기대한 문서가 결과에 포함되는가"가 무조건 참이 되어 아무것도 검증하지 못한다.
#: 절 단위로 쪼개져야 순위 단언이 실제로 구속력을 갖는다.
CHUNK_SIZE = 200
CHUNK_OVERLAP = 40


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """`sample-docs/` 두 문서를 실물 임베딩·실물 FTS5 로 색인한 하이브리드 하네스.

    모듈 스코프인 이유는 가중치 로딩과 색인이 이 파일에서 가장 비싼 일이기 때문이다.
    검색은 저장소를 바꾸지 않으므로 테스트끼리 상태를 공유해도 서로를 오염시키지 않는다.

    **동기 픽스처 안에서 `asyncio.run` 을 쓴다.** 색인은 async 경로지만 대역은 루프에
    묶인 자원을 들지 않으므로, 테스트마다 새 루프에서 검색해도 문제가 없다. 모듈 스코프
    async 픽스처를 만들려고 루프 스코프까지 손대는 것보다 이쪽이 싸다.

    구성은 배포 기본값과 같은 밀집+어휘다. 회귀 질의를 밀집 단독으로만 재면 융합이
    기존에 맞히던 것을 틀려도 이 파일이 통과한다. 밀집 단독은 비교가 필요한 자리에서
    `searching_with(retrievers=("dense",))` 로 같은 저장소 위에 따로 세운다.

    하한은 `0.0` 이다 — 순위를 재는 자리라 하한이 결과를 지우면 무엇이 1위였는지가
    관측되지 않는다. 배포 기본값이 실제로 어떻게 동작하는지는 아래에서 따로 확인한다.
    """
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

    이게 깨지면 아래 실패들은 검색 품질이 아니라 청킹 구성의 문제다.
    """
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

    "포함되는가"가 아니라 1위로 단언하는 이유는 샘플 문서가 작아서다 — 상위 5면 저장된
    청크의 상당수가 그냥 따라 나오므로, 포함 단언은 검색이 완전히 망가져도 통과한다.
    """
    results = (await harness.retrieval.search(query)).chunks

    assert results, "실물 임베딩으로 근거가 하나도 나오지 않았다"
    top = results[0]
    assert top.filename == expected_file, (
        f"1위가 {top.filename} 이다 — 기대는 {expected_file}\n"
        f"상위: {[(chunk.filename, round(chunk.score, 3)) for chunk in results]}"
    )
    assert expected_text in top.text, f"1위 청크에 {expected_text!r} 이 없다 — {top.text!r}"


async def test_an_unrelated_query_scores_below_every_relevant_one(harness):
    """무관 질의의 최고 **코사인**이 관련 질의들의 최저 최고점보다 낮아야 한다.

    응답의 `score` 가 아니라 밀집 기여 내역의 원점수를 본다 — 융합 점수는 순위에서만
    결정되므로 어떤 질의를 넣어도 1위가 `1.0` 이고, 그 값으로는 두 분포가 갈라졌는지를
    물을 수 없다. 밀집 하한이 비교하는 값이 바로 이 원점수다.

    **절대값으로 단언하지 않는다.** 점수는 모델에 종속되므로 상수를 박으면 모델을 바꾼
    순간 이 테스트가 품질이 아니라 그 상수를 재게 된다.
    """
    relevant_tops = [await _top_score(harness, query) for query, _, _ in RELEVANT]
    unrelated_top = await _top_score(harness, UNRELATED)

    assert all(score is not None for score in relevant_tops)
    assert unrelated_top is None or unrelated_top < min(relevant_tops), (
        f"무관 질의가 {unrelated_top} 점을 받았다 — 관련 질의 최저 최고점 {min(relevant_tops)}"
    )


async def test_the_shipped_similarity_floor_actually_drops_the_unrelated_query(harness):
    """**배포 기본값**으로도 무관 질의가 근거를 얻지 못하는지 — 계측값과 실제 설정의 대조다.

    위 테스트는 두 분포가 갈라졌다는 상대 관계까지만 본다. 하한을 그 사이에 놓았다고
    `ARCHITECTURE.md` 에 적어 두었으므로, 실제로 배포되는 숫자가 그 자리에 있는지를
    여기서 확인한다 — 문서에 적힌 근거와 코드의 기본값이 어긋나면 그건 허위 기재다.
    """
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

    실측 코사인이 `P3` 0.7969 · `hotfix` 0.8001 · `develop` 0.8102 로 셋 다 하한 아래라,
    문서에 그 문자열이 그대로 있는데도 밀집 단독은 빈 목록을 낸다 (근거는 ARCHITECTURE).
    """
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
