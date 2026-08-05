"""골든셋 생성 채점 — 두 구성의 답변을 세 층으로 채점해 나란히 찍는다.

품질을 단언하지 않는다. 회귀는 결정적인 층(순위 지표·기존 검색 테스트)이 지고, 여기서
나오는 수치는 `ARCHITECTURE.md` 실측표로 간다 (design 결정 13).
"""

import asyncio
from pathlib import Path

import pytest

from app.adapters.embedding import SentenceTransformerEmbedder
from app.adapters.lexical.sqlite import SqliteLexicalIndex
from app.adapters.llm import AppServerSession, CodexAnswerGenerator, SessionLaunch, SessionPool
from app.adapters.reranking import CrossEncoderReranker
from app.config import Settings, llm_environment
from app.core.answers import FinishReason
from app.services.qa import DoneEvent
from tests.conftest import (
    EMBEDDING_MODEL,
    RERANKER_MODEL,
    needs_credentials,
    needs_reranker_weights,
    needs_weights,
)
from tests.golden_harness import SOURCE_PDF, load_items, load_unanswerable, rank_of, spans_hold
from tests.llm_judge import (
    NO_EVIDENCE_TEXT,
    GenerationScores,
    Graded,
    Judgement,
    JudgeVerdict,
    judge,
)
from tests.qa_harness import make_qa_harness, sources_of
from tests.retrieval_harness import make_harness

#: 문항 하나가 구성마다 실물 턴 둘(생성·판정)을 쓴다. 한 바퀴가 수십 분이라 기본 실행에서
#: 빠지고, 구독이 필요해 `llm` 마커도 함께 붙는다.
pytestmark = [
    pytest.mark.llm,
    pytest.mark.slow,
    needs_weights,
    needs_reranker_weights,
    needs_credentials,
]

SHIPPED_FLOOR = Settings.model_fields["retrieval_min_score"].default
CHUNK_SIZE = Settings.model_fields["chunk_size"].default
CHUNK_OVERLAP = Settings.model_fields["chunk_overlap"].default

#: 배포 기본 K 그대로 잰다. 순위 층과 달리 여기서 재는 것은 실제로 프롬프트에 실리는 근거다.
MEASURED_K = Settings.model_fields["retrieval_top_k"].default

#: 실측 1.14초(design 결정 11)보다 크게 잡는다. 축소되면 융합 결과를 리랭킹 결과로 읽는다.
GENEROUS_RERANK_TIMEOUT = 120.0

#: 실물 턴 하나가 5~17초라 상한을 그 위로 둔다 (`test_llm_live`).
TURN_TIMEOUT = 180.0

CONFIGS = ("fusion", "rerank")


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """보험 요약집 PDF 한 건을 배포 기본 청크 구성으로 색인한 하네스."""
    built = make_harness(
        embedder=SentenceTransformerEmbedder(EMBEDDING_MODEL),
        lexical_index=SqliteLexicalIndex(tmp_path_factory.mktemp("lexical") / "index.sqlite3"),
        size=CHUNK_SIZE,
        overlap=CHUNK_OVERLAP,
        top_k=MEASURED_K,
        min_score=SHIPPED_FLOOR,
        retrievers=("dense", "lexical"),
        required=("dense",),
        rerank_timeout_seconds=GENEROUS_RERANK_TIMEOUT,
    )
    asyncio.run(built.ingestion.ingest(SOURCE_PDF.name, SOURCE_PDF.read_bytes()))
    return built


@pytest.fixture(scope="module")
def comparison(harness, tmp_path_factory):
    """두 구성의 채점표. 실물 세션이 한 이벤트 루프 안에 살아야 해 한 번에 돈다."""
    workspace = tmp_path_factory.mktemp("judge-workspace")
    measured = asyncio.run(_compare(harness, workspace))
    _report(measured)
    return measured


async def _compare(harness, workspace: Path) -> dict:
    """게이트를 세우고 두 구성을 채점한다. 세션 풀은 이 함수가 열고 닫는다."""
    reranker = CrossEncoderReranker(RERANKER_MODEL)
    gated = await _gate(harness, reranker)
    unanswerable = load_unanswerable()

    launch = SessionLaunch(env=llm_environment(), cwd=workspace, startup_timeout_seconds=60.0)
    pool = SessionPool(lambda: AppServerSession.start(launch), size=1)
    try:
        generator = CodexAnswerGenerator(pool, workspace=workspace)
        measured = {"gated": gated, "unanswerable": unanswerable}
        for config in CONFIGS:
            asking = make_qa_harness(
                retrieval=harness,
                generator=generator,
                reranker=reranker if config == "rerank" else None,
                top_k=MEASURED_K,
                timeout_seconds=TURN_TIMEOUT,
            )
            measured[config] = await _grade(asking, generator, gated, unanswerable, config)
    finally:
        await pool.aclose()
    return measured


async def _gate(harness, reranker) -> list:
    """두 구성 모두에서 근거가 상위 K 에 온 문항만 남긴다.

    교집합으로 좁히면 판정 통과 수의 차이가 상위 K 안의 순서에만 귀속된다."""
    fused = harness.searching_with(top_k=MEASURED_K)
    reranked = harness.searching_with(top_k=MEASURED_K, reranker=reranker)
    gated = []
    for item in load_items():
        before = await fused.search(item.question)
        after = await reranked.search(item.question)
        assert after.reranker, f"{item.id}: 리랭킹이 축소됐다"
        if rank_of(before.chunks, item.quote) and rank_of(after.chunks, item.quote):
            gated.append(item)
    return gated


async def _grade(asking, generator, gated, unanswerable, config: str) -> GenerationScores:
    """게이트를 통과한 문항과 근거 없는 문항을 한 구성에서 채점한다."""
    rows = []
    for item in list(gated) + list(unanswerable):
        answer, ran_reranker = await _answer(asking, item.question)
        if config == "rerank":
            assert ran_reranker, f"{item.id}: 답변 경로에서 리랭킹이 축소됐다"
        if answer is None:
            rows.append(_failed_generation(item))
            continue
        rows.append(
            Graded(
                id=item.id,
                probe=item.probe,
                retrieved=True,
                spans_ok=spans_hold(item, answer),
                judgement=await judge(generator, item, answer, timeout_seconds=TURN_TIMEOUT),
            )
        )
    return GenerationScores(tuple(rows))


async def _answer(asking, question: str) -> tuple[str | None, bool]:
    """질문 하나를 끝까지 읽어 채점할 본문과 리랭킹 여부로.

    조각이 아니라 종료 이벤트를 읽는다 — 근거 0건은 생성기를 부르지 않고 끝난다."""
    events = await asking.ask(question)
    reranked = bool(sources_of(events).reranker)
    answer = next((event.answer for event in events if isinstance(event, DoneEvent)), None)
    if answer is None:
        return None, reranked
    if answer.finish_reason is FinishReason.NO_EVIDENCE:
        return NO_EVIDENCE_TEXT, reranked
    return answer.text, reranked


def _failed_generation(item) -> Graded:
    """생성이 실패한 문항. 오답이 아니라 판정불가로 든다 — 판정자가 본 적이 없다."""
    return Graded(
        id=item.id,
        probe=item.probe,
        retrieved=True,
        spans_ok=None,
        judgement=Judgement(JudgeVerdict.UNJUDGED, "생성 실패 — 답변이 비었다"),
    )


def _report(measured: dict) -> None:
    """`pytest -s` 로 돌렸을 때 비교표를 그대로 읽을 수 있게 찍는다."""
    gated, unanswerable = measured["gated"], measured["unanswerable"]
    print(
        f"\n골든셋 생성 채점 · 게이트 통과 {len(gated)}문항 + 근거없음 {len(unanswerable)}문항"
        f" · 상위 {MEASURED_K}"
    )
    print(f"{'':13} {'일치':>6} {'불일치':>7} {'판정불가':>9} {'문자열':>7}")
    for config in CONFIGS:
        scores = measured[config]
        print(
            f"{'':13} {scores.counting(JudgeVerdict.MATCH):6}"
            f" {scores.counting(JudgeVerdict.MISMATCH):7}"
            f" {scores.counting(JudgeVerdict.UNJUDGED):9}"
            f" {scores.spans_passed:7}   ← {config}"
        )
        _report_disagreements(scores)


def _report_disagreements(scores: GenerationScores) -> None:
    """문자열 검사와 판정이 엇갈린 문항. 판정자를 의심할 자리가 여기다."""
    for row in scores.disagreements:
        print(f"{'':15} {row.id} {row.probe:32} 문자열={row.spans_ok} 판정={row.verdict}")


def test_the_gate_leaves_items_to_grade(comparison):
    """교집합이 비면 아래 수치는 전부 0 이 되고, 그 0 은 품질이 아니라 게이트의 값이다."""
    gated = comparison["gated"]

    assert gated, "두 구성 모두에서 근거가 온 문항이 없다 — 채점할 것이 없다"


def test_the_judge_settles_most_of_what_it_is_asked(comparison):
    """판정불가가 과반이면 그 회차의 통과 수는 읽을 수 없다 (design 결정 13)."""
    for config in CONFIGS:
        scores = comparison[config]
        unjudged = scores.counting(JudgeVerdict.UNJUDGED)

        assert unjudged * 2 <= len(scores.graded), (
            f"{config}: 판정불가 {unjudged}/{len(scores.graded)} — 통과 수를 읽지 않는다"
        )


def test_both_configurations_graded_the_same_items(comparison):
    """두 구성이 다른 문항을 풀었으면 통과 수의 차이가 순서 때문인지 알 수 없다."""
    graded = {config: [row.id for row in comparison[config].graded] for config in CONFIGS}

    assert graded["fusion"] == graded["rerank"]
