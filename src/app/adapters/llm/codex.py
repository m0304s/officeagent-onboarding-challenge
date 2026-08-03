"""`AnswerGenerator` 구현 — 턴 하나의 수명, 델타 파싱, 타임아웃, 인증 판정.

판정 전부가 `TurnReader` 라는 순수 상태 기계에 모여 있어 실물 CLI 없이 회귀를 고정한다.
에이전트를 텍스트 생성기로 좁히는 조치는 `ARCHITECTURE.md` 에 있다.
"""

import asyncio
import contextlib
import logging
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from app.adapters.llm.pool import SessionPool
from app.adapters.llm.session import AppServerSession
from app.core.exceptions import LlmGenerationFailed, LlmTimeout, LlmUnauthenticated

logger = logging.getLogger(__name__)

_THREAD_START = "thread/start"
_TURN_START = "turn/start"
_TURN_INTERRUPT = "turn/interrupt"
_TURN_COMPLETED = "turn/completed"
_AGENT_MESSAGE_DELTA = "item/agentMessage/delta"
_ITEM_COMPLETED = "item/completed"
_ERROR = "error"

_AGENT_MESSAGE = "agentMessage"
_UNAUTHORIZED = 401


class TurnReader:
    """턴 하나의 알림 스트림을 읽는 상태 기계. 프로세스도 큐도 모른다.

    델타를 글자 그대로 내보내고, 완성본은 델타가 0건일 때만 쓰는 degrade 안전판이다."""

    def __init__(self) -> None:
        self._deltas: dict[str, int] = {}
        self._completed = False

    @property
    def completed(self) -> bool:
        """`turn/completed` 를 받았는가 — 세션을 반납해도 되는가와 같은 질문이다.

        성패와 무관하다. 반납의 조건은 "확실히 끝났는가"뿐이다."""
        return self._completed

    def feed(self, method: str, params: Mapping[str, Any]) -> list[str]:
        """알림 하나를 먹고 내보낼 텍스트 조각들을 돌려준다."""
        if method == _AGENT_MESSAGE_DELTA:
            item_id = _text(params.get("itemId"))
            self._deltas[item_id] = self._deltas.get(item_id, 0) + 1
            delta = _text(params.get("delta"))
            return [delta] if delta else []

        if method == _ITEM_COMPLETED:
            return self._completed_item(params)

        if method == _ERROR:
            self._fail(params)

        if method == _TURN_COMPLETED:
            self._completed = True
            turn = params.get("turn")
            status = turn.get("status") if isinstance(turn, Mapping) else None
            if status != "completed":
                # `turn/failed` 라는 알림은 없다 — 메서드 이름만 보면 실패를 성공으로 센다.
                logger.warning("생성 턴이 실패로 끝났습니다 (status=%s)", status)
                raise LlmGenerationFailed("생성이 완료되지 못했습니다")

        return []

    def _completed_item(self, params: Mapping[str, Any]) -> list[str]:
        item = params.get("item")
        if not isinstance(item, Mapping) or item.get("type") != _AGENT_MESSAGE:
            return []
        item_id = _text(item.get("id"))
        text = _text(item.get("text"))
        if self._deltas.get(item_id, 0) or not text:
            return []
        logger.warning("델타 없이 완성본으로 도착했습니다 — 스트리밍이 퇴화했습니다")
        return [text]

    def _fail(self, params: Mapping[str, Any]) -> None:
        """`error` 알림의 처분. 401 이면 즉시 끊고, 나머지는 CLI 의 재시도에 맡긴다."""
        error = params.get("error")
        error = error if isinstance(error, Mapping) else {}
        will_retry = bool(params.get("willRetry"))

        if http_status(error) == _UNAUTHORIZED:
            # `willRetry` 와 무관하게 즉시 끊는다 — CLI 에 맡기면 자체 재시도 10회를
            # 기다려 18.6초를 쓰는데, 백오프를 돌아도 자격증명은 생기지 않는다.
            raise LlmUnauthenticated("생성기가 인증되지 않았습니다")

        if will_retry:
            # 여기서 예외로 바꾸면 CLI 의 재시도와 우리 재시도가 이중으로 걸린다.
            logger.info("생성기가 자체 재시도 중입니다")
            return
        logger.warning("생성기가 오류를 보고했습니다: %s", error.get("message"))
        raise LlmGenerationFailed("생성에 실패했습니다")


def http_status(error: Mapping[str, Any]) -> int | None:
    """`error` 알림에서 HTTP 상태를 꺼낸다. 없으면 `None`.

    `codexErrorInfo` 가 객체가 아닌 회차가 있고, 변형 이름은 가장 먼저 바뀔 부분이다."""
    info = error.get("codexErrorInfo")
    if not isinstance(info, Mapping):
        return None
    for detail in info.values():
        if isinstance(detail, Mapping) and isinstance(detail.get("httpStatusCode"), int):
            return int(detail["httpStatusCode"])
    return None


class CodexAnswerGenerator:
    """`codex app-server` 세션 위에서 턴 하나를 돌려 답변 조각을 흘린다.

    세션은 풀의 자산이라 타임아웃도 취소도 프로세스가 아니라 턴을 끊는다."""

    def __init__(
        self,
        pool: SessionPool,
        *,
        workspace: Path | None = None,
        model: str = "",
        interrupt_grace_seconds: float = 2.0,
    ) -> None:
        self._pool = pool
        self._workspace = workspace
        self._model = model
        self._interrupt_grace_seconds = interrupt_grace_seconds

    async def generate(self, prompt: str, *, timeout_seconds: float) -> AsyncIterator[str]:
        """프롬프트 하나로 턴 하나를 돌린다. 실패는 도메인 예외 셋 중 하나로 나간다."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        reader = TurnReader()
        session = await self._pool.acquire()
        thread_id: str | None = None
        try:
            try:
                started = await _before(
                    session.request(_THREAD_START, self._thread_params()), deadline
                )
                thread_id = _thread_id(started)
                # 구독이 `turn/start` 보다 먼저다 — 뒤면 그 사이 델타가 버려진다.
                queue = session.subscribe(thread_id)
                turn = session.request(_TURN_START, self._turn_params(thread_id, prompt))
                await _before(turn, deadline)

                while not reader.completed:
                    message = await _before(queue.get(), deadline)
                    if message is None:
                        raise LlmGenerationFailed("생성기 세션이 턴 도중에 종료됐습니다")
                    for text in reader.feed(*message):
                        yield text
            except TimeoutError as exc:
                raise LlmTimeout("생성이 상한 안에 끝나지 않았습니다") from exc
        finally:
            # 타임아웃과 취소가 같은 정리를 요구한다 — 취소 경로를 빠뜨리면 사용자가
            # 답을 보다 말 때마다 세션 하나가 어중간한 상태로 남는다.
            await _shielded(self._settle(session, thread_id, reader.completed))

    # ── 턴 파라미터 ─────────────────────────────────────────────────────

    def _thread_params(self) -> dict[str, Any]:
        """에이전트를 좁히는 네 가지 — 빈 `cwd`·읽기 전용·비대화형 승인·세션 파일 없음.

        비대화형이 아니면 승인 프롬프트 대기가 타임아웃의 가장 흔한 원인이 된다."""
        params: dict[str, Any] = {
            "cwd": str(self._empty_workspace()),
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "ephemeral": True,
        }
        if self._model:
            params["model"] = self._model
        return params

    def _turn_params(self, thread_id: str, prompt: str) -> dict[str, Any]:
        return {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]}

    def _empty_workspace(self) -> Path:
        """에이전트에게 줄 빈 작업 디렉터리. 첫 호출에 만든다.

        지연 생성인 것은 객체 생성만으로 파일시스템을 건드리지 않기 위해서다."""
        if self._workspace is None:
            self._workspace = Path(tempfile.mkdtemp(prefix="qa-codex-"))
        return self._workspace

    # ── 정리 ────────────────────────────────────────────────────────────

    async def _settle(
        self, session: AppServerSession, thread_id: str | None, completed: bool
    ) -> None:
        """턴을 끝내고 세션의 거취를 정한다 — 애매하면 버린다.

        반쯤 끝난 턴을 반납하면 다음 요청이 이전 턴의 델타를 받는다."""
        if thread_id is not None:
            if not completed:
                completed = await self._interrupt(session, thread_id)
            session.unsubscribe(thread_id)

        if completed and session.alive:
            await self._pool.release(session)
        else:
            if session.stderr_tail:
                logger.debug("폐기하는 세션의 stderr 마지막 줄: %s", session.stderr_tail[-1])
            await self._pool.discard(session)

    async def _interrupt(self, session: AppServerSession, thread_id: str) -> bool:
        """턴을 끊고 유예 안에 `turn/completed` 가 오는지 본다. 프로세스는 죽이지 않는다.

        중단의 성공이 응답이 아니라 후속 알림으로 관측되어 응답을 기다리지 않는다."""
        if not session.alive:
            return False
        queue = session.subscribe(thread_id)
        try:
            pending = await session.send(_TURN_INTERRUPT, {"threadId": thread_id})
        except LlmGenerationFailed:
            return False
        _drop(pending)

        deadline = asyncio.get_running_loop().time() + self._interrupt_grace_seconds
        while True:
            try:
                message = await _before(queue.get(), deadline)
            except TimeoutError:
                logger.debug("중단 요청에 유예 안에 응답이 없어 세션을 폐기합니다")
                return False
            if message is None:
                return False
            if message[0] == _TURN_COMPLETED:
                return True


def _thread_id(started: Mapping[str, Any]) -> str:
    thread = started.get("thread")
    identifier = thread.get("id") if isinstance(thread, Mapping) else None
    if not isinstance(identifier, str) or not identifier:
        raise LlmGenerationFailed("생성기가 대화 식별자를 돌려주지 않았습니다")
    return identifier


async def _before(awaitable: Any, deadline: float) -> Any:
    """마감까지만 기다린다.

    한 시도의 예산 하나를 나눠 쓴다 — 각자 상한을 가지면 합이 예산을 넘는다."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    async with asyncio.timeout(remaining):
        return await awaitable


async def _shielded(coro: Any) -> None:
    """취소가 정리를 잘라먹지 못하게 한다.

    취소된 `finally` 안에서 그냥 `await` 하면 중단 요청을 보내기도 전에 정리가 끝난다."""
    task = asyncio.ensure_future(coro)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.shield(task)


def _drop(future: "asyncio.Future[Any]") -> None:
    """결과를 쓰지 않을 future 의 예외를 조용히 소비한다 (경고 로그만 남지 않게)."""

    def _consume(done: "asyncio.Future[Any]") -> None:
        with contextlib.suppress(BaseException):
            done.exception()

    future.add_done_callback(_consume)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
