"""유사 매치 임계값 — 실물로 잰 세 분포와, 임계값이 막지 못하는 것.

부정문 쌍(0.9066~0.9979)이 같은 뜻 쌍(0.8844~0.9752)과 겹쳐 임계값으로 갈리지 않는다.
그 한계를 단언으로 고정한다 — 수치와 근거는 `ARCHITECTURE.md` 「임계값 0.93」에 있다.
"""

import pytest

from app.adapters.embedding import SentenceTransformerEmbedder
from app.config import Settings
from app.core.cache import cosine_similarity, normalize_query
from tests.conftest import EMBEDDING_MODEL, needs_weights

pytestmark = needs_weights

#: 배포 기본값. 테스트가 자기 값을 쓰면 재는 대상이 설정이 아니라 테스트가 된다.
THRESHOLD = Settings.model_fields["cache_semantic_threshold"].default

#: 임계값을 넘는 쌍 — PRD 가 요구하는 "유사 질문 캐시"가 실제로 성립하는 자리.
SAME_MEANING = [
    ("교육비는 얼마까지 지원되나요?", "교육비 지원 한도가 얼마인가요?"),  # 0.9658
    ("재택근무는 주 며칠까지 가능한가요?", "일주일에 며칠이나 재택근무를 할 수 있나요?"),  # 0.9752
    ("교육비 신청은 어떻게 하나요?", "교육비를 신청하는 절차가 궁금합니다"),  # 0.9605
    ("교육비는 얼마까지 지원되나요?", "교육비 지원 상한액은?"),  # 0.9488
]

#: 같은 뜻이지만 표현 차이가 커 임계값에 걸리는 쌍. 미스는 "느린 정답"이라 허용되는
#: 실패이고, 그 사실을 목록으로 드러내 둔다.
SAME_MEANING_BELOW = [
    ("연차는 며칠 부여되나요?", "연차 휴가가 몇 일인지 알려주세요"),  # 0.9122
    # 0.8930
    ("코드 리뷰는 몇 명의 승인이 필요한가요?", "PR 을 머지하려면 승인이 몇 개 필요한가요?"),
    ("재택근무는 주 며칠까지 가능한가요?", "재택 가능 일수"),  # 0.8844
]

DIFFERENT = [
    ("교육비는 얼마까지 지원되나요?", "재택근무는 주 며칠까지 가능한가요?"),
    ("코드 리뷰는 몇 명의 승인이 필요한가요?", "연차는 며칠 부여되나요?"),
    ("배포는 어떻게 진행되나요?", "건강검진은 얼마나 자주 받나요?"),
    ("교육비 지원 대상이 무엇인가요?", "브랜치 전략이 어떻게 되나요?"),
    ("재택근무 신청 절차는?", "장애 발생 시 대응 절차는?"),
]

#: 묻는 바가 반대인데 임계값 위에 남는 쌍. 이것이 값의 한계다.
NEGATION = [
    ("재택근무가 가능한가요?", "재택근무가 불가능한 경우가 있나요?"),  # 0.9733
    ("교육비 지원을 받을 수 있나요?", "교육비 지원을 받을 수 없는 경우가 있나요?"),  # 0.9773
    ("코드 리뷰 없이 머지할 수 있나요?", "코드 리뷰 없이 머지할 수 없나요?"),  # 0.9979
]

#: 겹침 구간에 걸려 우연히 미스가 되는 부정문 쌍. 같은 구간에 정상 쌍(0.9122)도 있어
#: 이 우연을 규칙으로 읽으면 정상 쌍부터 잃는다.
NEGATION_IN_THE_OVERLAP = ("환불 정책이 어떻게 되나요?", "환불이 안 되는 경우가 있나요?")


@pytest.fixture(scope="module")
def embedder() -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder(EMBEDDING_MODEL)


async def similarity(embedder, left: str, right: str) -> float:
    """캐시가 쓰는 경로 그대로 — 정규화한 질의를 질의 역할로 임베딩한다."""
    return cosine_similarity(
        await embedder.embed_query(normalize_query(left)),
        await embedder.embed_query(normalize_query(right)),
    )


@pytest.mark.parametrize(("left", "right"), DIFFERENT)
async def test_different_questions_stay_below_the_threshold(embedder, left, right):
    """묻는 바가 다른 질문에 캐시된 답변이 나가면 캐시가 틀린 답을 내는 층이 된다."""
    assert await similarity(embedder, left, right) < THRESHOLD


@pytest.mark.parametrize(("left", "right"), SAME_MEANING)
async def test_paraphrases_reach_the_threshold(embedder, left, right):
    """PRD 가 요구하는 "유사 질문 캐시"가 실제로 성립하는지."""
    assert await similarity(embedder, left, right) >= THRESHOLD


@pytest.mark.parametrize(("left", "right"), SAME_MEANING_BELOW)
async def test_distant_paraphrases_are_missed_and_that_is_accepted(embedder, left, right):
    """표현 차이가 크면 미스다 — 미스는 "느린 정답"이라 허용되는 실패다.

    이 목록이 비면 임계값을 낮출 여지가 생겼다는 뜻이라, 그때 분포를 다시 잰다."""
    assert await similarity(embedder, left, right) < THRESHOLD


async def test_paraphrases_are_separable_from_different_questions(embedder):
    """두 분포 사이에 자리가 있어야 임계값이 뜻을 갖는다.

    기본값은 그 자리보다 위에 있다 — 오탐과 미탐의 비용이 비대칭이기 때문이다."""
    same = [
        await similarity(embedder, left, right)
        for left, right in SAME_MEANING + SAME_MEANING_BELOW
    ]
    different = [await similarity(embedder, left, right) for left, right in DIFFERENT]

    assert min(same) > max(different), "두 분포가 겹치면 임계값으로 가를 수 없다"
    assert THRESHOLD > max(different)


@pytest.mark.parametrize(("left", "right"), NEGATION)
async def test_negation_pairs_survive_the_threshold(embedder, left, right):
    """묻는 바가 반대인 쌍이 임계값 위에 남는다 — 값의 근거가 아니라 값의 한계다.

    깨지면 임베딩이 부정을 잡기 시작했다는 뜻이라 분포를 다시 재야 한다."""
    assert await similarity(embedder, left, right) >= THRESHOLD


async def test_a_negation_pair_can_fall_in_the_overlap_by_luck(embedder):
    """겹침 구간의 쌍은 걸러지기도 한다 — 규칙이 아니라 우연이다.

    같은 구간에 정상 쌍도 있어(0.9122), 이 우연을 노려 임계값을 내리면 그쪽부터 잃는다."""
    left, right = NEGATION_IN_THE_OVERLAP
    below_threshold = await similarity(embedder, left, right)
    overlap_neighbour = await similarity(embedder, *SAME_MEANING_BELOW[0])

    assert below_threshold < THRESHOLD
    assert abs(below_threshold - overlap_neighbour) < 0.05, "둘이 같은 구간에 있다"


async def test_negation_is_closer_than_real_paraphrases(embedder):
    """임계값을 올려 부정문을 떨어뜨리면 진짜 같은 질문이 먼저 떨어진다.

    이 부등식이 "임계값 하나로는 풀 수 없다"의 증명이다 — 두 분포가 뒤집혀 있다."""
    negation = [await similarity(embedder, left, right) for left, right in NEGATION]
    same = [await similarity(embedder, left, right) for left, right in SAME_MEANING]

    assert max(negation) > max(same)
