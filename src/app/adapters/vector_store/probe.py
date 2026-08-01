"""벡터 스토어 헬스 프로브.

임베디드 퍼시스턴트 모드라 "도달 가능"의 의미가 네트워크가 아니라 **저장 경로 접근**이다.
불능의 실제 형태는 볼륨 미마운트·권한 없음·디스크 꽉 참이다.

클라이언트 객체가 만들어졌다는 사실만으로 정상 판정하면, 볼륨이 안 붙은 채로 "정상"이
나오고 첫 업로드에서 터진다. 그래서 **실제로 써 본다.**
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path

from app.core.models import ProbeResult, Status

logger = logging.getLogger(__name__)


class VectorStoreProbe:
    """저장 경로에 실제로 쓰기를 시도하고, 임베디드 클라이언트를 초기화해 확인한다.

    파일 I/O 와 클라이언트 초기화는 블로킹이므로 스레드풀로 오프로드한다.
    """

    name = "vector_store"

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self._path = Path(path)
        self._timeout = timeout_seconds
        self._client = None  # 초기화 비용이 크므로 한 번만 만들어 재사용

    async def check(self) -> ProbeResult:
        try:
            await asyncio.wait_for(asyncio.to_thread(self._probe), timeout=self._timeout)
            return ProbeResult(name=self.name, status=Status.OK)
        except TimeoutError:
            return self._unavailable("응답 시간 초과")
        except PermissionError:
            return self._unavailable("저장 경로에 쓸 수 없음 (권한 없음)")
        except OSError as exc:
            logger.warning("벡터 스토어 저장 경로 점검 실패", exc_info=exc)
            return self._unavailable(f"저장 경로에 쓸 수 없음 ({type(exc).__name__})")
        except Exception as exc:
            logger.warning("벡터 스토어 점검 실패", exc_info=exc)
            return self._unavailable(f"점검 실패 ({type(exc).__name__})")

    def _probe(self) -> None:
        """블로킹. 스레드풀에서만 호출한다."""
        self._assert_writable()
        self._heartbeat()

    def _assert_writable(self) -> None:
        self._path.mkdir(parents=True, exist_ok=True)
        marker = self._path / f".health-{uuid.uuid4().hex}"
        try:
            marker.write_bytes(b"")
        finally:
            try:
                os.unlink(marker)
            except FileNotFoundError:
                pass

    def _heartbeat(self) -> None:
        # import 비용이 커서 모듈 최상단이 아니라 여기서 가져온다. 앱 기동을 느리게 하지 않는다.
        import chromadb

        if self._client is None:
            self._client = chromadb.PersistentClient(path=str(self._path))
        self._client.heartbeat()

    def _unavailable(self, detail: str) -> ProbeResult:
        return ProbeResult(name=self.name, status=Status.UNAVAILABLE, detail=detail)
