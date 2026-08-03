"""수집 서비스 테스트 하네스.

서비스가 어댑터 넷을 주입받으므로, 테스트마다 그 넷을 조립하는 코드가 반복된다.
여기 모아 두어 각 테스트가 **자기가 바꾸는 것 하나만** 인자로 드러내게 한다.

기본 구성은 전부 대역이다. 저장소 어댑터의 성질(트랜잭션·영속성·메타데이터 왕복)은
`test_registry.py`·`test_vector_store.py` 가 실물로 덮고 있고, 서비스 테스트가 검증할
것은 **순서와 판정**이다 — 무엇을 언제 쓰고 지우는가, 실패했을 때 무엇이 남는가.
그리고 그 실패는 주입할 수 있어야 만들어진다.
"""

from app.adapters.parsers import ParserRegistry, default_parsers
from app.core.chunking import CHUNK_STRATEGY_VERSION, ChunkStrategy
from app.core.documents import derive_index_signature
from app.services.ingestion import IngestionService
from tests.stubs import FakeEmbedder, StubDocumentRegistry, StubVectorStore

LONG_KOREAN = (
    "사내 복리후생 안내\n\n" + "교육비는 연 200만원까지 지원합니다. 신청은 인사팀에 합니다. " * 30
)


def make_service(
    *,
    parsers=None,
    embedder=None,
    vector_store=None,
    registry=None,
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
    return IngestionService(
        ParserRegistry(default_parsers() if parsers is None else parsers),
        embedder,
        vector_store or StubVectorStore(),
        registry or StubDocumentRegistry(),
        index_signature=derive_index_signature(
            embedder_signature=embedder.signature,
            chunk_strategy=ChunkStrategy.RECURSIVE.value,
            chunk_strategy_version=CHUNK_STRATEGY_VERSION,
            chunk_size=size,
            chunk_overlap=overlap,
        ),
        chunk_strategy=ChunkStrategy.RECURSIVE,
        chunk_size=size,
        chunk_overlap=overlap,
        embedding_batch_size=batch_size,
        concurrency=concurrency,
    )
