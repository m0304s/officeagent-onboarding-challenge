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

    `detail` 에 내부 정보(트레이스·접속 문자열·자격증명)를 담지 않는다 — 응답으로 샌다."""

    name: str
    status: Status
    detail: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status is Status.OK


@dataclass(frozen=True)
class HealthReport:
    """서비스 전체 상태와 의존성별 상태.

    `dependencies` 는 항상 같은 집합이다 — 키 부재와 비정상 값을 구분할 필요를 없앤다."""

    dependencies: tuple[ProbeResult, ...]

    @property
    def status(self) -> Status:
        return Status.OK if self.is_ok else Status.UNAVAILABLE

    @property
    def is_ok(self) -> bool:
        return all(dep.is_ok for dep in self.dependencies)
