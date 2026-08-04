"""무효화 — 문서가 바뀌면 무엇이 사라지고 무엇이 남는가.

**핵심 3경로 중 하나다.** 여기서 깨지는 것은 성능이 아니라 정확성이다: 문서를 고쳤는데
옛 답변이 계속 나가면, 사용자는 서버가 자기 문서를 안 읽었다고 믿게 된다.

긍정과 부정의 **비대칭**이 이 파일의 절반이다.

| 종료 | 그 답변이 주장하는 것 | 무엇이 뒤집나 |
|------|---------------------|--------------|
| `stop` | **이 청크들이** 답을 담고 있다 | 그 청크가 바뀌는 것 |
| `no_evidence`·`insufficient_evidence` | **어디에도** 답이 없다 | 코퍼스에 내용이 더해지는 것 |

부정 판정을 만들려면 근거가 0건이어야 한다. 페이크 임베더의 점수에는 의미가 없어 하한을
`1.0` 으로 올려 그 상태를 만든다 — 실제로 `0.82` 하한이 하는 일과 같다.

부정 판정은 "없음에 대한 주장"이라 자기가 본 문서만 봐서는 반증되지 않는다 — 관계없어
보이는 문서 하나가 그 주장을 깨는데, 그 문서에는 이 항목을 가리키는 태그가 없다. 그래서
문서 태그와 별개로 부정 집합이라는 축이 하나 더 있고, **같은 사건이 긍정은 살리고 부정은
지운다**(6.7 ↔ 6.11 한 쌍이 그것을 고정한다).

수집·삭제가 캐시를 걷어내는지는 **다음 질문이 미스가 되는가**로 잰다. 캐시 내부를 들여다보면
"지웠다"만 확인되고 "지운 것이 옳은 항목인가"는 확인되지 않는다.
"""

import pytest

from app.core.answers import FinishReason
from app.core.exceptions import UnsupportedDocumentFormat
from tests.qa_harness import (
    QUESTION,
    VERDICT_ANSWERABLE,
    VERDICT_INSUFFICIENT,
    done_of,
    make_qa_harness,
)
from tests.retrieval_harness import GUIDE, POLICY
from tests.stubs import GenerationTurn, StubResponseCache

ANSWER = "교육비는 연 200만원까지 지원됩니다 [1]."
OTHER_ANSWER = "코드 리뷰는 1명 이상의 승인이 필요합니다 [1]."

#: 교체용 본문. 원본과 내용이 달라야 리비전이 갈린다.
REVISED_POLICY = POLICY.replace("연 200만원", "연 300만원")


def answering(text: str = ANSWER) -> GenerationTurn:
    return GenerationTurn(chunks=(VERDICT_ANSWERABLE, text))


def refusing() -> GenerationTurn:
    return GenerationTurn(chunks=(VERDICT_INSUFFICIENT, "제공된 근거로는 답할 수 없습니다."))


def cached(*turns, **kwargs) -> object:
    """캐시를 켠 하네스. 수집과 답변이 같은 캐시 계층을 본다."""
    return make_qa_harness(*(turns or (answering(),)), cached=True, **kwargs)


def nothing_relevant(*turns) -> object:
    """어떤 질문도 근거를 얻지 못하는 하네스 — 부정 판정을 만드는 유일한 방법이다."""
    return cached(*turns, min_score=1.0)


async def hit(harness, question: str = QUESTION) -> bool:
    return done_of(await harness.ask(question)).cache.hit


# ── 문서를 인용한 답변 ───────────────────────────────────────────────────


async def test_reuploading_invalidates_answers_that_cited_the_document():
    """재업로드가 그 문서를 인용한 답변을 무효화한다 (`document-ingestion`)."""
    harness = cached(answering(), answering())
    await harness.ingest("policy.txt", POLICY)
    await harness.ask()
    assert await hit(harness), "재업로드 전에는 히트여야 비교가 성립한다"

    await harness.ingest("policy.txt", REVISED_POLICY)

    assert not await hit(harness)
    assert harness.generator.calls == 2


async def test_deleting_invalidates_answers_that_cited_the_document():
    """삭제된 문서를 인용한 답변이 계속 나가면 지운 문장이 출처로 떠 있게 된다."""
    harness = cached()
    document = await harness.retrieval.ingest("policy.txt", POLICY)
    await harness.ask()

    await harness.retrieval.ingestion.delete(document.document_id)

    events = await harness.ask()
    assert not done_of(events).cache.hit
    assert done_of(events).answer.finish_reason is FinishReason.NO_EVIDENCE


async def test_an_unrelated_document_change_leaves_the_answer_alone():
    """관계없는 문서의 변경이 캐시를 지우면 업로드 한 번이 캐시 전체 비우기가 된다.

    6.11 과 짝이다 — **같은 사건**(문서 B 수집)이 긍정은 살리고 부정은 지운다 (결정 4)."""
    harness = cached()
    # 문서 하나만 올린 채 캐시한다 — 둘을 올리면 근거가 섞여 "A 만 인용한 답변"이 안 된다.
    await harness.ingest("policy.txt", POLICY)
    await harness.ask(QUESTION)

    guide = await harness.retrieval.ingest("guide.md", GUIDE)

    assert await hit(harness, QUESTION), "새 문서 수집이 긍정 답변을 지우면 안 된다"

    await harness.retrieval.ingestion.delete(guide.document_id)

    assert await hit(harness, QUESTION), "관계없는 문서의 삭제도 마찬가지다"
    assert harness.generator.calls == 1


# ── 아무것도 쓰지 않은 요청 ──────────────────────────────────────────────


async def test_an_unchanged_reupload_does_not_touch_the_cache():
    """바뀐 것이 없으면 캐시된 답변도 여전히 옳다 (design 결정 8)."""
    harness = cached()
    await harness.ingest("policy.txt", POLICY)
    await harness.ask()
    harness.cache.calls.clear()

    await harness.ingest("policy.txt", POLICY)

    assert "invalidate_document" not in harness.cache.calls
    assert await hit(harness)


async def test_a_rejected_upload_does_not_touch_the_cache():
    """변경이 실패해 롤백되면 캐시도 그대로여야 한다 (`response-cache`)."""
    harness = cached()
    await harness.ingest("policy.txt", POLICY)
    await harness.ask()
    harness.cache.calls.clear()

    with pytest.raises(UnsupportedDocumentFormat):
        await harness.retrieval.ingestion.ingest("policy.hwp", POLICY.encode("utf-8"))

    assert harness.cache.calls == []
    assert await hit(harness)


# ── 부정 판정은 코퍼스 내용이 뒤집는다 ──────────────────────────────────


async def test_ingesting_a_new_document_invalidates_no_evidence():
    """새 문서 수집이 근거 없음 판정을 무효화한다 (`document-ingestion`).

    그 항목에는 참조한 문서가 없어 문서 태그도 재검증도 닿지 못한다 — 부정 집합뿐이다."""
    harness = cached()
    assert done_of(await harness.ask()).answer.finish_reason is FinishReason.NO_EVIDENCE
    assert await hit(harness), "무효화 전에는 근거 없음도 히트다"

    await harness.ingest("policy.txt", POLICY)

    assert not await hit(harness)
    assert harness.generator.calls == 1, "미스 뒤 근거가 생겨 생성기가 처음 불린다"


async def test_replacing_a_document_invalidates_no_evidence():
    """문서를 교체해도 근거 없음 판정이 사라진다 — **문서 수가 그대로인 것이 함정이다.**

    무효화의 조건이 개수였다면 여기서만 조용히 틀린다."""
    harness = nothing_relevant()
    await harness.ingest("guide.md", GUIDE)
    assert done_of(await harness.ask()).answer.finish_reason is FinishReason.NO_EVIDENCE
    assert await hit(harness)

    await harness.ingest("guide.md", GUIDE + "\n환불은 30일 이내에 가능합니다.\n")

    assert not await hit(harness)


async def test_a_new_document_overturns_insufficient_evidence():
    """새 문서가 "답할 수 없음" 판정을 뒤집는다 (`response-cache`).

    판정을 뒤집는 문서는 그 항목이 인용한 문서와 **다른 문서**라 태그가 닿지 않는다."""
    harness = cached(refusing(), answering())
    await harness.ingest("policy.txt", POLICY)
    events = await harness.ask()
    assert done_of(events).answer.finish_reason is FinishReason.INSUFFICIENT_EVIDENCE
    assert await hit(harness)

    await harness.ingest("guide.md", GUIDE)  # 문서 A 는 건드리지 않는다

    assert not await hit(harness)
    assert harness.generator.calls == 2


async def test_recovering_a_stale_document_invalidates_no_evidence():
    """`STALE` 로 검색에서 빠진 문서가 **같은 내용으로** 복구되는 경로다.

    색인 서명이 그대로라 캐시 키가 살아 있는 유일한 재색인 경로이고, `REINDEXED` 를
    무효화 대상에서 빠뜨리면 여기서만 낫지 않는다 (design 결정 8)."""
    harness = cached()
    document = await harness.retrieval.ingest("policy.txt", POLICY)

    # 검색 대상에서 빼 `no_evidence` 를 만든다 — `stale` 은 서명을 그대로 둔다.
    from dataclasses import replace as replace_field

    from app.core.documents import IndexStatus

    await harness.retrieval.registry.commit(
        replace_field(document, chunk_count=0, index_status=IndexStatus.STALE)
    )
    assert done_of(await harness.ask()).answer.finish_reason is FinishReason.NO_EVIDENCE
    assert await hit(harness)

    await harness.ingest("policy.txt", POLICY)  # 같은 내용의 재수집 → REINDEXED

    assert not await hit(harness)


async def test_an_unchanged_reupload_does_not_clear_the_negative_set():
    """아무것도 저장하지 않고 끝난 수집은 이 판정을 무효화하지 않는다 (`response-cache`).

    항목에 태그가 없어(근거 0건) 여기서 사라진다면 그것은 부정 집합을 비웠다는 뜻뿐이다."""
    harness = nothing_relevant()
    await harness.ingest("policy.txt", POLICY)
    assert done_of(await harness.ask()).answer.finish_reason is FinishReason.NO_EVIDENCE
    assert await hit(harness)

    await harness.ingest("policy.txt", POLICY)  # 같은 바이트 → UNCHANGED

    assert await hit(harness)


async def test_deleting_does_not_clear_the_negative_set():
    """문서 삭제는 이 판정을 무효화하지 않아도 된다 — 지워서 없던 답이 생기지는 않는다."""
    harness = nothing_relevant()
    document = await harness.retrieval.ingest("guide.md", GUIDE)
    assert done_of(await harness.ask()).answer.finish_reason is FinishReason.NO_EVIDENCE
    assert await hit(harness)

    await harness.retrieval.ingestion.delete(document.document_id)

    assert await hit(harness)


# ── 캐시가 죽어 있어도 ───────────────────────────────────────────────────


async def test_ingestion_succeeds_when_the_cache_store_is_dead():
    """무효화 실패가 문서 변경 요청을 실패로 만들어서는 안 된다 (`response-cache`).

    문서는 이미 바뀌었고 그것을 되돌리는 쪽이 더 큰 손해다."""
    harness = make_qa_harness(answering(), cache=StubResponseCache(fail=True))

    result = await harness.retrieval.ingestion.ingest("policy.txt", POLICY.encode("utf-8"))

    assert result.document.chunk_count > 0
    assert "invalidate_document" in harness.cache.calls, "시도는 했다"


async def test_deletion_succeeds_when_the_cache_store_is_dead():
    harness = make_qa_harness(answering(), cache=StubResponseCache(fail=True))
    document = await harness.retrieval.ingest("policy.txt", POLICY)

    deleted = await harness.retrieval.ingestion.delete(document.document_id)

    assert deleted.document_id == document.document_id


async def test_a_failed_invalidation_is_logged(caplog):
    """그 사실은 관측 가능해야 한다 (`response-cache`)."""
    harness = make_qa_harness(answering(), cache=StubResponseCache(fail=True))

    with caplog.at_level("WARNING", logger="app.services.cache"):
        await harness.retrieval.ingest("policy.txt", POLICY)

    assert any("무효화" in record.message for record in caplog.records)
