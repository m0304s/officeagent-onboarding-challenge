"""벡터 스토어 어댑터.

`VectorStore` 프로토콜(`adapters/protocols.py`)의 구현체와 헬스 프로브. Chroma 를 **서버
모드**로 쓰므로 별도 컨테이너이며, 영속화는 그 컨테이너의 볼륨이 담당한다.
"""

from app.adapters.vector_store.chroma import ChromaVectorStore, collection_for
from app.adapters.vector_store.probe import VectorStoreProbe

__all__ = ["ChromaVectorStore", "VectorStoreProbe", "collection_for"]
