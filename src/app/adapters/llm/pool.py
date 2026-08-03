"""세션 풀 — 지연 기동, 상한, 죽은 세션 교체.

**이 표면에서 새로 생기는 위험이 전부 여기 있다.** 요청마다 프로세스를 띄우던 시절에는
없던 것들이다: 죽은 세션을 빌려주는 경우, 상한 대기, 기동 실패. 그래서 독립 부품이고,
가짜 프로세스로 실물 CLI 없이 전부 시험한다(`tests/test_llm_pool.py`).

**세션을 재사용하는 이유는 기동 비용이 첫 요청에만 붙기 때문이다.** 실측에서
`thread/start` 첫 응답이 8.11초인데 두 번째는 0.07초다 — 그 8초는 인증·세션 준비다.
요청마다 프로세스를 띄우면 `exec` 보다 빨라지지 않고 델타만 얻는다.

**지연 기동이 부팅 계약을 지킨다.** 풀을 만드는 일이 프로세스를 띄우지 않으므로, 자격증명이
없는 환경에서도 기동과 헬스가 그대로 성립한다(`tests/test_boot.py`). 기동 시 예열하면 그
8초가 부팅 경로로 옮겨 가고, 인증이 없는 평가자 환경에서는 실패로 옮겨 간다.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.adapters.llm.session import AppServerSession

logger = logging.getLogger(__name__)


class SessionPool:
    """세션 몇 개를 빌려주고 돌려받는다.

    상한에 걸린 요청은 **실패하지 않고 대기한다.** 수집이 같은 규율을 이미 쓰고 있고,
    대기 중에도 `sources` 이벤트는 이미 나가 있으므로 사용자에게는 생성이 느린 것과
    구분되지 않는다. 상한을 오류로 바꾸면 부하가 조금 몰릴 때마다 답변이 실패한다.

    상한이 `qa_concurrency` 와 같은 값이라는 것이 중요하다 — 세마포어와 풀이 같은 수를
    두 번 세지 않고, 생성 하나당 프로세스 하나라는 관계가 유지된다.
    """

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
        """세션 하나를 빌린다. 상한에 걸리면 자리가 날 때까지 **기다린다**.

        **빌려줄 때 생존을 확인한다.** 반납 시점에 살아 있던 세션도 그 사이에 죽을 수
        있다(자격증명 갱신, CLI 자체 종료). 확인하지 않으면 그 요청이 응답 없는 세션을
        받아 상한까지 매달린 뒤 시간 초과로 실패한다 — 원인이 사망인데 사유가 지연으로
        기록된다.
        """
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

        호출자는 **턴이 확실히 끝났을 때만** 이 메서드를 부른다(`turn/completed` 를 받은
        경우). 애매한 세션을 반납하면 다음 요청이 이전 턴의 델타를 받는다 — 8초를 다시
        무는 것이 답변이 섞이는 것보다 훨씬 싸다.
        """
        if self._closed or not session.alive:
            await self.discard(session)
            return
        self._idle.append(session)
        self._slots.release()

    async def discard(self, session: AppServerSession) -> None:
        """세션을 버린다. 자리는 반드시 돌려준다.

        `finally` 로 자리를 돌려주는 이유는 회수가 실패해도 상한이 영구히 줄어들면 안
        되기 때문이다. 프로세스 하나를 못 죽인 대가가 "그 뒤로 동시 생성이 하나 줄어든
        서비스"가 되어서는 안 된다.
        """
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
