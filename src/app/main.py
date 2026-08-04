"""앱 팩토리와 의존성 배선.

전역 싱글턴을 두지 않아 테스트가 어댑터를 갈아끼울 수 있고 교체가 이 파일로 국한된다.
부팅 경로는 LLM 을 건드리지 않고 임베딩만 선로딩한다 (`ARCHITECTURE.md` 의존성 배선).
"""

import logging
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.adapters.cache.probe import CacheProbe
from app.adapters.embedding import SentenceTransformerEmbedder
from app.adapters.lexical import SqliteLexicalIndex, fts5_is_available
from app.adapters.llm import AppServerSession, CodexAnswerGenerator, SessionLaunch, SessionPool
from app.adapters.parsers import ParserRegistry, default_parsers
from app.adapters.protocols import (
    AnswerGenerator,
    DocumentParser,
    DocumentRegistry,
    Embedder,
    HealthProbe,
    LexicalIndex,
    VectorStore,
)
from app.adapters.registry import SqliteDocumentRegistry
from app.adapters.vector_store import ChromaVectorStore, VectorStoreProbe, collection_for
from app.api.errors import register_error_handlers
from app.api.logging import RequestLoggingMiddleware, configure_logging
from app.api.routes import documents, health, qa, search
from app.config import Settings, get_settings, llm_environment
from app.core.chunking import CHUNK_STRATEGY_VERSION
from app.core.documents import derive_index_signature
from app.core.lexical import DEFAULT_TOKENIZER
from app.services.health import HealthService
from app.services.ingestion import IngestionService
from app.services.qa import QaService
from app.services.retrieval import RetrievalService

logger = logging.getLogger(__name__)


def default_probes(settings: Settings) -> tuple[HealthProbe, ...]:
    return (
        CacheProbe(url=settings.cache_url, timeout_seconds=settings.probe_timeout_seconds),
        VectorStoreProbe(
            url=settings.vector_store_url,
            timeout_seconds=settings.probe_timeout_seconds,
        ),
    )


class _CodexLauncher:
    """세션 하나를 띄우는 팩토리. 첫 호출 전까지 CLI 도 파일시스템도 건드리지 않는다.

    지연이 계약이다 — 기동 경로가 자격증명을 요구하면 인증 없는 환경에서 서비스가 죽는다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._launch: SessionLaunch | None = None

    async def __call__(self) -> AppServerSession:
        if self._launch is None:
            self._launch = SessionLaunch(
                # 상속하지 않는다 — 컨테이너 환경변수에 저장소 접속 정보가 들어 있다.
                env=llm_environment(),
                # 에이전트가 파일을 뒤질 대상 자체를 없앤다.
                cwd=Path(tempfile.mkdtemp(prefix="qa-codex-home-")),
                startup_timeout_seconds=self._settings.qa_llm_session_startup_timeout_seconds,
            )
        return await AppServerSession.start(self._launch)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """기동 시 임베딩 선로딩과 벡터 스토어 정리를 하고, 종료 시 세션을 회수한다.

    앞의 둘은 기동 조건이 아니다 — 실패해도 뜨고 로그만 남는다. 오래 걸리는 쪽이 앞이다."""
    await _warm_up_embedder(app)

    if not fts5_is_available():
        # 기동을 세우지 않는다. 어휘 색인 없이도 밀집 검색은 그대로 도는데, 여기서
        # 죽으면 "한 줄 실행"이 런타임 빌드 옵션에 묶인다.
        logger.warning(
            "이 런타임의 sqlite3 에 FTS5 가 없습니다 — 어휘 색인은 첫 사용에서 실패합니다"
        )

    report = await app.state.ingestion_service.reconcile_storage()
    if report.stale_documents or report.removed_chunks:
        logger.info(
            "기동 정리를 마쳤습니다",
            extra={
                "stale_documents": len(report.stale_documents),
                "removed_chunks": report.removed_chunks,
            },
        )
    try:
        yield
    finally:
        # 회수하지 않으면 컨테이너에 자식 프로세스가 남는다. 풀이 없는 배선(테스트가
        # 생성기를 주입한 경우)에서는 회수할 것도 없다.
        pool: SessionPool | None = app.state.session_pool
        if pool is not None:
            await pool.aclose()


async def _warm_up_embedder(app: FastAPI) -> None:
    """임베딩 모델을 미리 올린다. 실패는 경고로 끝낸다.

    미리 안 하면 비용이 첫 업로드에 청구된다. 실패해도 뜨는 것은 지연 로딩이 백스톱이라서다."""
    embedder = app.state.embedder
    try:
        await embedder.warm_up()
    except Exception as exc:
        logger.warning(
            "임베딩 모델 선로딩에 실패했습니다 — 첫 수집 요청에서 다시 시도합니다",
            exc_info=exc,
        )


def create_app(
    settings: Settings | None = None,
    probes: Sequence[HealthProbe] | None = None,
    parsers: Sequence[DocumentParser] | None = None,
    embedder: Embedder | None = None,
    vector_store: VectorStore | None = None,
    lexical_index: LexicalIndex | None = None,
    registry: DocumentRegistry | None = None,
    generator: AnswerGenerator | None = None,
) -> FastAPI:
    """앱을 만든다.

    어댑터를 전부 주입받는 이유는 테스트의 결정론과 교체 지점이 같은 자리이기 때문이다."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    probes = default_probes(settings) if probes is None else tuple(probes)

    app = FastAPI(title=settings.app_name, lifespan=_lifespan)
    app.add_middleware(RequestLoggingMiddleware)
    register_error_handlers(app)
    app.state.settings = settings
    app.state.health_service = HealthService(
        probes=probes,
        probe_timeout_seconds=settings.probe_timeout_seconds,
        total_timeout_seconds=settings.health_total_timeout_seconds,
    )
    # 생성은 모델을 올리지 않는다 — 팩토리가 동기라 올릴 수도 없고, 올리면
    # `create_app` 자체가 실패할 수 있다.
    if embedder is None:
        embedder = SentenceTransformerEmbedder(settings.embedding_model)
    if vector_store is None:
        # 컬렉션 이름에 차원이 든다 — 없으면 차원이 다른 모델로 바꿨을 때 재업로드가
        # 영구히 실패한다 (`collection_for`).
        vector_store = ChromaVectorStore(
            settings.vector_store_url, collection_name=collection_for(embedder.dimension)
        )
    if lexical_index is None:
        lexical_index = SqliteLexicalIndex(
            settings.lexical_index_path,
            tokenizer=DEFAULT_TOKENIZER,
            min_token_rarity=settings.lexical_min_token_rarity,
        )

    # 한 번만 유도한다 — 두 서비스가 각자 유도하면 한쪽만 고쳐진 순간 방금 올린 문서가
    # 오류 없이 검색되지 않는다.
    index_signature = derive_index_signature(
        embedder_signature=embedder.signature,
        chunk_strategy=settings.chunk_strategy.value,
        chunk_strategy_version=CHUNK_STRATEGY_VERSION,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        # 하나의 서명이 두 색인을 함께 지배한다 — 나누면 문서 하나가 "밀집으로는 검색
        # 가능하고 어휘로는 stale" 인 상태를 가질 수 있게 된다 (design 결정 8).
        tokenizer_signature=DEFAULT_TOKENIZER.signature_material,
    )

    # 두 서비스가 같은 인스턴스를 본다 — 아니면 업로드 직후 문서가 검색되지 않는다.
    if registry is None:
        registry = SqliteDocumentRegistry(settings.registry_path)

    # 서비스 안에서 꺼내지 않는다 — 배선이 배선한 것을 들고 있는다.
    app.state.embedder = embedder
    app.state.ingestion_service = IngestionService(
        ParserRegistry(default_parsers() if parsers is None else parsers),
        embedder,
        vector_store,
        lexical_index,
        registry,
        index_signature=index_signature,
        chunk_strategy=settings.chunk_strategy,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        embedding_batch_size=settings.embedding_batch_size,
        concurrency=settings.ingestion_concurrency,
    )
    app.state.retrieval_service = RetrievalService(
        embedder,
        vector_store,
        registry,
        # 수집이 청크를 찍은 것과 같은 서명이라 두 서비스가 같은 색인 세대에 묶인다.
        index_signature=index_signature,
        top_k=settings.retrieval_top_k,
        min_score=settings.retrieval_min_score,
    )
    # 풀은 지연 기동이라 여기서 프로세스가 뜨지 않는다 — 첫 `/qa` 요청이 띄운다.
    app.state.session_pool = None
    if generator is None:
        pool = SessionPool(_CodexLauncher(settings), size=settings.qa_concurrency)
        app.state.session_pool = pool
        generator = CodexAnswerGenerator(
            pool,
            model=settings.qa_llm_model,
            interrupt_grace_seconds=settings.qa_llm_interrupt_grace_seconds,
        )
    app.state.qa_service = QaService(
        app.state.retrieval_service,
        generator,
        timeout_seconds=settings.qa_llm_timeout_seconds,
        max_attempts=settings.qa_llm_max_attempts,
        retry_backoff_seconds=settings.qa_llm_retry_backoff_seconds,
        # 세션 풀과 같은 값이라 두 곳이 같은 수를 두 번 세지 않는다.
        concurrency=settings.qa_concurrency,
    )

    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(search.router)
    app.include_router(qa.router)
    return app
