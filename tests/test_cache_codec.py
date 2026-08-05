"""캐시 페이로드 왕복 — 저장된 항목이 원래 응답을 그대로 복원하는가.

Redis 계약 테스트는 마커 뒤라 기본 실행에서 빠진다. 왕복까지 그 뒤에 두면 `done` 이
캐시를 거치며 달라지는 회귀를 아무도 잡지 않는다.
"""

import json

import pytest

from app.adapters.cache.codec import (
    PAYLOAD_VERSION,
    dumps_entry,
    loads_entry,
    pack_vector,
    unpack_vector,
)
from app.core.answers import Answer, Citation, FinishReason
from app.core.cache import CachedAnswer
from app.core.documents import ChunkLocation, DocumentFormat
from app.core.fusion import Contribution
from app.core.reranking import ORDERED_BY_FUSION, ORDERED_BY_RERANK
from app.core.retrieval import ScoredChunk


def chunk(**overrides) -> ScoredChunk:
    fields = {
        "document_id": "doc-1",
        "revision": "rev-1",
        "index_signature": "sig-1",
        "chunk_index": 3,
        "text": "교육비는 연 200만원까지 지원됩니다.",
        "location": ChunkLocation(char_start=120, char_end=540),
        "filename": "company-policy.txt",
        "format": DocumentFormat.TXT,
        "score": 0.8,
        "contributions": (
            Contribution(retriever="dense", rank=1, native_score=0.91),
            Contribution(retriever="lexical", rank=4, native_score=2.3),
        ),
    }
    return ScoredChunk(**{**fields, **overrides})


def roundtrip(entry: CachedAnswer) -> CachedAnswer:
    return loads_entry(dumps_entry(entry))


def test_answer_survives_the_roundtrip():
    """`done` 의 네 필드가 원래 생성 시점의 값과 같아야 한다 (`response-cache`)."""
    source = chunk()
    entry = CachedAnswer(
        answer=Answer(
            text="연 200만원입니다. [1]",
            finish_reason=FinishReason.STOP,
            citations=(Citation.of(1, source),),
            dropped_markers=2,
        ),
        top_k=5,
        target_documents=2,
        sources=(source,),
    )

    assert roundtrip(entry) == entry


def test_sources_survive_with_their_contributions():
    """기여 내역이 빠지면 히트의 `sources` 가 미스의 것과 달라진다."""
    entry = CachedAnswer(
        answer=Answer(text="본문", finish_reason=FinishReason.STOP),
        top_k=3,
        target_documents=1,
        sources=(chunk(), chunk(chunk_index=4, document_id="doc-2")),
    )

    restored = roundtrip(entry)

    assert restored.sources == entry.sources
    assert restored.sources[0].contributions[1].retriever == "lexical"


def test_the_ranking_signal_survives_the_roundtrip():
    """리랭킹 점수만 왕복하면 히트가 그 점수를 든 채 융합 순서라고 말하게 된다."""
    entry = CachedAnswer(
        answer=Answer(text="본문", finish_reason=FinishReason.STOP),
        top_k=5,
        target_documents=1,
        sources=(chunk(rerank_score=0.9826), chunk(chunk_index=4, rerank_score=0.0)),
        ordered_by=ORDERED_BY_RERANK,
        reranker="BAAI/bge-reranker-v2-m3",
    )

    restored = roundtrip(entry)

    assert restored == entry
    assert [source.rerank_score for source in restored.sources] == [0.9826, 0.0]


def test_an_entry_written_before_reranking_still_loads():
    """세대를 올리면 되살릴 수 있는 항목까지 미스가 된다 — 없는 값은 리랭킹이 돌지
    않았다는 뜻으로 읽는다."""
    entry = CachedAnswer(
        answer=Answer(text="본문", finish_reason=FinishReason.STOP),
        top_k=5,
        target_documents=1,
        sources=(chunk(),),
    )
    payload = json.loads(dumps_entry(entry))
    del payload["ordered_by"], payload["reranker"], payload["sources"][0]["rerank_score"]

    restored = loads_entry(json.dumps(payload).encode("utf-8"))

    assert restored.ordered_by == ORDERED_BY_FUSION
    assert restored.reranker is None
    assert restored.sources[0].rerank_score is None
    assert restored.answer == entry.answer


def test_pdf_page_survives():
    """쪽 번호가 빠지면 PDF 출처가 어디에서 왔는지를 잃는다."""
    entry = CachedAnswer(
        answer=Answer(text="본문", finish_reason=FinishReason.STOP),
        top_k=5,
        target_documents=1,
        sources=(
            chunk(
                format=DocumentFormat.PDF,
                filename="development-guide.pdf",
                location=ChunkLocation(char_start=0, char_end=80, page=7),
            ),
        ),
    )

    assert roundtrip(entry).sources[0].location.page == 7


def test_no_evidence_entry_survives_as_itself():
    """근거도 인용도 본문도 없는 종료가 왕복에서 `stop` 으로 둔갑하면 빈 화면이 정상
    답변으로 보인다."""
    entry = CachedAnswer(answer=Answer.no_evidence(), top_k=5, target_documents=0)

    restored = roundtrip(entry)

    assert restored == entry
    assert restored.answer.finish_reason is FinishReason.NO_EVIDENCE
    assert restored.sources == () and restored.answer.text == ""


def test_insufficient_evidence_keeps_its_sources_and_stays_uncited():
    """근거는 있고 인용은 없는 종료다 — 둘 중 하나만 복원하면 도메인 불변식이 깨진다."""
    entry = CachedAnswer(
        answer=Answer(
            text="문서에 답이 없습니다", finish_reason=FinishReason.INSUFFICIENT_EVIDENCE
        ),
        top_k=5,
        target_documents=1,
        sources=(chunk(),),
    )

    restored = roundtrip(entry)

    assert restored == entry
    assert restored.answer.citations == () and restored.sources


def test_document_ids_survive_for_tagging():
    """태그가 페이로드에서 유도되므로, 근거가 왕복에서 줄면 무효화 대상도 준다."""
    entry = CachedAnswer(
        answer=Answer(text="본문", finish_reason=FinishReason.STOP),
        top_k=5,
        target_documents=2,
        sources=(chunk(document_id="doc-a"), chunk(document_id="doc-b")),
    )

    assert roundtrip(entry).document_ids == ("doc-a", "doc-b")


def test_broken_domain_invariants_do_not_survive_decoding():
    """깨진 항목이 스트림으로 재생되느니 미스가 낫다 — 값 객체가 다시 검사한다."""
    entry = CachedAnswer(
        answer=Answer(text="본문", finish_reason=FinishReason.STOP),
        top_k=5,
        target_documents=1,
        sources=(chunk(),),
    )
    tampered = dumps_entry(entry).replace(
        b'"finish_reason":"stop"', b'"finish_reason":"no_evidence"'
    )

    with pytest.raises(ValueError):
        loads_entry(tampered)


def test_payload_from_another_generation_is_rejected():
    """세대가 다른 페이로드를 되살리면 옛 구조가 새 코드의 값인 척 나간다."""
    payload = dumps_entry(
        CachedAnswer(answer=Answer.no_evidence(), top_k=5, target_documents=0)
    ).replace(f'"version":{PAYLOAD_VERSION}'.encode(), b'"version":999')

    with pytest.raises(ValueError):
        loads_entry(payload)


def test_vector_roundtrip_keeps_direction():
    """float32 로 눕히므로 값은 근사지만 방향은 유지되어야 유사도가 뜻을 잃지 않는다."""
    values = [0.1234567, -0.5, 0.0, 1.0]

    restored = unpack_vector(pack_vector(values))

    assert len(restored) == len(values)
    for original, actual in zip(restored, values, strict=True):
        assert original == pytest.approx(actual, abs=1e-6)


def test_vector_bytes_are_compact():
    """벡터를 페이로드에서 뗀 이유가 스캔 비용이라, 차원당 4바이트를 고정한다."""
    assert len(pack_vector([0.0] * 384)) == 384 * 4


def test_truncated_vector_is_rejected():
    """잘린 바이트를 조용히 받으면 차원이 어긋난 채 유사도가 계산된다."""
    with pytest.raises(ValueError):
        unpack_vector(pack_vector([1.0, 2.0])[:-1])
