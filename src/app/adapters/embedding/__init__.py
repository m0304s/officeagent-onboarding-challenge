"""임베딩 어댑터.

`Embedder` 프로토콜(`adapters/protocols.py`)의 구현체. 지금은 로컬 sentence-transformers
모델 하나이며, 교체 지점은 이 패키지 하나로 국한된다 — ONNX 런타임 기반으로 갈아타도
`services/`·`core/`·`api/`는 한 줄도 바뀌지 않는다.
"""

from app.adapters.embedding.local import (
    KNOWN_MODEL_PROFILES,
    PREFIX_CONVENTION,
    SentenceTransformerEmbedder,
)

__all__ = ["KNOWN_MODEL_PROFILES", "PREFIX_CONVENTION", "SentenceTransformerEmbedder"]
