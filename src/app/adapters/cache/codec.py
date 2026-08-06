"""캐시 페이로드 직렬화 — 값 객체 ↔ JSON, 질의 벡터 ↔ float32 바이트.

저장소와 파일을 나눈 이유는 왕복 정확성이 Redis 없이 검증되어야 하기 때문이다.
Redis 계약 테스트는 `redis` 마커 뒤에 있어 기본 실행에서 빠진다.
"""

import json
import struct
from collections.abc import Sequence
from typing import Any

from app.core.answers import Answer, Citation, FinishReason
from app.core.cache import CachedAnswer
from app.core.documents import ChunkLocation, DocumentFormat
from app.core.fusion import Contribution
from app.core.reranking import ORDERED_BY_FUSION
from app.core.retrieval import ScoredChunk

#: 페이로드 구조가 바뀌면 올린다. 세대가 다른 항목은 되살리지 않고 미스로 떨어뜨린다 —
#: 키 접두사(`qa:v1`)를 올리는 것이 정식 절차이고, 이 값은 그것을 빠뜨렸을 때의 방어다.
PAYLOAD_VERSION = 1


def dumps_entry(entry: CachedAnswer) -> bytes:
    """캐시 항목 하나를 저장 가능한 바이트로."""
    payload = {
        "version": PAYLOAD_VERSION,
        "answer": {
            "text": entry.answer.text,
            "finish_reason": entry.answer.finish_reason.value,
            "dropped_markers": entry.answer.dropped_markers,
            "citations": [_dump_citation(citation) for citation in entry.answer.citations],
        },
        "top_k": entry.top_k,
        "target_documents": entry.target_documents,
        "sources": [_dump_source(chunk) for chunk in entry.sources],
        "ordered_by": entry.ordered_by,
        "reranker": entry.reranker,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def loads_entry(raw: bytes | str) -> CachedAnswer:
    """저장된 바이트를 캐시 항목으로. 형식이 어긋나면 `ValueError`.

    도메인 불변식은 값 객체가 다시 검사한다 — 깨진 항목이 스트림으로 재생되지 않는다."""
    payload = json.loads(raw)
    if payload.get("version") != PAYLOAD_VERSION:
        raise ValueError(f"알 수 없는 캐시 페이로드 세대: {payload.get('version')!r}")

    answer = payload["answer"]
    return CachedAnswer(
        answer=Answer(
            text=answer["text"],
            finish_reason=FinishReason(answer["finish_reason"]),
            citations=tuple(_load_citation(item) for item in answer["citations"]),
            dropped_markers=answer["dropped_markers"],
        ),
        top_k=payload["top_k"],
        target_documents=payload["target_documents"],
        sources=tuple(_load_source(item) for item in payload["sources"]),
        # 리랭킹이 붙기 전에 쓰인 항목에는 이 셋이 없다. 세대를 올려 버리면 되살릴 수
        # 있는 항목까지 미스가 되므로, 없는 값은 "리랭킹이 돌지 않았다"로 읽는다.
        ordered_by=payload.get("ordered_by", ORDERED_BY_FUSION),
        reranker=payload.get("reranker"),
    )


def pack_vector(values: Sequence[float]) -> bytes:
    """질의 벡터를 float32 로 눕힌다.

    페이로드에서 뗀 값이라 후보 스캔이 답변 본문까지 끌어오지 않는다 (design 결정 2)."""
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(raw: bytes) -> tuple[float, ...]:
    """float32 바이트를 벡터로. 길이가 4의 배수가 아니면 `ValueError`."""
    if len(raw) % 4:
        raise ValueError("float32 벡터가 아닌 바이트다")
    return struct.unpack(f"<{len(raw) // 4}f", raw)


def _dump_citation(citation: Citation) -> dict[str, Any]:
    return {
        "marker": citation.marker,
        "document_id": citation.document_id,
        "filename": citation.filename,
        "format": citation.format.value,
        "revision": citation.revision,
        "chunk_index": citation.chunk_index,
        "location": _dump_location(citation.location),
        "score": citation.score,
    }


def _load_citation(data: dict[str, Any]) -> Citation:
    return Citation(
        marker=data["marker"],
        document_id=data["document_id"],
        filename=data["filename"],
        format=DocumentFormat(data["format"]),
        revision=data["revision"],
        chunk_index=data["chunk_index"],
        location=_load_location(data["location"]),
        score=data["score"],
    )


def _dump_source(chunk: ScoredChunk) -> dict[str, Any]:
    return {
        "document_id": chunk.document_id,
        "revision": chunk.revision,
        "index_signature": chunk.index_signature,
        "chunk_index": chunk.chunk_index,
        "text": chunk.text,
        "location": _dump_location(chunk.location),
        "filename": chunk.filename,
        "format": chunk.format.value,
        "score": chunk.score,
        "rerank_score": chunk.rerank_score,
        # 기여 내역이 빠지면 히트의 `sources` 이벤트가 미스의 것과 달라진다.
        "contributions": [
            {
                "retriever": contribution.retriever,
                "rank": contribution.rank,
                "native_score": contribution.native_score,
            }
            for contribution in chunk.contributions
        ],
    }


def _load_source(data: dict[str, Any]) -> ScoredChunk:
    return ScoredChunk(
        document_id=data["document_id"],
        revision=data["revision"],
        index_signature=data["index_signature"],
        chunk_index=data["chunk_index"],
        text=data["text"],
        location=_load_location(data["location"]),
        filename=data["filename"],
        format=DocumentFormat(data["format"]),
        score=data["score"],
        rerank_score=data.get("rerank_score"),
        contributions=tuple(
            Contribution(
                retriever=item["retriever"],
                rank=item["rank"],
                native_score=item["native_score"],
            )
            for item in data["contributions"]
        ),
    )


def _dump_location(location: ChunkLocation) -> dict[str, Any]:
    return {
        "char_start": location.char_start,
        "char_end": location.char_end,
        "page": location.page,
    }


def _load_location(data: dict[str, Any]) -> ChunkLocation:
    return ChunkLocation(
        char_start=data["char_start"],
        char_end=data["char_end"],
        page=data["page"],
    )
