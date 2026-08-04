"""임베딩 어댑터 — 로딩 없이 서명을 읽는가, 역할 접두사가 새지 않는가.

실제 모델을 쓰는 단언은 파일 끝의 통합 테스트 하나뿐이고 가중치가 없으면 스킵한다.
나머지는 페이크로 돈다 — `pytest` 한 줄이 네트워크에 묶이지 않게.
"""

import asyncio
import time

import pytest

from app.adapters.embedding import (
    KNOWN_MODEL_PROFILES,
    PREFIX_CONVENTION,
    SentenceTransformerEmbedder,
)
from app.adapters.protocols import Embedder
from app.core.exceptions import ConfigurationError
from tests.conftest import EMBEDDING_MODEL as DEFAULT_MODEL
from tests.conftest import needs_weights
from tests.stubs import FakeEmbedder

KOREAN = "교육비는 연 200만원까지 지원합니다. 신청은 인사팀에 합니다."


# ── 프로토콜 준수 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "embedder",
    [FakeEmbedder(), SentenceTransformerEmbedder(DEFAULT_MODEL)],
    ids=["fake", "sentence-transformers"],
)
def test_implementations_satisfy_the_protocol(embedder):
    """페이크가 프로토콜을 벗어나면 페이크로 통과한 테스트가 실물에서 깨진다."""
    assert isinstance(embedder, Embedder)


# ── 서명 — 색인 서명의 재료 ──────────────────────────────────────────────


def test_the_signature_is_readable_without_loading_the_model():
    """서명은 수집이 시작되기 전에 필요하다. 읽는 일이 로딩을 유발하면 지연 초기화가
    무의미해진다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)

    assert embedder.signature
    assert embedder.dimension == 384
    assert embedder.max_input_tokens == 512
    assert embedder._model is None, "속성을 읽었을 뿐인데 모델이 올라왔다"


def test_the_signature_carries_the_four_facts_that_decide_the_vector():
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)

    assert embedder.signature == f"{DEFAULT_MODEL}/384/l2norm/{PREFIX_CONVENTION}"


def test_the_signature_keeps_the_organization_prefix():
    """조직 접두사를 떼면 다른 조직의 동명 모델이 같은 서명을 받는다."""
    assert SentenceTransformerEmbedder(DEFAULT_MODEL).signature.startswith("intfloat/")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (
            SentenceTransformerEmbedder("intfloat/multilingual-e5-small"),
            SentenceTransformerEmbedder("intfloat/multilingual-e5-base"),
        ),
        (
            SentenceTransformerEmbedder(DEFAULT_MODEL, normalize=True),
            SentenceTransformerEmbedder(DEFAULT_MODEL, normalize=False),
        ),
    ],
    ids=["model", "normalization"],
)
def test_a_different_configuration_gets_a_different_signature(left, right):
    """서명이 같으면 재색인이 일어나지 않는다 — 서명이 막으려던 실패가 서명을 통해 돌아온다."""
    assert left.signature != right.signature


def test_an_unknown_model_stops_startup():
    """차원을 추측해 서명에 넣으면 서명이 거짓이 되고, 재색인 강제라는 목적이 사라진다."""
    with pytest.raises(ConfigurationError) as caught:
        SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2")

    # 받아들여지는 값 목록이 메시지에 있어야 고칠 수 있다.
    for known in KNOWN_MODEL_PROFILES:
        assert known in caught.value.message


# ── 페이크 임베더 ────────────────────────────────────────────────────────


async def test_the_fake_is_deterministic_across_instances():
    """수집 테스트가 재실행마다 다른 벡터를 만들면 리비전 교체 단언이 흔들린다."""
    first = await FakeEmbedder().embed_documents([KOREAN])
    again = await FakeEmbedder().embed_documents([KOREAN])

    assert first == again


async def test_the_fake_separates_different_texts():
    vectors = await FakeEmbedder().embed_documents([KOREAN, "다른 내용입니다."])

    assert vectors[0] != vectors[1]


@pytest.mark.parametrize("dimension", [8, 384, 768])
async def test_the_fake_honours_the_declared_dimension(dimension):
    """모델 교체 상황을 실제 모델 없이 재현하는 수단이다."""
    embedder = FakeEmbedder(dimension=dimension)

    vectors = await embedder.embed_documents([KOREAN])

    assert len(vectors[0]) == dimension


async def test_the_fake_records_batches():
    """배치 경계와 중복 인코딩을 뒤 단계 테스트가 확인할 수 있어야 한다."""
    embedder = FakeEmbedder()

    await embedder.embed_documents(["가", "나"])
    await embedder.embed_documents(["다"])

    assert embedder.batches == [["가", "나"], ["다"]]


def test_the_fakes_token_count_is_tunable():
    """토큰 가드가 걸리는 상황을 실제 토크나이저 없이 만들 수 있어야 한다."""
    text = "가" * 100

    assert FakeEmbedder(chars_per_token=2).count_document_tokens(text) == 50
    assert FakeEmbedder(chars_per_token=1).count_document_tokens(text) == 100


def test_the_fake_counts_both_roles_the_same():
    """페이크에는 역할 접두사가 없으므로 흉내 낼 차이도 없다.

    인위적인 차이를 만들면 검증 대상이 페이크가 되고 실물은 미확인으로 남는다."""
    embedder = FakeEmbedder(chars_per_token=2)

    assert embedder.count_query_tokens(KOREAN) == embedder.count_document_tokens(KOREAN)


async def test_embedding_nothing_calls_nothing():
    assert await FakeEmbedder().embed_documents([]) == []


# ── 오프로드 ─────────────────────────────────────────────────────────────


async def test_encoding_does_not_block_the_event_loop():
    """인코딩은 CPU 바운드다 — 루프에서 그냥 돌면 문서 하나가 서비스 전체를 멈춘다.

    실물 모델은 빨라서 오히려 이 회귀를 드러내지 못해 느린 인코더를 끼운다."""
    delay = 0.2
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)
    embedder._model = _SlowModel(delay)  # 로딩을 건너뛴다 — 여기서 볼 것은 인코딩이다
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(delay / 20)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        vectors = await embedder.embed_documents([KOREAN, "다른 내용입니다."])
    finally:
        ticker.cancel()

    assert len(vectors) == 2
    assert ticks > 1, "인코딩 동안 이벤트 루프가 멈췄다"


class _SlowModel:
    """`encode` 만 있는 느린 모델 대역. 지연은 블로킹이다."""

    def __init__(self, delay: float = 0.0) -> None:
        self._delay = delay
        self.encoded: list[list[str]] = []

    def encode(self, texts, **_kwargs):
        self.encoded.append(list(texts))
        if self._delay:
            time.sleep(self._delay)
        return [[0.1] * 384 for _ in texts]


# ── 토큰 계산 ────────────────────────────────────────────────────────────


class _RecordingTokenizer:
    """인코딩된 문자열을 그대로 남기는 토크나이저 대역. 토큰 하나가 문자 하나다."""

    def __init__(self) -> None:
        self.encoded: list[str] = []

    def encode(self, text: str) -> list[str]:
        self.encoded.append(text)
        return list(text)


class _TokenizerOnlyModel:
    """토크나이저만 있는 모델 대역 — 가중치 없이 계산 경로만 본다."""

    def __init__(self) -> None:
        self.tokenizer = _RecordingTokenizer()


def test_each_count_encodes_its_own_role_prefix():
    """세는 문자열이 실제로 인코딩되는 문자열과 어긋나면 가드가 거짓말을 한다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)
    embedder._model = _TokenizerOnlyModel()

    embedder.count_document_tokens(KOREAN)
    embedder.count_query_tokens(KOREAN)

    assert embedder._model.tokenizer.encoded == [
        "passage: " + KOREAN,
        "query: " + KOREAN,
    ]


def test_the_two_counts_differ_by_the_prefix_length():
    """역할마다 인코딩되는 문자열이 다르므로 계산도 갈려야 한다.

    뭉치면 한쪽이 틀리고, 그 틀림은 상한 바로 아래 입력이 잘리는 방식으로만 드러난다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)
    embedder._model = _TokenizerOnlyModel()

    document = embedder.count_document_tokens(KOREAN)
    query = embedder.count_query_tokens(KOREAN)

    assert document - query == len("passage: ") - len("query: ")


# ── 선로딩 ───────────────────────────────────────────────────────────────


async def test_warming_up_loads_the_model_and_encodes_once():
    """로딩만 하면 첫 `encode` 의 초기화 비용이 그대로 남는다.

    실제로 한 번 돌리는 김에 "이 모델로 벡터가 나오는가"까지 기동 시점에 확인된다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)
    loaded = _SlowModel()
    embedder._load = lambda: loaded  # 가중치 없이 로딩 경로만 대신한다

    await embedder.warm_up()

    assert embedder._model is loaded, "선로딩이 모델을 올리지 않았다"
    assert len(loaded.encoded) == 1, "선로딩이 인코딩까지 하지 않았다"
    assert loaded.encoded[0][0].startswith("passage: "), "역할 접두사 없이 워밍업했다"


async def test_warming_up_does_not_swallow_its_failure():
    """실패를 여기서 삼키면 무엇이 준비되지 않았는지가 사라진다.

    계속 뜰지 말지는 호출자(앱 팩토리)가 정한다 — 어댑터는 사실만 올린다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)

    def explode():
        raise ConfigurationError("가중치를 찾지 못했습니다")

    embedder._load = explode

    with pytest.raises(ConfigurationError):
        await embedder.warm_up()


async def test_the_first_encode_still_loads_when_warm_up_never_ran():
    """지연 로딩은 선로딩이 생겨도 남는다 — 선로딩 실패의 백스톱이다.

    걷어내면 일시적 원인에도 재시작 전까지 수집이 죽는다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)
    loaded = _SlowModel()
    embedder._load = lambda: loaded

    vectors = await embedder.embed_documents([KOREAN])  # warm_up 없이 바로 인코딩

    assert embedder._model is loaded
    assert len(vectors) == 1


async def test_warming_up_twice_loads_once():
    """기동 훅이 두 번 불리거나 첫 요청이 곧바로 이어져도 모델은 한 벌이어야 한다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)
    loads = 0

    def load():
        nonlocal loads
        loads += 1
        return _SlowModel()

    embedder._load = load

    await embedder.warm_up()
    await embedder.warm_up()
    await embedder.embed_documents([KOREAN])

    assert loads == 1, "모델을 두 벌 올렸다 — 메모리가 두 배가 된다"


# ── 실물 모델 (가중치가 로컬에 있을 때만) ────────────────────────────────
# 스킵 조건은 `conftest.py` 에 있다 — 두 벌로 두면 한쪽만 고쳐진 채 안 돌 수 있다.


@pytest.fixture(scope="module")
def real_embedder():
    """가중치 로딩이 이 파일에서 가장 비싼 일이라 한 번만 한다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)
    embedder._ensure_model()
    return embedder


@needs_weights
async def test_the_real_model_matches_what_the_signature_declares(real_embedder):
    """선언이 틀리면 서명이 거짓이 되고 토큰 가드가 상한을 넘겨 통과시킨다.

    페이크만 검증하면 이 어긋남이 배포까지 살아남는다."""
    embedder = real_embedder

    vectors = await embedder.embed_documents([KOREAN])

    assert len(vectors[0]) == embedder.dimension
    assert f"/{embedder.dimension}/" in embedder.signature
    assert embedder._model.max_seq_length >= embedder.max_input_tokens


@needs_weights
async def test_the_real_model_actually_gets_the_role_prefix(real_embedder):
    """접두사를 어댑터 안에 가둔 결과, 밖에서는 붙었는지 확인할 길이 이것뿐이다."""
    raw = real_embedder._model  # 어댑터를 거치지 않은 같은 모델
    expected = raw.encode(["passage: " + KOREAN], normalize_embeddings=True)[0].tolist()

    vectors = await real_embedder.embed_documents([KOREAN])

    assert vectors[0] == pytest.approx(expected, abs=1e-6)


@needs_weights
async def test_documents_and_queries_land_in_different_places(real_embedder):
    """같은 문자열이라도 역할이 다르면 다른 벡터여야 한다 — 규약이 실제로 적용된 증거다."""
    document = (await real_embedder.embed_documents([KOREAN]))[0]
    query = await real_embedder.embed_query(KOREAN)

    assert document != pytest.approx(query, abs=1e-6)


@needs_weights
def test_the_real_token_count_includes_the_prefix(real_embedder):
    """본문만 세면 접두사 몫만큼 과소 계산되어 상한 바로 아래 청크가 조용히 잘린다."""
    with_prefix = real_embedder.count_document_tokens(KOREAN)
    bare = len(real_embedder._model.tokenizer.encode(KOREAN, add_special_tokens=False))

    assert with_prefix > bare


@needs_weights
def test_the_real_query_count_matches_what_the_model_actually_encodes(real_embedder):
    """이 일치가 깨지면 가드가 통과시킨 질의가 잘린다.

    특수 토큰(`<s>`·`</s>`)까지 포함해야 성립한다 — 둘이 빠지면 계산이 2 적어진다."""
    counted = real_embedder.count_query_tokens(KOREAN)
    fed = real_embedder._model.tokenize(["query: " + KOREAN])

    assert counted == int(fed["attention_mask"][0].sum())
    assert counted > len(
        real_embedder._model.tokenizer.encode("query: " + KOREAN, add_special_tokens=False)
    )


@needs_weights
async def test_the_real_model_warms_up():
    """대역이 아니라 실물 가중치로 선로딩 경로가 끝까지 도는지 확인한다.

    기동 시점에 도는 경로라, 여기서 깨지면 컨테이너가 매번 경고를 남기며 뜬다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)

    await embedder.warm_up()

    assert embedder._model is not None
    assert len(await embedder.embed_query("교육비")) == embedder.dimension
