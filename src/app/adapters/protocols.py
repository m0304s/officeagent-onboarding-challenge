"""어댑터 계약.

이 change가 어댑터에 요구하는 동작은 "너 살아 있냐" 하나뿐이므로 좁은 프로토콜만 정의한다.
`VectorStore`(upsert/query/delete)나 `CacheStore`(get/set/invalidate) 같은 넓은 인터페이스는
**정의하지 않는다.** 소비자가 없는 상태에서 정한 인터페이스는 실제 사용처가 생기는 순간
거의 반드시 틀린 것으로 드러난다. 각 프로토콜은 그것을 실제로 쓰는 change에서 정의한다.
"""

from typing import Protocol, runtime_checkable

from app.core.models import ProbeResult


@runtime_checkable
class HealthProbe(Protocol):
    """의존성 하나의 도달 가능 여부를 보고한다.

    구현체는 예외를 던져도 된다. 서비스 계층이 잡아 해당 프로브만 비정상으로 기록한다.
    다만 스스로 판별 가능한 실패는 예외 대신 `ProbeResult`로 돌려주는 편이 사유 요약이
    구체적이라 진단에 낫다.
    """

    name: str

    async def check(self) -> ProbeResult: ...
