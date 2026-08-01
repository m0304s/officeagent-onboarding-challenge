"""앱 팩토리와 의존성 배선.

배선은 여기서 한 번만 한다. 모듈 전역 싱글턴을 두지 않으므로 테스트가 프로브를 대역으로
갈아끼울 수 있다.

**부팅 경로에서 LLM 제공자를 호출하지 않는다.** 인증 정보가 없거나 손상되어 있어도 기동과
헬스 보고는 성립해야 한다. LLM 어댑터는 이 change에 존재하지 않으며, 도입될 때에도 지연
초기화로 붙인다.
"""

from collections.abc import Sequence

from fastapi import FastAPI

from app.adapters.cache.probe import CacheProbe
from app.adapters.parsers import ParserRegistry, default_parsers
from app.adapters.protocols import DocumentParser, HealthProbe
from app.adapters.vector_store.probe import VectorStoreProbe
from app.api.errors import register_error_handlers
from app.api.logging import RequestLoggingMiddleware, configure_logging
from app.api.routes import documents, health
from app.config import Settings, get_settings
from app.services.health import HealthService
from app.services.ingestion import IngestionService


def default_probes(settings: Settings) -> tuple[HealthProbe, ...]:
    return (
        CacheProbe(url=settings.cache_url, timeout_seconds=settings.probe_timeout_seconds),
        VectorStoreProbe(
            path=settings.vector_store_path,
            timeout_seconds=settings.probe_timeout_seconds,
        ),
    )


def create_app(
    settings: Settings | None = None,
    probes: Sequence[HealthProbe] | None = None,
    parsers: Sequence[DocumentParser] | None = None,
) -> FastAPI:
    """앱을 만든다.

    `settings`/`probes`/`parsers`를 주입할 수 있는 이유는 테스트가 의존성 상태를
    결정론적으로 구성해야 하기 때문이다. 실제 컨테이너를 죽여가며 상태를 만들면 느리고
    불안정해진다. 파서는 여기에 더해 **교체 지점**이기도 하다 — PDF 파서를 통째로
    갈아끼워도 바뀌는 곳은 이 인자 하나다.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    probes = default_probes(settings) if probes is None else tuple(probes)

    app = FastAPI(title=settings.app_name)
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
        chunk_strategy=settings.chunk_strategy,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    app.include_router(health.router)
    app.include_router(documents.router)
    return app
