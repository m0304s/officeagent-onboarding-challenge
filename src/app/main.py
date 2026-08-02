"""앱 팩토리와 의존성 배선.

배선은 여기서 한 번만 한다. 모듈 전역 싱글턴을 두지 않으므로 테스트가 어댑터를 대역으로
갈아끼울 수 있고, 구현 교체가 이 파일 한 곳으로 국한된다.

**부팅 경로에서 LLM 제공자를 호출하지 않는다.** 인증 정보가 없거나 손상되어 있어도 기동과
헬스 보고는 성립해야 한다. LLM 어댑터는 이 change에 존재하지 않으며, 도입될 때에도 지연
초기화로 붙인다. 같은 이유로 임베딩 모델도 여기서 올리지 않는다 — 어댑터가 첫 호출에서
로딩한다.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.cache.probe import CacheProbe
from app.adapters.embedding import SentenceTransformerEmbedder
from app.adapters.parsers import ParserRegistry, default_parsers
from app.adapters.protocols import (
    DocumentParser,
    DocumentRegistry,
    Embedder,
    HealthProbe,
    VectorStore,
)
from app.adapters.registry import SqliteDocumentRegistry
from app.adapters.vector_store import ChromaVectorStore, VectorStoreProbe
from app.api.errors import register_error_handlers
from app.api.logging import RequestLoggingMiddleware, configure_logging
from app.api.routes import documents, health
from app.config import Settings, get_settings
from app.services.health import HealthService
from app.services.ingestion import IngestionService

logger = logging.getLogger(__name__)


def default_probes(settings: Settings) -> tuple[HealthProbe, ...]:
    return (
        CacheProbe(url=settings.cache_url, timeout_seconds=settings.probe_timeout_seconds),
        VectorStoreProbe(
            path=settings.vector_store_path,
            timeout_seconds=settings.probe_timeout_seconds,
        ),
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """기동 시 벡터 스토어를 레지스트리에 맞춘다.

    정리 자체가 기동 조건이 되어서는 안 되므로, 서비스가 실패를 삼키고 보고만 한다.
    """
    report = await app.state.ingestion_service.reconcile_storage()
    if report.stale_documents or report.removed_chunks:
        logger.info(
            "기동 정리를 마쳤습니다",
            extra={
                "stale_documents": len(report.stale_documents),
                "removed_chunks": report.removed_chunks,
            },
        )
    yield


def create_app(
    settings: Settings | None = None,
    probes: Sequence[HealthProbe] | None = None,
    parsers: Sequence[DocumentParser] | None = None,
    embedder: Embedder | None = None,
    vector_store: VectorStore | None = None,
    registry: DocumentRegistry | None = None,
) -> FastAPI:
    """앱을 만든다.

    어댑터를 전부 주입할 수 있는 이유는 두 가지다. 테스트가 의존성 상태를 결정론적으로
    구성해야 하고(실제 컨테이너를 죽여가며 상태를 만들면 느리고 불안정하다), 이 인자들이
    곧 **교체 지점**이기 때문이다 — PDF 파서든 임베딩 런타임이든 벡터 스토어든, 갈아끼울
    때 바뀌는 곳은 여기 한 줄이다.
    """
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
    app.state.ingestion_service = IngestionService(
        ParserRegistry(default_parsers() if parsers is None else parsers),
        # 임베더 생성은 모델을 올리지 않는다. 모양(차원·입력 창)은 어댑터가 선언하고,
        # 가중치는 첫 임베딩 호출에서 로딩한다.
        SentenceTransformerEmbedder(settings.embedding_model) if embedder is None else embedder,
        ChromaVectorStore(settings.vector_store_path) if vector_store is None else vector_store,
        SqliteDocumentRegistry(settings.registry_path) if registry is None else registry,
        chunk_strategy=settings.chunk_strategy,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        embedding_batch_size=settings.embedding_batch_size,
        concurrency=settings.ingestion_concurrency,
    )
    app.include_router(health.router)
    app.include_router(documents.router)
    return app
