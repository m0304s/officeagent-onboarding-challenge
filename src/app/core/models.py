"""상태 값 객체.

`core/`는 프레임워크와 외부 라이브러리를 import 하지 않는다. 표준 라이브러리만 쓴다.
"""

from dataclasses import dataclass
from enum import StrEnum


class Status(StrEnum):
    """의존성 하나 또는 서비스 전체의 상태."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProbeResult:
    """의존성 점검 한 건의 결과.

    `detail`은 사람이 읽을 실패 사유 요약이다. 스택 트레이스·접속 문자열·자격증명 같은
    내부 정보를 담아서는 안 된다. 그런 정보는 로그로만 남긴다.
    """

    name: str
    status: Status
    detail: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status is Status.OK


@dataclass(frozen=True)
class HealthReport:
    """서비스 전체 상태와 의존성별 상태.

    `dependencies`는 정상 여부와 무관하게 항상 같은 집합이다. 키가 사라지는 것과
    값이 비정상인 것을 소비자가 구분할 필요가 없게 하기 위함이다.
    """

    dependencies: tuple[ProbeResult, ...]

    @property
    def status(self) -> Status:
        return Status.OK if self.is_ok else Status.UNAVAILABLE

    @property
    def is_ok(self) -> bool:
        return all(dep.is_ok for dep in self.dependencies)
