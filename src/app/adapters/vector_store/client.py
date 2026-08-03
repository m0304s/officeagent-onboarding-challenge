"""Chroma 서버 접속 — 주소 해석과 클라이언트 생성.

해석과 생성을 한곳에 모은 이유는 프로브가 정상이라고 말하는 서버와 실제로 쓰는 서버가
갈리지 않게 하려는 것이다. 해석은 기동 시점, 접속은 첫 사용 시점이다.
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

    설정을 셋이 아니라 URL 하나로 둔 이유는 셋이 늘 함께 바뀌는 값이기 때문이다."""
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

    `chromadb` import 가 비싸 모듈 최상단이 아니라 여기서 한다."""
    import chromadb
    from chromadb.config import Settings

    return chromadb.HttpClient(
        host=endpoint.host,
        port=endpoint.port,
        ssl=endpoint.ssl,
        # 켠 적 없는 외부 전송이 기본값으로 일어나는 것을 막는다.
        settings=Settings(anonymized_telemetry=False),
    )
