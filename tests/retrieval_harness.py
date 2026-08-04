"""검색 서비스 테스트 하네스 — 수집·검색을 같은 대역 위에 세운다.

대역에 청크를 손으로 심으면 "둘이 어긋났을 때 검색이 무엇을 하는가"를 테스트가 스스로
조작하게 된다. 기본 하한과 기본 구성(밀집 단독)의 근거는 `tests/README.md` 에 있다.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.adapters.cache.null import NullResponseCache
from app.adapters.parsers import ParserRegistry, default_parsers
from app.adapters.protocols import Embedder, LexicalIndex
from app.adapters.retrievers import RetrieverDependencies, build_retriever
from app.core.chunking import CHUNK_STRATEGY_VERSION, ChunkStrategy
from app.core.documents import Document, derive_index_signature
from app.core.lexical import DEFAULT_TOKENIZER
from app.core.prompting import PROMPT_VERSION
from app.services.cache import CacheService
from app.services.ingestion import IngestionService
from app.services.retrieval import RetrievalService, RetrieverBinding
from tests.stubs import (
    FakeEmbedder,
    StubDocumentRegistry,
    StubLexicalIndex,
    StubResponseCache,
    StubVectorStore,
)

#: 후보 깊이. `Settings` 기본값과 같은 값이라 하네스가 배포 구성과 같은 깊이로 돈다.
CANDIDATE_DEPTH = 50

#: 여러 청크로 쪼개지는 길이의 한국어 문서 둘. 주제를 갈라 둔 이유는 "다른 문서의 청크가
#: 섞였는가"를 본문으로 눈에 보이게 확인하기 위해서다.
POLICY = (
    "사내 복리후생 안내\n\n"
    "교육비는 연 200만원까지 지원합니다. 신청은 인사팀에 하며 영수증을 첨부해야 합니다.\n"
    "재택근무는 주 2회까지 가능합니다. 팀장 승인 후 사내 시스템에 등록합니다.\n"
    "연차는 입사 첫해 11일, 이듬해부터 15일이 부여됩니다. 미사용 연차는 이월되지 않습니다.\n"
    "건강검진은 매년 1회 지원하며 지정 병원에서 받습니다. 배우자 검진도 포함됩니다.\n"
    "경조사비는 결혼 100만원, 조사 50만원을 지급합니다. 경조 휴가는 별도로 부여됩니다.\n"
    "야근 식대는 1인당 1만원까지 지원하며 법인카드로 결제합니다. 주말 근무도 같습니다.\n"
    "도서 구입비는 월 5만원까지 지원합니다. 업무와 관련된 도서에 한합니다.\n"
    "동호회 활동비는 분기당 인당 3만원을 지원합니다. 5인 이상이면 등록할 수 있습니다.\n"
    "장기근속 포상은 5년 단위로 지급하며 유급 휴가 5일이 함께 부여됩니다.\n"
)
GUIDE = (
    "개발 가이드\n\n"
    "코드 리뷰는 최소 1명의 승인을 받아야 머지할 수 있습니다. 리뷰는 24시간 안에 응답합니다.\n"
    "브랜치는 main 에서 따고 feature 접두사를 붙입니다. 머지는 스쿼시로 합니다.\n"
    "배포는 태그를 올리면 자동으로 진행됩니다. 롤백은 이전 태그를 다시 배포합니다.\n"
    "P1 장애는 30분 안에 원인을 파악하고 담당자를 지정합니다. 사후 회고는 3일 안에 작성합니다.\n"
    "테스트는 커밋 전에 전부 통과해야 합니다. 실패한 채로 올린 브랜치는 리뷰하지 않습니다.\n"
    "의존성 추가는 팀 논의를 거칩니다. 라이선스와 유지보수 상태를 함께 확인합니다.\n"
    "로그에는 사용자 입력 원문을 남기지 않습니다. 식별자와 집계값만 기록합니다.\n"
    "문서와 코드가 어긋나면 문서를 먼저 고칩니다. 어긋난 문서는 없는 것보다 나쁩니다.\n"
)
SHORT = "재택근무는 주 2회까지 가능합니다.\n"


@dataclass
class Harness:
    """같은 대역 위에 선 수집·검색 서비스 한 쌍."""

    ingestion: IngestionService
    retrieval: RetrievalService
    embedder: Embedder
    store: StubVectorStore
    lexical: LexicalIndex
    registry: StubDocumentRegistry
    #: 수집과 답변이 함께 보는 캐시 계층. 무효화가 두 서비스에 걸친 계약이라 하나여야 한다.
    cache_service: CacheService
    #: 캐시 저장소 대역. 캐시를 끈 하네스에서는 `None` 이다.
    cache: StubResponseCache | None
    index_signature: str
    _top_k: int
    _min_score: float
    _retrievers: tuple[str, ...]
    _weights: dict[str, float]
    _required: tuple[str, ...]

    async def ingest(self, filename: str, text: str) -> Document:
        return (await self.ingestion.ingest(filename, text.encode())).document

    def searching_with(
        self,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
        retrievers: Sequence[str] | None = None,
        weights: dict[str, float] | None = None,
        required: Sequence[str] | None = None,
    ) -> RetrievalService:
        """같은 대역을 보되 설정만 다른 검색 서비스.

        앱을 다시 만들면 저장소도 새로 생겨 하한·구성 비교가 무의미해진다."""
        return _service(
            self.embedder,
            self.store,
            self.lexical,
            self.registry,
            signature=self.index_signature,
            top_k=self._top_k if top_k is None else top_k,
            min_score=self._min_score if min_score is None else min_score,
            retrievers=self._retrievers if retrievers is None else retrievers,
            weights=self._weights if weights is None else weights,
            required=self._required if required is None else required,
        )

    def chunk_text(self, document_id: str, chunk_index: int) -> str:
        """저장된 청크의 실제 본문.

        정규화와 분할을 거친 값이라 원문에서 짐작할 수 없는데, 순위 단언이 그 값에 기댄다."""
        return self.store.chunks_of(document_id)[chunk_index].text


def make_harness(
    *,
    embedder: Embedder | None = None,
    vector_store: StubVectorStore | None = None,
    lexical_index: LexicalIndex | None = None,
    registry: StubDocumentRegistry | None = None,
    cache: StubResponseCache | None = None,
    semantic_threshold: float = 0.93,
    operation_timeout_seconds: float = 0.2,
    breaker_failures: int = 3,
    breaker_cooldown_seconds: float = 30.0,
    clock=None,
    size: int = 200,
    overlap: int = 40,
    top_k: int = 5,
    min_score: float = 0.0,
    retrievers: Sequence[str] = ("dense",),
    weights: dict[str, float] | None = None,
    required: Sequence[str] = ("dense",),
) -> Harness:
    embedder = embedder or FakeEmbedder()
    store = vector_store or StubVectorStore()
    lexical = lexical_index or StubLexicalIndex()
    registry = registry or StubDocumentRegistry()
    weights = weights or {}
    # 배선(`create_app`)이 하는 일 그대로 — 한 번 유도해 두 서비스에 같은 값을 준다.
    signature = derive_index_signature(
        embedder_signature=embedder.signature,
        chunk_strategy=ChunkStrategy.RECURSIVE.value,
        chunk_strategy_version=CHUNK_STRATEGY_VERSION,
        chunk_size=size,
        chunk_overlap=overlap,
        tokenizer_signature=DEFAULT_TOKENIZER.signature_material,
    )
    # 배선(`create_app`)과 같다 — 수집과 답변이 같은 캐시 계층 인스턴스를 본다.
    cache_service = CacheService(
        cache or NullResponseCache(),
        registry,
        embedder,
        prompt_version=PROMPT_VERSION,
        model="fake-model",
        semantic_threshold=semantic_threshold,
        semantic_candidates=200,
        operation_timeout_seconds=operation_timeout_seconds,
        breaker_failures=breaker_failures,
        breaker_cooldown_seconds=breaker_cooldown_seconds,
        **({"clock": clock} if clock else {}),
    )
    return Harness(
        ingestion=IngestionService(
            ParserRegistry(default_parsers()),
            embedder,
            store,
            lexical,
            registry,
            cache_service,
            index_signature=signature,
            chunk_strategy=ChunkStrategy.RECURSIVE,
            chunk_size=size,
            chunk_overlap=overlap,
            embedding_batch_size=64,
            concurrency=2,
        ),
        retrieval=_service(
            embedder,
            store,
            lexical,
            registry,
            signature=signature,
            top_k=top_k,
            min_score=min_score,
            retrievers=retrievers,
            weights=weights,
            required=required,
        ),
        embedder=embedder,
        store=store,
        lexical=lexical,
        registry=registry,
        cache_service=cache_service,
        cache=cache,
        index_signature=signature,
        _top_k=top_k,
        _min_score=min_score,
        _retrievers=tuple(retrievers),
        _weights=weights,
        _required=tuple(required),
    )


def _service(
    embedder: Embedder,
    store: StubVectorStore,
    lexical: LexicalIndex,
    registry: StubDocumentRegistry,
    *,
    signature: str,
    top_k: int,
    min_score: float,
    retrievers: Sequence[str],
    weights: dict[str, float],
    required: Sequence[str],
) -> RetrievalService:
    """이름 목록을 배선이 하는 것과 같은 경로로 인스턴스에 옮긴다.

    등록부를 지나가야 "이름으로 구성한다"가 실제로 검증된다."""
    dependencies = RetrieverDependencies(
        embedder=embedder,
        vector_store=store,
        lexical_index=lexical,
        dense_min_score=min_score,
    )
    return RetrievalService(
        [
            RetrieverBinding(
                name=name,
                weight=weights.get(name, 1.0),
                candidate_depth=CANDIDATE_DEPTH,
                required=name in required,
                retriever=build_retriever(name, dependencies),
            )
            for name in retrievers
        ],
        registry,
        index_signature=signature,
        top_k=top_k,
    )
