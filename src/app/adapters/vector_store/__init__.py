"""벡터 스토어 어댑터.

`VectorStore` 프로토콜(`adapters/protocols.py`)의 구현체와 헬스 프로브. 임베디드
퍼시스턴트 모드라 별도 컨테이너 없이 볼륨 하나로 영속화된다.
"""

from app.adapters.vector_store.chroma import ChromaVectorStore
from app.adapters.vector_store.probe import VectorStoreProbe

__all__ = ["ChromaVectorStore", "VectorStoreProbe"]
