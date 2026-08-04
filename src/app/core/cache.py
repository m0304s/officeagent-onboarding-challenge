"""캐시 도메인 — 질의 정규화, 항목 지문 유도, 유사 매치 판정.

I/O 가 없다. 임계값 판정까지 여기 두는 것은 어댑터가 정책을 갖지 않게 하려는 것이다
(design 결정 1). 표준 라이브러리만 쓴다.
"""

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.answers import Answer
from app.core.retrieval import ScoredChunk

_WHITESPACE = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """NFC → 앞뒤 공백 제거 → 연속 공백 축약 → casefold.

    어간 추출·조사 제거를 넣지 않는 이유는 정확 매치 층이 글자만 보는 층이기 때문이다."""
    normalized = unicodedata.normalize("NFC", query)
    return _WHITESPACE.sub(" ", normalized.strip()).casefold()


def derive_cache_key(
    *,
    query: str,
    top_k: int,
    prompt_version: str,
    index_signature: str,
    model: str,
) -> str:
    """다섯 재료에서 캐시 항목의 지문을 유도한다 — 하나만 달라도 다른 항목이 된다.

    질의를 키에 그대로 담으면 `redis-cli KEYS` 나 로그에 질문이 복원 가능하게 남는다."""
    _require_resolved_top_k(top_k)
    return _fingerprint(
        {
            "query": normalize_query(query),
            "top_k": top_k,
            "prompt_version": prompt_version,
            "index_signature": index_signature,
            "model": model,
        }
    )


def derive_cache_scope(
    *,
    top_k: int,
    prompt_version: str,
    index_signature: str,
    model: str,
) -> str:
    """질의를 뺀 네 재료의 지문 — 유사 매치가 훑어도 되는 후보 집합의 이름이다.

    K 나 프롬프트가 다른 항목이 유사도만으로 히트가 되는 것을 막는다 (`response-cache`)."""
    _require_resolved_top_k(top_k)
    # 키와 같은 재료를 쓰므로 접두사로 갈라 둔다 — 없으면 질의가 빈 항목의 키와 겹친다.
    return _fingerprint(
        {
            "scope": "qa",
            "top_k": top_k,
            "prompt_version": prompt_version,
            "index_signature": index_signature,
            "model": model,
        }
    )[:16]


def _require_resolved_top_k(top_k: int) -> None:
    """`None` 이 들어오면 K 를 생략한 요청과 기본값을 명시한 요청이 갈린다 (design 결정 14)."""
    if top_k < 1:
        raise ValueError("top_k 는 1 이상이어야 한다")


def _fingerprint(materials: dict[str, object]) -> str:
    """정규 JSON → SHA-256. `derive_index_signature` 와 같은 직렬화 규약이다.

    값 경계가 모호한 연결은 서로 다른 구성이 같은 지문을 받는 자리를 남긴다."""
    canonical = json.dumps(materials, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CacheLayer(StrEnum):
    """히트가 어느 층에서 났는가 — 값이 그대로 응답에 실린다."""

    EXACT = "exact"
    SEMANTIC = "semantic"


@dataclass(frozen=True)
class SourceVersion:
    """캐시 항목이 근거로 쓴 문서 하나와 그때의 리비전.

    태그 무효화의 대상이자 현재성 재검증의 기준이다 (design 결정 4·7)."""

    document_id: str
    revision: str


@dataclass(frozen=True)
class CachedAnswer:
    """캐시 항목 하나 — `sources` 와 `done` 이벤트를 복원할 재료 전부.

    `Answer` 를 통째로 드는 것은 종료 사유와 인용의 불변식을 다시 구현하지 않으려는 것이다."""

    answer: Answer
    top_k: int
    target_documents: int
    #: 인용이 아니라 근거 전부다 — 히트가 재생하는 것은 청크 본문이고,
    #: `insufficient_evidence` 항목에는 인용이 아예 없다 (design 결정 4).
    sources: tuple[ScoredChunk, ...] = ()

    @property
    def source_versions(self) -> tuple[SourceVersion, ...]:
        """근거가 걸친 `(문서, 리비전)` 목록. 같은 문서의 청크 여럿은 하나로 묶인다."""
        versions = {
            SourceVersion(document_id=chunk.document_id, revision=chunk.revision): None
            for chunk in self.sources
        }
        return tuple(versions)

    @property
    def document_ids(self) -> tuple[str, ...]:
        """이 항목에 태그로 달릴 문서 식별자들 — 등장 순서를 유지한다."""
        return tuple({version.document_id: None for version in self.source_versions})


@dataclass(frozen=True)
class CacheLookup:
    """조회 한 번의 결과 — 히트 여부·층·유사도·항목.

    미스와 히트가 같은 타입이라 호출부가 `None` 검사와 필드 접근을 섞지 않는다."""

    entry: CachedAnswer | None = None
    layer: CacheLayer | None = None
    similarity: float | None = None
    #: 히트한 항목의 지문. 재검증에서 버린 항목을 지우려면 어느 항목이었는지가 필요한데,
    #: 유사 매치에서는 그것을 저장소만 안다 (design 결정 7).
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if (self.entry is None) != (self.layer is None):
            raise ValueError("히트는 항목과 층을 함께 든다")
        if (self.entry is None) != (self.fingerprint is None):
            raise ValueError("히트는 자기 지문을 함께 든다")
        # 유사도는 유사 매치의 판정 근거라, 다른 층에 실리면 응답에서 뜻이 갈린다.
        if (self.similarity is not None) != (self.layer is CacheLayer.SEMANTIC):
            raise ValueError("유사도는 유사 매치 히트에만 실린다")

    @property
    def hit(self) -> bool:
        return self.entry is not None

    @classmethod
    def miss(cls) -> "CacheLookup":
        """미스 — 층도 유사도도 없다."""
        return cls()

    @classmethod
    def exact(cls, fingerprint: str, entry: CachedAnswer) -> "CacheLookup":
        """정확 매치 히트."""
        return cls(entry=entry, layer=CacheLayer.EXACT, fingerprint=fingerprint)

    @classmethod
    def semantic(cls, fingerprint: str, entry: CachedAnswer, similarity: float) -> "CacheLookup":
        """유사 매치 히트 — 판정에 쓰인 유사도를 함께 든다."""
        return cls(
            entry=entry,
            layer=CacheLayer.SEMANTIC,
            similarity=similarity,
            fingerprint=fingerprint,
        )


@dataclass(frozen=True)
class SimilarityMatch:
    """후보 스캔이 고른 하나 — 항목 지문과 그 유사도."""

    fingerprint: str
    similarity: float


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """두 벡터의 코사인 유사도. 차원이 다르거나 비어 있으면 오류.

    한쪽이 영벡터면 방향이 없어 `0.0` 이다 — 임계값 아래라 히트가 되지 않는다."""
    if not left or not right:
        raise ValueError("빈 벡터는 비교하지 않는다")
    if len(left) != len(right):
        raise ValueError("차원이 다른 벡터는 비교하지 않는다")

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norms = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norms if norms else 0.0


def best_match(
    query_vector: Sequence[float],
    candidates: Iterable[tuple[str, Sequence[float]]],
    *,
    threshold: float,
) -> SimilarityMatch | None:
    """임계값 이상인 후보 중 유사도가 가장 높은 하나. 없으면 `None`.

    유사도가 같으면 먼저 온 후보가 남는다 — 후보 순서가 최신순이라 최신이 이긴다."""
    best: SimilarityMatch | None = None
    for fingerprint, vector in candidates:
        similarity = cosine_similarity(query_vector, vector)
        if similarity < threshold:
            continue
        if best is None or similarity > best.similarity:
            best = SimilarityMatch(fingerprint=fingerprint, similarity=similarity)
    return best
