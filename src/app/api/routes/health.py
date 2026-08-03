"""헬스 엔드포인트 — 정상 200, 필수 의존성 하나라도 불능이면 503.

503 에도 공통 오류 봉투를 쓰지 않는다. 상태 보고와 오류 통지는 다른 일이고, 본문
구조가 코드마다 갈리면 소비자가 파서를 둘 들어야 한다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.core.models import HealthReport
from app.services.health import HealthService

router = APIRouter(tags=["health"])

HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503


def get_health_service(request: Request) -> HealthService:
    """앱 팩토리가 배선한 서비스를 꺼낸다. 모듈 전역 싱글턴을 두지 않는 이유는
    테스트에서 교체가 어려워지기 때문이다."""
    return request.app.state.health_service


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


@router.get("/health")
async def health(service: HealthServiceDep) -> Response:
    report = await service.check()
    return JSONResponse(
        content=_serialize(report),
        status_code=HTTP_OK if report.is_ok else HTTP_SERVICE_UNAVAILABLE,
    )


def _serialize(report: HealthReport) -> dict:
    """의존성 목록을 항상 같은 집합으로 내보낸다 — 키 부재와 비정상 값을 안 갈라도 되게."""
    return {
        "status": report.status.value,
        "dependencies": {
            dep.name: {"status": dep.status.value, "detail": dep.detail}
            for dep in report.dependencies
        },
    }
