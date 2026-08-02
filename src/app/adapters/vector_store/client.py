"""Chroma 서버 접속 — 주소 해석과 클라이언트 생성.

어댑터와 헬스 프로브가 같은 서버를 본다. 주소를 두 곳에서 각자 해석하면 프로브가 정상이라고
말하는 서버와 실제로 쓰는 서버가 달라질 수 있어, 해석과 생성을 여기 하나로 모은다.

**주소 해석은 기동 시점에, 접속은 첫 사용 시점에** 한다. 오타 난 URL 은 기동을 막아야
하지만(설정 오류는 조용히 넘어가지 않는다), 서버가 떠 있는지는 기동 조건이 아니다 —
의존성이 불능이어도 서비스는 뜨고 헬스가 그 사실을 보고한다는 기존 규약 그대로다.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.core.exceptions import ConfigurationError

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class ChromaEndpoint:
    """Chroma 서버 하나의 접속 정보."""

    host: str
    port: int
    ssl: bool

    def __str__(self) -> str:
        """로그에 남기는 표현. 자격증명이 없는 주소라 그대로 적어도 된다."""
        return f"{'https' if self.ssl else 'http'}://{self.host}:{self.port}"


def parse_url(url: str) -> ChromaEndpoint:
    """`http://host:port` 를 접속 정보로 옮긴다. 형식이 아니면 `ConfigurationError`.

    Chroma 클라이언트가 host·port·ssl 을 따로 받으므로 어딘가에서는 쪼개야 한다.
    설정 항목을 셋으로 늘리는 대신 URL 하나로 두는 이유는, 그 셋이 언제나 함께 바뀌는
    값이라 따로 두면 서로 어긋난 조합을 만들 수 있기 때문이다.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _DEFAULT_PORTS or not parsed.hostname:
        raise ConfigurationError(
            f"벡터 스토어 주소가 올바르지 않습니다: {url!r} — http://host:port 형식이어야 합니다"
        )
    return ChromaEndpoint(
        host=parsed.hostname,
        port=parsed.port or _DEFAULT_PORTS[parsed.scheme],
        ssl=parsed.scheme == "https",
    )


def create_client(endpoint: ChromaEndpoint) -> Any:
    """블로킹. 스레드풀에서만 호출한다 — 생성 시점에 서버로 요청이 나간다.

    `chromadb` import 는 비싸서 모듈 최상단이 아니라 여기서 한다. 앱 기동을 느리게 하지
    않기 위해서다.
    """
    import chromadb
    from chromadb.config import Settings

    return chromadb.HttpClient(
        host=endpoint.host,
        port=endpoint.port,
        ssl=endpoint.ssl,
        # 텔레메트리를 끈다. 평가자 환경이 오프라인일 수 있고, 무엇보다 우리가 켠 적 없는
        # 외부 전송이 기본값으로 일어나면 안 된다.
        settings=Settings(anonymized_telemetry=False),
    )
