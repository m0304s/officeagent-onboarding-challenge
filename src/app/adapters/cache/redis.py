"""캐시 저장소 연결 — 지연 생성, 재사용, 종료.

`probe.py` 와 클라이언트를 공유하지 않는다. 프로브는 매번 새로 연결해 닫아 "지금 닿는가"를
묻고, 이쪽은 연결을 들고 있어야 요청마다 핸드셰이크를 물지 않는다.
"""

import contextlib
import logging

import redis.asyncio as redis
from redis.exceptions import RedisError

from app.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


def create_client(url: str, *, timeout_seconds: float) -> redis.Redis:
    """접속 문자열로 비동기 클라이언트를 만든다. 연결은 첫 명령에서 열린다.

    `decode_responses` 를 켜지 않는 이유는 벡터를 바이너리로 넣고 빼기 때문이다."""
    try:
        return redis.from_url(
            url,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
            decode_responses=False,
        )
    except (ValueError, RedisError) as exc:
        raise ConfigurationError(f"캐시 저장소 주소가 올바르지 않습니다: {url!r}") from exc


class RedisConnection:
    """클라이언트 하나의 수명. 기동 시점에는 아무 데도 접속하지 않는다.

    캐시에 닿지 못해도 서비스가 기동되어야 한다는 것이 스펙 요구사항이다."""

    def __init__(self, url: str, *, timeout_seconds: float) -> None:
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._client: redis.Redis | None = None

    def client(self) -> redis.Redis:
        """첫 호출에서 만들고 이후 재사용한다 — 여기서 네트워크가 열리지는 않는다."""
        if self._client is None:
            self._client = create_client(self._url, timeout_seconds=self._timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        """종료 시 닫는다. 닫기 실패가 프로세스 종료를 막으면 재배포가 걸린다."""
        client, self._client = self._client, None
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.aclose()
