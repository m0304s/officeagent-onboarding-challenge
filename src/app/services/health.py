"""헬스 점검 오케스트레이션.

프로브들을 병렬로 돌리고, 프로브별 상한과 전체 상한을 함께 건다.
하나가 매달려도 나머지 결과는 보고한다 — 어느 의존성이 문제인지 식별하려면 그래야 한다.
"""

import asyncio
import logging
from collections.abc import Sequence

from app.adapters.protocols import HealthProbe
from app.core.models import HealthReport, ProbeResult, Status

logger = logging.getLogger(__name__)


class HealthService:
    def __init__(
        self,
        probes: Sequence[HealthProbe],
        probe_timeout_seconds: float,
        total_timeout_seconds: float,
    ) -> None:
        self._probes = tuple(probes)
        self._probe_timeout = probe_timeout_seconds
        self._total_timeout = total_timeout_seconds

    async def check(self) -> HealthReport:
        tasks = [asyncio.create_task(self._run(probe)) for probe in self._probes]
        await asyncio.wait(tasks, timeout=self._total_timeout)

        results: list[ProbeResult] = []
        for probe, task in zip(self._probes, tasks, strict=True):
            if task.done():
                results.append(task.result())
            else:
                # 전체 상한에 걸린 프로브. 취소하되 나머지 결과는 그대로 보고한다.
                task.cancel()
                results.append(
                    ProbeResult(
                        name=probe.name,
                        status=Status.UNAVAILABLE,
                        detail="전체 점검 시간 초과",
                    )
                )
        return HealthReport(dependencies=tuple(results))

    async def _run(self, probe: HealthProbe) -> ProbeResult:
        """예외를 밖으로 내보내지 않는다 — 프로브 하나의 버그로 헬스가 500이 되면
        정작 진단해야 할 때 아무것도 못 본다."""
        try:
            return await asyncio.wait_for(probe.check(), timeout=self._probe_timeout)
        except TimeoutError:
            return ProbeResult(name=probe.name, status=Status.UNAVAILABLE, detail="응답 시간 초과")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("의존성 점검이 예기치 않게 실패했습니다: %s", probe.name, exc_info=exc)
            return ProbeResult(
                name=probe.name,
                status=Status.UNAVAILABLE,
                detail=f"점검 실패 ({type(exc).__name__})",
            )
