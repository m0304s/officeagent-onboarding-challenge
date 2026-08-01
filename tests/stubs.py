"""테스트용 프로브 대역.

`HealthProbe`가 프로토콜이라 대역을 주입해 의존성 상태를 결정론적으로 만들 수 있다.
실제 컨테이너를 죽여가며 상태를 만들면 느리고 불안정하며, 무엇보다 외부 서비스 없이
스위트가 돌아야 한다.
"""

import asyncio

from app.core.models import ProbeResult, Status


class StubProbe:
    """지정한 결과를 그대로 돌려주는 프로브.

    `delay`를 주면 무응답 상황을, `raises`를 주면 프로브 자체가 터지는 상황을 만든다.
    """

    def __init__(
        self,
        name: str,
        status: Status = Status.OK,
        detail: str | None = None,
        delay: float = 0.0,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self._status = status
        self._detail = detail
        self._delay = delay
        self._raises = raises

    async def check(self) -> ProbeResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return ProbeResult(name=self.name, status=self._status, detail=self._detail)
