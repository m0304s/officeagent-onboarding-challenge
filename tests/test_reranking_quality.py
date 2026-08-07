"""리랭킹 품질 — 골든셋으로 켠 구성과 끈 구성을 같은 저장소 위에서 비교한다.

저장소를 새로 만들면 청크도 임베딩도 달라져 비교가 성립하지 않는다. 실물 가중치 둘이
필요해 없으면 통째로 건너뛴다. 재는 것과 재지 않는 것은 `tests/README.md` 에 있다.
"""

import asyncio

import pytest

from app.adapters.embedding import SentenceTransformerEmbedder
from app.adapters.lexical.sqlite import SqliteLexicalIndex
from app.adapters.reranking import CrossEncoderReranker
from app.config import Settings
from tests.conftest import EMBEDDING_MODEL, RERANKER_MODEL, needs_reranker_weights, needs_weights
from tests.golden_harness import SOURCE_PDF, Scores, load_items, rank_of
from tests.retrieval_harness import make_harness

#: 44문항을 두 하한 × 두 구성으로 돌아 CPU 에서 30분대다. 기본 실행에서 빠지는 이유는
#: 실물 CLI 층과 같다 (`tests/README.md`).
pytestmark = [pytest.mark.slow, needs_weights, needs_reranker_weights]

SHIPPED_FLOOR = Settings.model_fields["retrieval_min_score"].default
CHUNK_SIZE = Settings.model_fields["chunk_size"].default
CHUNK_OVERLAP = Settings.model_fields["chunk_overlap"].default

#: 순위 분포를 보려면 K 상한까지 받아야 한다. 리랭킹은 절단보다 앞이라 여기서 K 를 넓혀도
#: 상위 5의 순서는 배포 구성의 그것과 같다.
MEASURED_K = Settings.model_fields["retrieval_max_top_k"].default

#: 실측 1.14초(설계 결정 11)보다 훨씬 크게 잡는다. 축소되면 리랭킹이 돌지 않은 결과를
#: 리랭킹 결과로 읽게 되는데, 그 사고는 조용하다.
GENEROUS_TIMEOUT = 120.0


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """보험 요약집 PDF 한 건을 배포 기본 청크 구성으로 색인한 하네스."""
    built = make_harness(
        embedder=SentenceTransformerEmbedder(EMBEDDING_MODEL),
        lexical_index=SqliteLexicalIndex(tmp_path_factory.mktemp("lexical") / "index.sqlite3"),
        size=CHUNK_SIZE,
        overlap=CHUNK_OVERLAP,
        retrievers=("dense", "lexical"),
        required=("dense",),
        rerank_timeout_seconds=GENEROUS_TIMEOUT,
    )
    asyncio.run(built.ingestion.ingest(SOURCE_PDF.name, SOURCE_PDF.read_bytes()))
    return built


@pytest.fixture(scope="module")
def items():
    return load_items()


@pytest.fixture(scope="module")
def comparison(harness, items):
    """두 구성 × 두 하한의 순위표. 무거워서 모듈당 한 번만 돈다."""
    reranker = CrossEncoderReranker(RERANKER_MODEL)
    measured = {}
    for floor_name, floor in (("shipped", SHIPPED_FLOOR), ("open", 0.0)):
        for config in ("fusion", "rerank"):
            searching = harness.searching_with(
                top_k=MEASURED_K,
                min_score=floor,
                reranker=reranker if config == "rerank" else None,
            )
            measured[floor_name, config] = asyncio.run(_score(searching, items, config))
    _report(measured, items)
    return measured


async def _score(searching, items, config: str) -> Scores:
    """문항 전부를 한 구성에 통과시켜 근거 인용문의 순위를 걷는다."""
    ranks: dict[str, int | None] = {}
    for item in items:
        result = await searching.search(item.question)
        # 축소되면 융합 결과를 리랭킹 결과로 읽게 된다 — 그 사고는 조용하다. 다만 후보
        # 0건은 축소가 아니라 골든셋이 재려던 미스라, 여기서 끊으면 표가 통째로 사라진다.
        if config == "rerank" and result.chunks:
            assert result.reranker, f"{item.id}: 리랭킹이 축소됐다"
        ranks[item.id] = rank_of(result.chunks, item.quote)
    return Scores(ranks)


def _report(measured: dict, items) -> None:
    """`pytest -s` 로 돌렸을 때 비교표를 그대로 읽을 수 있게 찍는다."""
    print(f"\n골든셋 {len(items)}문항 · 상위 {MEASURED_K} · 청크 {CHUNK_SIZE}/{CHUNK_OVERLAP}")
    for floor_name in ("shipped", "open"):
        floor = SHIPPED_FLOOR if floor_name == "shipped" else 0.0
        print(f"\n[하한 {floor}]  {'':10} {'@1':>5} {'@3':>5} {'@5':>5} {'@20':>5} {'MRR':>7}")
        for config in ("fusion", "rerank"):
            scores = measured[floor_name, config]
            print(
                f"{'':13} {config:10}"
                f" {scores.recall_at(1):5} {scores.recall_at(3):5}"
                f" {scores.recall_at(5):5} {scores.recall_at(MEASURED_K):5}"
                f" {scores.mrr:7.3f}"
            )
        _report_moves(measured[floor_name, "fusion"], measured[floor_name, "rerank"], items)


def _report_moves(before: Scores, after: Scores, items) -> None:
    """순위가 움직인 문항을 probe 와 함께. 총계만 보면 어느 축이 움직였는지 알 수 없다."""
    for item in items:
        was, now = before.ranks[item.id], after.ranks[item.id]
        if was == now:
            continue
        print(f"{'':15} {item.id} {item.probe:32} {_rank(was)} → {_rank(now)}")


def _rank(rank: int | None) -> str:
    return "없음" if rank is None else f"{rank}위"


def test_the_document_splits_into_more_chunks_than_the_rerank_depth(harness):
    """전제 확인 — 청크가 리랭크 깊이보다 적으면 리랭커가 전부를 보게 되어
    "깊이 밖 후보" 경로가 이 측정에서 아예 돌지 않는다."""
    indexed = list(harness.registry.documents)
    chunks = sum(len(harness.store.chunks_of(document_id)) for document_id in indexed)

    assert chunks > MEASURED_K, f"청크가 {chunks}개뿐이라 순위 비교가 퇴화한다"


def test_reranking_does_not_drop_any_candidate(harness, items):
    """리랭킹은 걸러내지 않는다 — 결과 집합이 같고 순서만 다르다 (`retrieval` 스펙)."""
    reranker = CrossEncoderReranker(RERANKER_MODEL)
    fused = harness.searching_with(top_k=MEASURED_K, min_score=SHIPPED_FLOOR)
    reranked = harness.searching_with(
        top_k=MEASURED_K, min_score=SHIPPED_FLOOR, reranker=reranker
    )

    async def compare(question: str) -> tuple[set, set]:
        before = await fused.search(question)
        after = await reranked.search(question)
        return (
            {(chunk.document_id, chunk.chunk_index) for chunk in before.chunks},
            {(chunk.document_id, chunk.chunk_index) for chunk in after.chunks},
        )

    for item in items[:5]:
        before, after = asyncio.run(compare(item.question))
        assert before == after, f"{item.id}: 리랭킹이 결과 집합을 바꿨다"


def test_reranking_does_not_lose_evidence_that_fusion_found(comparison):
    """켠 구성이 끈 구성보다 근거를 적게 찾으면 얹는 단계가 아니라 빼는 단계다."""
    fusion = comparison["shipped", "fusion"]
    rerank = comparison["shipped", "rerank"]

    assert rerank.recall_at(MEASURED_K) == fusion.recall_at(MEASURED_K), (
        f"상위 {MEASURED_K} 안의 근거 수가 달라졌다 — "
        f"융합 {fusion.recall_at(MEASURED_K)}, 리랭킹 {rerank.recall_at(MEASURED_K)}"
    )


def test_reranking_does_not_regress_the_top_of_the_list(comparison):
    """1위 정확도가 떨어지면 이 단계는 순위를 섞고 있는 것이다 (design 결정 1의 실패 모양)."""
    fusion = comparison["shipped", "fusion"]
    rerank = comparison["shipped", "rerank"]

    assert rerank.recall_at(1) >= fusion.recall_at(1), (
        f"1위 근거가 줄었다 — 융합 {fusion.recall_at(1)}, 리랭킹 {rerank.recall_at(1)}"
    )
