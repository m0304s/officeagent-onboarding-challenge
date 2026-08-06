"""캐시 저장소 헬스 프로브.

캐시는 별도 컨테이너로 뜨므로 "도달 가능"의 의미가 네트워크 도달이다.
연결한 뒤 ping 과 벡터셋 지원 확인으로 판별한다.
"""

import asyncio
import logging

import redis.asyncio as redis
from redis.exceptions import ResponseError

from app.core.models import ProbeResult, Status

logger = logging.getLogger(__name__)

#: 만들지 않는 키다 — 없는 키의 `VCARD` 는 `0` 이라 지원 여부만 드러난다.
_VECTOR_SET_PROBE_KEY = "qa:probe:vectorset"


class CacheProbe:
    """연결 후 ping 과 벡터셋 확인. 실패 사유는 접속 문자열을 노출하지 않는 요약만 남긴다."""

    name = "cache"

    def __init__(self, url: str, timeout_seconds: float) -> None:
        self._url = url
        self._timeout = timeout_seconds

    async def check(self) -> ProbeResult:
        client = None
        try:
            client = redis.from_url(
                self._url,
                socket_connect_timeout=self._timeout,
                socket_timeout=self._timeout,
            )
            await asyncio.wait_for(client.ping(), timeout=self._timeout)
            if not await asyncio.wait_for(_supports_vector_sets(client), timeout=self._timeout):
                return self._unavailable("벡터셋 미지원 (Redis 8 이상이 필요합니다)")
            return ProbeResult(name=self.name, status=Status.OK)
        except TimeoutError:
            return self._unavailable("응답 시간 초과")
        except Exception as exc:
            # str(exc) 에 접속 URL 이 섞여 나오는 클라이언트가 있어 그대로 쓰지 않는다.
            # 원인 추적에 필요한 정보는 로그로만 남긴다.
            logger.warning("캐시 저장소 점검 실패", exc_info=exc)
            return self._unavailable(f"연결 실패 ({type(exc).__name__})")
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001 - 정리 실패가 점검 결과를 뒤집으면 안 된다
                    logger.debug("캐시 클라이언트 정리 실패", exc_info=True)

    def _unavailable(self, detail: str) -> ProbeResult:
        return ProbeResult(name=self.name, status=Status.UNAVAILABLE, detail=detail)


async def _supports_vector_sets(client) -> bool:
    """유사 매치 후보 선택이 벡터셋에 걸려 있다 — 없는 서버에서는 저장부터 실패한다.

    `COMMAND INFO` 로 묻지 않는 것은 미지원 서버의 nil 을 응답 파서가 처리하지 못해서다."""
    try:
        await client.vset().vcard(_VECTOR_SET_PROBE_KEY)
    except ResponseError:
        return False
    return True
