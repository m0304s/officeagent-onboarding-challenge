"""헬스 엔드포인트.

정상이면 200, 필수 의존성 중 하나라도 불능이면 503.

503일 때도 **공통 오류 봉투를 쓰지 않는다.** 헬스는 오류를 알리는 엔드포인트가 아니라
상태를 보고하는 엔드포인트다. 두 코드에서 본문 구조가 갈리면 소비자가 파서를 둘 들고
있어야 하고, "어느 의존성이 문제인가"가 오류 메시지 문자열 안으로 뭉개진다.
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
    """의존성 목록은 정상 여부와 무관하게 항상 같은 집합으로 내보낸다.

    키가 사라지는 것과 값이 비정상인 것을 소비자가 구분할 필요가 없게 하기 위함이다.
    """
    return {
        "status": report.status.value,
        "dependencies": {
            dep.name: {"status": dep.status.value, "detail": dep.detail}
            for dep in report.dependencies
        },
    }
