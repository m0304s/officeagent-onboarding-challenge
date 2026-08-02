"""벡터 스토어 헬스 프로브.

Chroma 를 **서버 모드**로 쓰므로 "도달 가능"의 의미가 네트워크 도달이다. 캐시 프로브와
같은 성질이고, 불능의 실제 형태는 컨테이너 미기동·주소 오설정·네트워크 단절이다.

**실제로 heartbeat 를 호출한다.** 클라이언트 객체가 만들어졌다는 사실만으로 정상 판정하면
서버가 없는 채로 "정상"이 나오고 첫 업로드에서 터진다.
"""

import asyncio
import logging

from app.adapters.vector_store.client import create_client, parse_url
from app.core.models import ProbeResult, Status

logger = logging.getLogger(__name__)


class VectorStoreProbe:
    """Chroma 서버에 heartbeat 를 보낸다.

    파이썬 클라이언트가 동기 HTTP 라 호출이 블로킹이므로 스레드풀로 오프로드한다.
    """

    name = "vector_store"

    def __init__(self, url: str, timeout_seconds: float) -> None:
        # 주소 해석은 여기서 — 오타는 기동을 막아야 한다. 접속은 첫 점검에서 한다.
        self._endpoint = parse_url(url)
        self._timeout = timeout_seconds
        self._client = None  # 초기화 비용이 크므로 한 번만 만들어 재사용

    async def check(self) -> ProbeResult:
        try:
            await asyncio.wait_for(asyncio.to_thread(self._probe), timeout=self._timeout)
            return ProbeResult(name=self.name, status=Status.OK)
        except TimeoutError:
            # 상한은 **응답**에 걸린다. 클라이언트가 자체 타임아웃을 노출하지 않아, 매달린
            # 요청을 보내는 워커 스레드 자체는 라이브러리·OS 타임아웃까지 남는다. 헬스
            # 응답 시간이 그것에 끌려가지 않는다는 점이 여기서 지켜야 할 성질이다.
            return self._unavailable("응답 시간 초과")
        except Exception as exc:
            # str(exc) 에 접속 주소가 섞여 나올 수 있어 그대로 쓰지 않는다.
            # 원인 추적에 필요한 정보는 로그로만 남긴다.
            logger.warning("벡터 스토어 점검 실패", exc_info=exc)
            return self._unavailable(f"연결 실패 ({type(exc).__name__})")

    def _probe(self) -> None:
        """블로킹. 스레드풀에서만 호출한다."""
        if self._client is None:
            self._client = create_client(self._endpoint)
        self._client.heartbeat()

    def _unavailable(self, detail: str) -> ProbeResult:
        return ProbeResult(name=self.name, status=Status.UNAVAILABLE, detail=detail)
