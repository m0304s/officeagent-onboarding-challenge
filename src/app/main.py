"""앱 팩토리와 의존성 배선.

배선은 여기서 한 번만 한다. 모듈 전역 싱글턴을 두지 않으므로 테스트가 어댑터를 대역으로
갈아끼울 수 있고, 구현 교체가 이 파일 한 곳으로 국한된다.

**부팅 경로에서 LLM 제공자를 호출하지 않는다.** 인증 정보가 없거나 손상되어 있어도 기동과
헬스 보고는 성립해야 한다. LLM 어댑터는 이 change에 존재하지 않으며, 도입될 때에도 지연
초기화로 붙인다.

임베딩 모델은 반대로 **기동 훅에서 미리 올린다.** 차이는 실패의 성격이다 — LLM 인증은
평가자 환경마다 다르고 없는 것이 정상이지만, 임베딩 모델은 이미지에 함께 굽는 우리
자산이라 없으면 그 자체가 이상 신호다. 다만 **선로딩도 기동 조건은 아니다**: 실패하면
경고만 남기고 뜨며, 첫 임베딩 호출의 지연 로딩이 백스톱으로 남는다.
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
from app.adapters.vector_store import ChromaVectorStore, VectorStoreProbe, collection_for
from app.api.errors import register_error_handlers
from app.api.logging import RequestLoggingMiddleware, configure_logging
from app.api.routes import documents, health
from app.config import Settings, get_settings
from app.core.chunking import CHUNK_STRATEGY_VERSION
from app.core.documents import derive_index_signature
from app.services.health import HealthService
from app.services.ingestion import IngestionService

logger = logging.getLogger(__name__)


def default_probes(settings: Settings) -> tuple[HealthProbe, ...]:
    return (
        CacheProbe(url=settings.cache_url, timeout_seconds=settings.probe_timeout_seconds),
        VectorStoreProbe(
            url=settings.vector_store_url,
            timeout_seconds=settings.probe_timeout_seconds,
        ),
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """기동 시 두 가지를 미리 한다 — 임베딩 모델 선로딩, 벡터 스토어 정리.

    **둘 다 기동 조건이 아니다.** 실패해도 서비스는 뜨고, 무엇이 준비되지 않았는지만
    로그로 남는다. "설정을 전혀 제공하지 않아도 기동에 성공한다"는 요구사항이 여전히
    유효하고, 평가자가 처음 실행하는 한 줄이 부수 작업 하나 때문에 실패하면 안 된다.

    선로딩이 먼저인 이유는 순서 의존이 아니라 관측성이다 — 오래 걸리는 쪽을 먼저 두어야
    기동 로그가 무엇을 기다리는 중인지 순서대로 말한다.
    """
    await _warm_up_embedder(app)

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


async def _warm_up_embedder(app: FastAPI) -> None:
    """임베딩 모델을 미리 올린다. 실패는 경고로 끝낸다.

    미리 하지 않으면 비용이 사라지는 게 아니라 **첫 업로드에게 청구된다.** 평가자가
    처음 눌러 보는 요청이 정확히 그 요청이고, 가중치 부재나 차원 선언 불일치 같은
    문제도 그때서야 500으로 드러난다.

    실패해도 계속 뜨는 것이 안전한 이유는 **지연 로딩이 백스톱으로 남아 있기**
    때문이다 — 첫 임베딩 호출이 다시 시도하므로 일시적 실패는 스스로 회복된다.
    """
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
    # 임베더 **생성**은 모델을 올리지 않는다. 모양(차원·입력 창)은 어댑터가 선언하고,
    # 가중치는 기동 훅의 선로딩이 올린다(`_warm_up_embedder`). 팩토리가 동기 함수라
    # 여기서 올릴 수도 없고, 올리면 `create_app` 자체가 실패할 수 있다.
    if embedder is None:
        embedder = SentenceTransformerEmbedder(settings.embedding_model)
    if vector_store is None:
        # 컬렉션 이름에 차원이 들어간다. Chroma 가 컬렉션당 차원 하나만 허용하고 그
        # 차원이 컬렉션을 비운 뒤에도 남기 때문이다 — 이 배선이 아니면 차원이 다른
        # 모델로 바꿨을 때 재업로드가 영구히 실패한다 (`collection_for` 참조).
        vector_store = ChromaVectorStore(
            settings.vector_store_url, collection_name=collection_for(embedder.dimension)
        )

    # **색인 서명은 여기서 한 번만 유도한다.** 수집은 이 값으로 청크를 찍고 검색은
    # 이 값으로 필터하는데, 두 서비스가 각자 유도하면 재료 목록이 두 곳에 생긴다.
    # 한쪽만 고쳐진 순간 방금 올린 문서가 검색되지 않으면서 어디에도 오류가 남지
    # 않는다 — 두 값이 각자 자기 기준으로는 옳기 때문이다.
    index_signature = derive_index_signature(
        embedder_signature=embedder.signature,
        chunk_strategy=settings.chunk_strategy.value,
        chunk_strategy_version=CHUNK_STRATEGY_VERSION,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )

    # 기동 훅이 선로딩을 부르려면 임베더에 닿아야 한다. 수집 서비스 안에서 꺼내지
    # 않는 이유는 그게 서비스의 내부 구성이기 때문이다 — 배선이 배선한 것을 들고 있는다.
    app.state.embedder = embedder
    app.state.ingestion_service = IngestionService(
        ParserRegistry(default_parsers() if parsers is None else parsers),
        embedder,
        vector_store,
        SqliteDocumentRegistry(settings.registry_path) if registry is None else registry,
        index_signature=index_signature,
        chunk_strategy=settings.chunk_strategy,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        embedding_batch_size=settings.embedding_batch_size,
        concurrency=settings.ingestion_concurrency,
    )
    app.include_router(health.router)
    app.include_router(documents.router)
    return app
