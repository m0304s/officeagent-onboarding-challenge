"""`AnswerGenerator` 구현 — 턴 하나의 수명, 델타 파싱, 타임아웃, 인증 판정.

세션이 "메시지를 누구에게 주는가"를 맡았으므로 여기 남는 것은 **그 메시지가 무슨 뜻인가**다.
그 판정 전부가 `TurnReader` 라는 순수 상태 기계에 모여 있다 — 프로세스도 큐도 모르고,
`(method, params)` 를 먹고 내보낼 텍스트 조각을 돌려주거나 도메인 예외를 던진다.

**순수하게 둔 이유는 이 층의 회귀를 실물 CLI 없이 고정하기 위해서다.** 저장해 둔 실물 알림
샘플(`tests/fixtures/codex/`)을 그대로 먹여 델타·401·종료 판정을 단언한다. 손으로 지어낸
샘플은 우리가 상상한 형식을 검증할 뿐이라, 그 파일들이 곧 "무엇을 보고 파서를 만들었는가"의
증거다.

**에이전트를 텍스트 생성기로 좁히는 조치가 이 어댑터의 핵심이다.** 대상이 도구를 쓰고 파일을
읽을 수 있는 실행체라 기본값이 우리가 원하는 것과 다르다 — 빈 작업 디렉터리, 읽기 전용
샌드박스, 비대화형 승인, 세션 파일 없음, 그리고 프롬프트는 argv 가 아니라 `turn/start` 의
`input` 으로 간다(문서 본문이 프로세스 목록에 노출되지 않고, argv 길이 상한이 문맥 크기의
숨은 천장이 되지도 않는다).
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

    **델타를 글자 그대로 내보낸다.** 합치지도 쪼개지도 않는다 — 실측에서 209자 답변이 델타
    112건으로 오고, 그 입자도가 곧 사용자가 보는 스트리밍이다.

    **`item/completed` 의 완성본은 출력에 쓰지 않는다.** 이미 델타로 다 나갔으므로 또
    내보내면 답변이 두 번 적힌다. **예외 하나** — 어떤 아이템에 델타가 0건인데 완성본에
    텍스트가 있으면 그때만 한 번 내보낸다. 델타가 사라지는 회귀가 나도 빈 답변(생성 실패로
    분류돼 헛재시도)이 아니라 `exec` 시절 동작으로 **degrade** 되게 하는 안전판이다.

    **`item.type` 으로 거르지 않으면 프롬프트를 답변으로 착각한다.** 우리가 보낸 프롬프트가
    `userMessage` 아이템으로 되돌아온다(픽스처에 그대로 들어 있다).
    """

    def __init__(self) -> None:
        self._deltas: dict[str, int] = {}
        self._completed = False

    @property
    def completed(self) -> bool:
        """`turn/completed` 를 받았는가 — **세션을 반납해도 되는가**와 같은 질문이다.

        성패와 무관하다. 실패한 턴도 끝난 턴이고, 반납의 조건은 "확실히 끝났는가"뿐이다.
        """
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
                # **`turn/failed` 라는 알림은 없다.** 실패해도 메서드는 `turn/completed`
                # 이고 성패는 여기 있다 — 메서드 이름만 보면 실패를 성공으로 센다.
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
            # **`willRetry` 와 무관하게 즉시 끊는다.** 실측에서 첫 401 이 `willRetry: true`
            # 로 왔고, 그것을 CLI 에 맡기면 자체 재시도 10회를 기다려 18.6초를 쓴다.
            # 백오프를 몇 번 돌아도 자격증명이 생기지 않는다는 논리는 CLI 의 재시도에도
            # 그대로 적용된다 — 첫 401 은 약 2초에 온다.
            raise LlmUnauthenticated("생성기가 인증되지 않았습니다")

        if will_retry:
            # 401 이 아닌 오류에만 적용한다. 여기서 예외로 바꾸면 CLI 의 재시도와 우리
            # 재시도가 이중으로 걸린다.
            logger.info("생성기가 자체 재시도 중입니다")
            return
        logger.warning("생성기가 오류를 보고했습니다: %s", error.get("message"))
        raise LlmGenerationFailed("생성에 실패했습니다")


def http_status(error: Mapping[str, Any]) -> int | None:
    """`error` 알림에서 HTTP 상태를 꺼낸다. 없으면 `None`.

    **`codexErrorInfo` 가 항상 객체는 아니다** — 마지막 `error` 알림에서는 문자열 `"other"`
    로 온다(픽스처에 그 회차가 들어 있다). 객체를 가정한 체이닝은 거기서 죽고, 그 죽음은
    인증 실패 경로에서만 나므로 정상 경로 테스트로는 잡히지 않는다.

    변형 이름(`responseStreamDisconnected` 등)을 상수로 박지 않고 값들을 훑는 이유는, 그
    이름이 실험 단계 표면에서 가장 먼저 바뀔 만한 부분이기 때문이다. 우리가 아는 사실은
    "상태 코드가 한 겹 안에 있다"까지다.
    """
    info = error.get("codexErrorInfo")
    if not isinstance(info, Mapping):
        return None
    for detail in info.values():
        if isinstance(detail, Mapping) and isinstance(detail.get("httpStatusCode"), int):
            return int(detail["httpStatusCode"])
    return None


class CodexAnswerGenerator:
    """`codex app-server` 세션 위에서 턴 하나를 돌려 답변 조각을 흘린다.

    **생성이 프로세스를 만들지 않는다.** 세션은 풀의 자산이고 이 어댑터가 만드는 것은
    thread 와 turn 뿐이다. 그래서 타임아웃도 취소도 프로세스를 죽이지 않고 턴을 끊는다 —
    스펙이 요구하는 "중단된 시도가 만든 자원을 정리한다"는 그 둘을 가리킨다.

    **객체 생성은 CLI 를 건드리지 않는다.** 풀이 지연 기동이고 작업 디렉터리도 첫 호출에
    만들어지므로, 배선이 이 어댑터를 만드는 것만으로는 프로세스도 파일도 생기지 않는다.
    """

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
                # **구독이 `turn/start` 보다 먼저다.** 뒤로 가면 그 사이에 도착한 델타가
                # 주인 없는 알림으로 버려진다.
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
            # **정리를 `finally` 에 두는 이유**는 타임아웃과 취소(순회 중단)가 같은 정리를
            # 요구하기 때문이다. 취소 경로에만 정리를 빠뜨리면 사용자가 답을 보다 말고 새
            # 질문을 보낼 때마다 세션 하나가 어중간한 상태로 남는다.
            await _shielded(self._settle(session, thread_id, reader.completed))

    # ── 턴 파라미터 ─────────────────────────────────────────────────────

    def _thread_params(self) -> dict[str, Any]:
        """에이전트를 좁히는 네 가지가 전부 여기 실린다.

        - `cwd`   빈 임시 디렉터리. 파일을 뒤질 대상 자체를 없앤다
        - `sandbox`  읽기 전용
        - `approvalPolicy`  비대화형. 승인 프롬프트를 기다리며 매달리는 것이 타임아웃의
          가장 흔한 원인이 된다
        - `ephemeral`  세션 파일을 남기지 않는다 — 요청 사이에 맥락이 새지 않는다
        """
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

        지연 생성인 이유는 배선이 이 어댑터를 만드는 것만으로 파일시스템을 건드리면 안
        되기 때문이다 — 기동 경로가 CLI 를 건드리지 않는다는 계약과 같은 자리다.
        """
        if self._workspace is None:
            self._workspace = Path(tempfile.mkdtemp(prefix="qa-codex-"))
        return self._workspace

    # ── 정리 ────────────────────────────────────────────────────────────

    async def _settle(
        self, session: AppServerSession, thread_id: str | None, completed: bool
    ) -> None:
        """턴을 끝내고 세션의 거취를 정한다 — **애매하면 버린다.**

        반납 조건은 `turn/completed` 를 받았을 때뿐이다. 그 외에는 전부 폐기다. 기동 8초가
        아깝지만, 반쯤 끝난 턴을 반납하면 다음 요청이 이전 턴의 델타를 받는다.
        """
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
        """턴을 끊고 유예 안에 `turn/completed` 가 오는지 본다. **프로세스는 죽이지 않는다.**

        중단의 성공은 응답이 아니라 뒤따라오는 `turn/completed` 로 관측된다 — 그래서 요청을
        보내고 응답을 기다리지 않는다. 유예 안에 오지 않으면 호출자가 세션을 폐기한다.
        """
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

    요청마다 상한을 새로 주지 않고 **한 시도의 예산 하나**를 나눠 쓰는 형태다. 각자 상한을
    가지면 합이 예산을 넘어, 설정된 상한보다 오래 걸리는 시도가 정상으로 취급된다.
    """
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    async with asyncio.timeout(remaining):
        return await awaitable


async def _shielded(coro: Any) -> None:
    """취소가 정리를 잘라먹지 못하게 한다.

    취소된 요청의 `finally` 안에서 그냥 `await` 하면 그 대기도 즉시 취소되어, 중단 요청을
    보내기도 전에 정리가 끝나 버린다. 방패를 씌우면 우리는 취소를 받아들이되 정리는
    끝까지 돈다 — 남는 것이 프로세스라 잘라먹으면 그대로 누수다.
    """
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
