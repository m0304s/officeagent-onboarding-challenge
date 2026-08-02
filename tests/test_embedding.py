"""임베딩 어댑터.

여기서 고정하는 것은 두 가지다.

1. **모델을 로딩하지 않고도** 색인 서명의 재료(`signature`·`dimension`)를 읽을 수 있다.
   읽는 일이 로딩을 유발하면 지연 초기화가 무의미해진다.
2. 역할 접두사가 어댑터 밖으로 새지 않는다.

실제 모델을 쓰는 단언은 파일 끝의 통합 테스트 하나뿐이며, 가중치가 로컬에 없으면
스킵한다. 나머지는 전부 페이크로 돈다 — `pytest` 한 줄이 네트워크에 묶이면 안 된다.
"""

import pytest

from app.adapters.embedding import (
    KNOWN_MODEL_PROFILES,
    PREFIX_CONVENTION,
    SentenceTransformerEmbedder,
)
from app.adapters.protocols import Embedder
from app.core.exceptions import ConfigurationError
from tests.stubs import FakeEmbedder

DEFAULT_MODEL = "intfloat/multilingual-e5-small"
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
    """서명은 수집이 시작되기 전에 필요하다. 읽는 일이 로딩을 유발하면 안 된다."""
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
    """서명이 같으면 재색인이 일어나지 않는다 — 서명이 막으려던 바로 그 실패다."""
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

    assert FakeEmbedder(chars_per_token=2).count_tokens(text) == 50
    assert FakeEmbedder(chars_per_token=1).count_tokens(text) == 100


async def test_embedding_nothing_calls_nothing():
    assert await FakeEmbedder().embed_documents([]) == []


# ── 실물 모델 (가중치가 로컬에 있을 때만) ────────────────────────────────


def _weights_are_cached(model_name: str) -> bool:
    """네트워크를 건드리지 않고 캐시만 확인한다."""
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(model_name, local_files_only=True)
    except Exception:
        return False
    return True


needs_weights = pytest.mark.skipif(
    not _weights_are_cached(DEFAULT_MODEL),
    reason=f"{DEFAULT_MODEL} 가중치가 로컬에 없습니다 (컨테이너 이미지에는 구워져 있습니다)",
)


@pytest.fixture(scope="module")
def real_embedder():
    """가중치 로딩이 이 파일에서 가장 비싼 일이라 한 번만 한다."""
    embedder = SentenceTransformerEmbedder(DEFAULT_MODEL)
    embedder._ensure_model()
    return embedder


@needs_weights
async def test_the_real_model_matches_what_the_signature_declares(real_embedder):
    """선언이 틀리면 서명이 거짓이 되고 토큰 가드가 상한을 넘겨 통과시킨다.

    페이크만 검증하면 이 어긋남이 배포까지 살아남는다.
    """
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
    with_prefix = real_embedder.count_tokens(KOREAN)
    bare = len(real_embedder._model.tokenizer.encode(KOREAN))

    assert with_prefix > bare
