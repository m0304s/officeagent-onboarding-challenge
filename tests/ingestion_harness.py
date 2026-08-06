"""수집 서비스 테스트 하네스 — 어댑터 넷을 조립하는 코드를 모아 둔다.

각 테스트가 자기가 바꾸는 것 하나만 인자로 드러내게 한다. 기본이 전부 대역인 이유는
서비스 테스트가 재는 것이 순서와 판정이고, 그 실패는 주입할 수 있어야 만들어져서다.
"""

from app.adapters.cache.null import NullResponseCache
from app.adapters.parsers import (
    ParserRegistry,
    PdfExtraction,
    PdfExtractionChoice,
    default_parsers,
    select_pdf_extraction,
)
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
    pdf_extraction: PdfExtractionChoice | None = None,
    size: int = 200,
    overlap: int = 40,
    batch_size: int = 64,
    concurrency: int = 2,
) -> IngestionService:
    """수집 서비스 하나. 지정하지 않은 어댑터는 기본 대역이 채운다.

    서명을 여기서 유도하는 것은 배선이 하는 일을 그대로 재현하기 위해서다."""
    embedder = embedder or FakeEmbedder()
    registry = registry or StubDocumentRegistry()
    # 파서와 서명 재료를 같은 객체에서 꺼낸다 — 한쪽만 지정하는 길을 남기면 그것이
    # 가장 쓰기 쉬운 코드가 되고, 어긋난 하네스는 오류 없이 통과한다.
    pdf_extraction = pdf_extraction or select_pdf_extraction(PdfExtraction.MARKDOWN)
    return IngestionService(
        ParserRegistry(default_parsers(pdf_extraction) if parsers is None else parsers),
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
            pdf_extraction_signature=pdf_extraction.signature_material,
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
