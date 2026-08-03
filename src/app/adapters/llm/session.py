"""app-server 세션 하나 — 프로세스 수명과 stdio 위의 줄 단위 JSON-RPC.

아는 것은 메시지를 누구에게 주는가까지다. 응답은 id 로, 알림은 `params.threadId` 로
가른다. `threadId` 없는 알림과 stderr 처리 근거는 `ARCHITECTURE.md` 에 있다.
"""

import asyncio
import contextlib
import itertools
import json
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.exceptions import LlmGenerationFailed

logger = logging.getLogger(__name__)

#: 기본값(64 KiB)으로는 부족하다 — app-server 가 우리 프롬프트를 되돌려 주는데, 그것이
#: 30 KB 대라 기본 상한 근처에서 `readline` 이 죽는다. 큰 문맥에서만 나는 실패다.
_LINE_LIMIT = 4 * 1024 * 1024

#: 실패 시 로그에 남길 stderr 마지막 줄 수. 무한정 들고 있으면 오래 사는 세션에서 그
#: 자체가 누수다.
_STDERR_TAIL = 20

_CLIENT_NAME = "officeagent-document-qa"
_CLIENT_VERSION = "0.1.0"


@dataclass(frozen=True)
class SessionLaunch:
    """프로세스를 어떻게 띄우는가.

    `env` 에 기본값을 두지 않아 환경 비상속을 구조로 만들고, `cwd` 는 빈 임시 디렉터리다."""

    env: Mapping[str, str]
    cwd: Path
    command: Sequence[str] = ("codex", "app-server")
    #: 기동 + 핸드셰이크 상한. 실측 8.5초의 세 배 이상이라 정상 경로가 닿지 않는다.
    startup_timeout_seconds: float = 30.0
    #: 회수 시 `terminate()` 뒤에 주는 유예. 넘기면 `kill()` 이다.
    shutdown_grace_seconds: float = 2.0


def parse_message(line: str) -> dict[str, Any] | None:
    """JSON-RPC 한 줄을 읽는다. 읽을 수 없으면 `None`.

    깨진 줄에 세션 전체가 죽으면 정상 요청이 실패한다 — 한 줄만 버린다."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        message = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("생성기 출력에서 읽을 수 없는 줄을 건너뜁니다 (%d자)", len(stripped))
        return None
    if not isinstance(message, dict):
        logger.warning("생성기 출력의 줄이 객체가 아닙니다 — 건너뜁니다")
        return None
    return message


@dataclass
class _Turn:
    """한 thread 에 흘러드는 알림을 받는 자리. `None` 은 세션 사망 신호다.

    사망을 큐로 알리지 않으면 즉시 알 수 있는 사실이 타임아웃으로 둔갑한다."""

    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = field(default_factory=asyncio.Queue)


class AppServerSession:
    """`codex app-server` 프로세스 하나와 그 위의 JSON-RPC 대화.

    세션당 활성 턴은 하나다 — 둘이면 `turnId` 없는 알림의 주인이 모호해진다."""

    def __init__(self, process: asyncio.subprocess.Process, *, grace_seconds: float) -> None:
        self._process = process
        self._grace_seconds = grace_seconds
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._turns: dict[str, _Turn] = {}
        self._readers: list[asyncio.Task[None]] = []
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)
        self._server_info: dict[str, Any] = {}
        self._alive = True

    # ── 기동과 핸드셰이크 ───────────────────────────────────────────────

    @classmethod
    async def start(cls, launch: SessionLaunch) -> "AppServerSession":
        """프로세스를 띄우고 핸드셰이크까지 끝낸 세션을 돌려준다.

        기동 실패를 생성 실패로 정규화하는 것은 서비스의 처분이 같기 때문이다."""
        try:
            process = await asyncio.create_subprocess_exec(
                *launch.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(launch.cwd),
                # 상속하지 않는다. 빈 것과 주지 않은 것은 다르다 — `env=None` 이면 부모
                # 환경이 통째로 간다.
                env=dict(launch.env),
                limit=_LINE_LIMIT,
            )
        except OSError as exc:
            raise LlmGenerationFailed("생성기를 실행할 수 없습니다") from exc

        session = cls(process, grace_seconds=launch.shutdown_grace_seconds)
        session._readers = [
            asyncio.create_task(session._read_stdout()),
            asyncio.create_task(session._read_stderr()),
        ]
        try:
            async with asyncio.timeout(launch.startup_timeout_seconds):
                session._server_info = await session.request("initialize", _initialize_params())
                await session.notify("initialized")
        except TimeoutError as exc:
            await session.close()
            raise LlmGenerationFailed("생성기 세션이 상한 안에 준비되지 않았습니다") from exc
        except BaseException:
            await session.close()
            raise
        return session

    # ── 상태 ────────────────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        """빌려줘도 되는 상태인가.

        EOF 와 프로세스 종료를 둘 다 본다 — 종료 코드만 보면 응답 없는 세션을 빌려준다."""
        return self._alive and self._process.returncode is None

    @property
    def server_info(self) -> dict[str, Any]:
        """`initialize` 응답. 진단용이고 응답에는 싣지 않는다."""
        return self._server_info

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """마지막 stderr 몇 줄. 실패를 로그에 남길 때만 쓴다."""
        return tuple(self._stderr_tail)

    # ── 송신 ────────────────────────────────────────────────────────────

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """요청을 보내고 응답을 기다린다. 상한은 호출자가 건다.

        한 시도의 예산을 여러 요청이 나눠 써, 요청마다 상한을 두면 합이 예산을 넘는다."""
        return await await_response(await self.send(method, params))

    async def send(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> asyncio.Future[dict[str, Any]]:
        """요청을 보내고 기다리지 않는다. 응답 자리만 걸어 둔다.

        중단의 성공은 뒤따라오는 `turn/completed` 로 관측되어 기다릴 이유가 없다."""
        identifier = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}}
            )
        except BaseException:
            self._pending.pop(identifier, None)
            raise
        return future

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """알림을 보낸다 — 응답이 없는 메시지다."""
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _write(self, payload: Mapping[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None or not self.alive:
            raise LlmGenerationFailed("생성기 세션이 살아 있지 않습니다")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            stdin.write(line.encode("utf-8"))
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            self._mark_dead("쓰기 도중 파이프가 끊겼습니다")
            raise LlmGenerationFailed("생성기 세션에 요청을 보내지 못했습니다") from exc

    # ── 알림 구독 ───────────────────────────────────────────────────────

    def subscribe(self, thread_id: str) -> asyncio.Queue[tuple[str, dict[str, Any]] | None]:
        """이 thread 로 오는 알림을 받을 큐를 연다.

        `turn/start` 보다 먼저 열어야 한다 — 뒤면 첫 델타가 주인 없이 버려진다."""
        turn = self._turns.get(thread_id)
        if turn is None:
            turn = _Turn()
            self._turns[thread_id] = turn
            if not self.alive:
                turn.queue.put_nowait(None)
        return turn.queue

    def unsubscribe(self, thread_id: str) -> None:
        self._turns.pop(thread_id, None)

    # ── 수신 ────────────────────────────────────────────────────────────

    async def _read_stdout(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # 상한을 넘긴 줄. 세션을 죽이지 않고 그 줄만 포기한다.
                    logger.warning("생성기 출력 한 줄이 상한을 넘어 건너뜁니다")
                    continue
                if not raw:
                    break
                message = parse_message(raw.decode("utf-8", errors="replace"))
                if message is not None:
                    self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 읽기 루프가 조용히 멎으면 세션이 응답 없는 좀비가 된다
            logger.warning("생성기 출력을 읽는 중 오류가 났습니다", exc_info=exc)
        finally:
            self._mark_dead("생성기가 출력을 닫았습니다")

    async def _read_stderr(self) -> None:
        """읽되 응답에는 싣지 않는다 — 안 읽으면 버퍼가 차서 세션이 조용히 멎는다."""
        stderr = self._process.stderr
        assert stderr is not None
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                try:
                    raw = await stderr.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    continue
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("생성기 stderr: %s", text)

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        identifier = message.get("id")
        if identifier is not None:
            self._resolve(identifier, message)
            return

        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            # 세션 수준 알림이다. 큐에 넣으면 남의 턴이 그것을 읽는다.
            logger.debug("세션 수준 알림 %s", method)
            return

        turn = self._turns.get(thread_id)
        if turn is None:
            logger.debug("구독자 없는 thread 의 알림 %s", method)
            return
        turn.queue.put_nowait((method, params))

    def _resolve(self, identifier: Any, message: Mapping[str, Any]) -> None:
        future = self._pending.pop(identifier, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if error is not None:
            # 서버 문구는 로그까지다. 응답에 실으면 내부 메시지가 그대로 노출된다.
            logger.warning("생성기가 요청을 거부했습니다: %s", error)
            future.set_exception(LlmGenerationFailed("생성기가 요청을 처리하지 못했습니다"))
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _mark_dead(self, reason: str) -> None:
        """사망을 기다리는 모두에게 알린다.

        안 알리면 즉시 알 수 있는 사실이 타임아웃으로 둔갑해 재시도가 잘못된 사유로 돈다."""
        if not self._alive:
            return
        self._alive = False
        logger.debug("생성기 세션이 종료됐습니다: %s", reason)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(LlmGenerationFailed("생성기 세션이 종료됐습니다"))
        self._pending.clear()
        for turn in self._turns.values():
            turn.queue.put_nowait(None)

    # ── 회수 ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """프로세스를 회수한다 — `terminate()` → 유예 → `kill()` → `wait()`.

        마지막 `wait()` 가 빠지면 세션을 갈아 끼울 때마다 좀비가 하나씩 쌓인다."""
        self._mark_dead("세션을 회수했습니다")

        stdin = self._process.stdin
        if stdin is not None and not stdin.is_closing():
            with contextlib.suppress(Exception):
                stdin.close()

        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            try:
                async with asyncio.timeout(self._grace_seconds):
                    await self._process.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._process.kill()
                await self._process.wait()
        else:
            await self._process.wait()

        for task in self._readers:
            task.cancel()
        if self._readers:
            await asyncio.gather(*self._readers, return_exceptions=True)
        self._readers = []


async def await_response(future: asyncio.Future[dict[str, Any]]) -> dict[str, Any]:
    """응답 future 를 기다리되, 취소되면 그 자리를 남기지 않는다."""
    try:
        return await future
    except asyncio.CancelledError:
        future.cancel()
        raise


def _initialize_params() -> dict[str, Any]:
    return {"clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION}}
