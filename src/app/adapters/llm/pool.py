"""세션 풀 — 지연 기동, 상한, 죽은 세션 교체.

이 표면에서 새로 생기는 위험(죽은 세션·상한 대기·기동 실패)이 전부 여기 있어 독립
부품이다. 재사용 근거와 지연 기동이 지키는 부팅 계약은 `ARCHITECTURE.md` 에 있다.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.adapters.llm.session import AppServerSession

logger = logging.getLogger(__name__)


class SessionPool:
    """세션 몇 개를 빌려주고 돌려받는다.

    상한에 걸린 요청은 실패하지 않고 대기하고, 상한은 `qa_concurrency` 와 같은 값이다."""

    def __init__(
        self,
        factory: Callable[[], Awaitable[AppServerSession]],
        *,
        size: int,
    ) -> None:
        if size < 1:
            raise ValueError("세션 풀의 상한은 1 이상이어야 한다")
        self._factory = factory
        self._slots = asyncio.Semaphore(size)
        self._idle: list[AppServerSession] = []
        self._live: set[AppServerSession] = set()
        self._closed = False

    @property
    def idle(self) -> int:
        """반납되어 대기 중인 세션 수. 테스트와 로그가 재사용을 관측하는 창이다."""
        return len(self._idle)

    async def acquire(self) -> AppServerSession:
        """세션 하나를 빌린다. 상한에 걸리면 자리가 날 때까지 기다린다.

        빌려줄 때 생존을 확인한다 — 안 하면 사망이 지연으로 둔갑해 기록된다."""
        if self._closed:
            raise RuntimeError("닫힌 풀에서 세션을 빌릴 수 없다")
        await self._slots.acquire()
        try:
            while self._idle:
                session = self._idle.pop()
                if session.alive:
                    return session
                logger.debug("죽은 세션을 폐기하고 새로 기동합니다")
                self._live.discard(session)
                await session.close()
            session = await self._factory()
        except BaseException:
            self._slots.release()
            raise
        self._live.add(session)
        return session

    async def release(self, session: AppServerSession) -> None:
        """세션을 돌려받는다. 죽어 있으면 반납이 아니라 폐기다.

        호출자는 턴이 끝났을 때만 부른다 — 애매하면 다음 요청이 이전 턴의 델타를 받는다."""
        if self._closed or not session.alive:
            await self.discard(session)
            return
        self._idle.append(session)
        self._slots.release()

    async def discard(self, session: AppServerSession) -> None:
        """세션을 버린다. 자리는 언제나 돌려준다.

        `finally` 인 이유는 회수가 실패해도 상한이 영구히 줄어들면 곤란하기 때문이다."""
        self._live.discard(session)
        try:
            await session.close()
        finally:
            self._slots.release()

    async def aclose(self) -> None:
        """풀 전체를 회수한다. 종료 훅이 부른다 — 안 부르면 컨테이너에 자식이 남는다."""
        self._closed = True
        sessions = list(self._live)
        self._idle.clear()
        self._live.clear()
        if sessions:
            await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
