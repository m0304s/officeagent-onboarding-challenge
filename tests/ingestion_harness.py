"""수집 서비스 테스트 하네스.

서비스가 어댑터 넷을 주입받으므로, 테스트마다 그 넷을 조립하는 코드가 반복된다.
여기 모아 두어 각 테스트가 **자기가 바꾸는 것 하나만** 인자로 드러내게 한다.

기본 구성은 전부 대역이다. 저장소 어댑터의 성질(트랜잭션·영속성·메타데이터 왕복)은
`test_registry.py`·`test_vector_store.py` 가 실물로 덮고 있고, 서비스 테스트가 검증할
것은 **순서와 판정**이다 — 무엇을 언제 쓰고 지우는가, 실패했을 때 무엇이 남는가.
그리고 그 실패는 주입할 수 있어야 만들어진다.
"""

from app.adapters.cache.null import NullResponseCache
from app.adapters.parsers import ParserRegistry, default_parsers
from app.core.chunking import CHUNK_STRATEGY_VERSION, ChunkStrategy
from app.core.documents import derive_index_signature
from app.core.lexical import DEFAULT_TOKENIZER
from app.core.prompting import PROMPT_VERSION
from app.services.cache import CacheService
from app.services.ingestion import IngestionService
from tests.stubs import (
    FakeEmbedder,
    StubDocumentRegistry,
    StubLexicalIndex,
    StubResponseCache,
    StubVectorStore,
)

LONG_KOREAN = (
    "사내 복리후생 안내\n\n" + "교육비는 연 200만원까지 지원합니다. 신청은 인사팀에 합니다. " * 30
)


def make_service(
    *,
    parsers=None,
    embedder=None,
    vector_store=None,
    lexical_index=None,
    registry=None,
    cache: StubResponseCache | None = None,
    tokenizer=DEFAULT_TOKENIZER,
    size: int = 200,
    overlap: int = 40,
    batch_size: int = 64,
    concurrency: int = 2,
) -> IngestionService:
    """수집 서비스 하나. 지정하지 않은 어댑터는 기본 대역이 채운다.

    색인 서명을 여기서 유도하는 이유는 배선(`create_app`)이 하는 일을 그대로
    재현하기 위해서다. 서비스가 더 이상 스스로 유도하지 않으므로, "임베더
    `signature`를 바꾸면 색인 서명이 달라진다"를 재현하려면 하네스가 실제 배선과
    **같은 재료로** 유도해야 한다.
    """
    embedder = embedder or FakeEmbedder()
    registry = registry or StubDocumentRegistry()
    return IngestionService(
        ParserRegistry(default_parsers() if parsers is None else parsers),
        embedder,
        vector_store or StubVectorStore(),
        lexical_index or StubLexicalIndex(),
        registry,
        # 무효화가 수집의 계약이 되었으므로 하네스도 캐시 계층을 든다. 기본값이 꺼진
        # 캐시인 것은 수집 테스트가 재는 것이 저장 결과의 모양이기 때문이다.
        make_cache_service(cache, registry, embedder),
        index_signature=derive_index_signature(
            embedder_signature=embedder.signature,
            chunk_strategy=ChunkStrategy.RECURSIVE.value,
            chunk_strategy_version=CHUNK_STRATEGY_VERSION,
            chunk_size=size,
            chunk_overlap=overlap,
            tokenizer_signature=tokenizer.signature_material,
        ),
        chunk_strategy=ChunkStrategy.RECURSIVE,
        chunk_size=size,
        chunk_overlap=overlap,
        embedding_batch_size=batch_size,
        concurrency=concurrency,
    )


def make_cache_service(
    cache: StubResponseCache | None,
    registry: StubDocumentRegistry,
    embedder,
) -> CacheService:
    """배선(`create_app`)이 세우는 것과 같은 캐시 계층. 대역을 주지 않으면 꺼진 캐시다."""
    return CacheService(
        cache or NullResponseCache(),
        registry,
        embedder,
        prompt_version=PROMPT_VERSION,
        model="fake-model",
        semantic_threshold=0.93,
        semantic_candidates=200,
        operation_timeout_seconds=0.2,
        breaker_failures=3,
        breaker_cooldown_seconds=30.0,
    )
