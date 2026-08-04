"""이름 → retriever 팩토리 표.

동적 import 를 쓰지 않는다 — 등록되지 않은 이름이 런타임에 나타날 수 있게 되면 설정
오타가 기동을 지나쳐 조용히 도는 배포가 된다 (design 결정 5).
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.adapters.protocols import Embedder, LexicalIndex, Retriever, VectorStore
from app.adapters.retrievers.dense import DenseRetriever
from app.adapters.retrievers.lexical import LexicalRetriever
from app.core.exceptions import ConfigurationError


@dataclass(frozen=True)
class RetrieverDependencies:
    """팩토리가 골라 쓸 어댑터와 하한 전부. 배선이 한 번 모아 넘긴다.

    팩토리마다 인자를 따로 받으면 배선이 이름별로 분기해 등록부의 뜻이 없어진다."""

    embedder: Embedder
    vector_store: VectorStore
    lexical_index: LexicalIndex
    dense_min_score: float


def _dense(dependencies: RetrieverDependencies) -> Retriever:
    return DenseRetriever(
        dependencies.embedder,
        dependencies.vector_store,
        min_score=dependencies.dense_min_score,
    )


def _lexical(dependencies: RetrieverDependencies) -> Retriever:
    return LexicalRetriever(dependencies.lexical_index)


#: 설정이 쓸 수 있는 이름 전부. 새 알고리즘을 더할 때 손대는 곳이 이 표 한 줄이다.
RETRIEVER_FACTORIES: dict[str, Callable[[RetrieverDependencies], Retriever]] = {
    "dense": _dense,
    "lexical": _lexical,
}

RETRIEVER_NAMES = frozenset(RETRIEVER_FACTORIES)


def build_retriever(name: str, dependencies: RetrieverDependencies) -> Retriever:
    """이름 하나를 인스턴스로. 등록되지 않은 이름은 기동을 세운다."""
    factory = RETRIEVER_FACTORIES.get(name)
    if factory is None:
        raise ConfigurationError(
            f"알 수 없는 retriever 이름입니다 — {name!r}",
            retriever=name,
            known_retrievers=sorted(RETRIEVER_NAMES),
        )
    return factory(dependencies)
