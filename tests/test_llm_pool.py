"""세션·풀 수명 — 가짜 프로세스 위에서 고정한다.

핸드셰이크 무응답·턴 도중 사망·중단 요청 무시는 실물 CLI 로 만들 수 없어, 이 층은 실물의
대체재가 아니라 실물이 닿지 못하는 곳이다 (`tests/fake_app_server.py`).
"""

import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.adapters.llm import AppServerSession, CodexAnswerGenerator, SessionLaunch, SessionPool
from app.core.exceptions import LlmGenerationFailed, LlmTimeout, LlmUnauthenticated
from tests.fake_app_server import DEFAULT_DELTAS

FAKE_SERVER = Path(__file__).parent / "fake_app_server.py"

#: 가짜 서버(파이썬 하나)를 띄우는 데 필요한 최소 환경. 실물 배선과 같은 규율이다 —
#: 상속하지 않고 필요한 것만 넘긴다. 한글 델타가 파이프에서 깨지지 않도록 인코딩을 고정한다.
_PASSTHROUGH = ("PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "LD_LIBRARY_PATH")


def _env() -> dict[str, str]:
    inherited = {key: os.environ[key] for key in _PASSTHROUGH if key in os.environ}
    return {**inherited, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


class Launcher:
    """세션을 몇 번 띄웠는지 세는 팩토리.

    재사용을 관측하는 유일한 창이다 — 빨랐다는 것만으로는 재사용을 알 수 없다."""

    def __init__(self, cwd: Path, *flags: str, startup_timeout: float = 5.0) -> None:
        self.launch = SessionLaunch(
            env=_env(),
            cwd=cwd,
            command=(sys.executable, str(FAKE_SERVER), *flags),
            startup_timeout_seconds=startup_timeout,
            shutdown_grace_seconds=1.0,
        )
        self.starts = 0
        self.sessions: list[AppServerSession] = []

    async def __call__(self) -> AppServerSession:
        self.starts += 1
        session = await AppServerSession.start(self.launch)
        self.sessions.append(session)
        return session


@pytest.fixture
async def generators(tmp_path: Path):
    """가짜 서버를 쓰는 풀과 생성기를 만들고, 끝나면 남은 자식을 회수한다.

    여기서 새는 것이 프로세스라, 실패한 회차의 자식이 다음 테스트를 느리게 만든다."""
    pools: list[SessionPool] = []

    def _make(
        *flags: str,
        size: int = 1,
        interrupt_grace_seconds: float = 0.5,
        startup_timeout: float = 5.0,
    ) -> tuple[Launcher, SessionPool, CodexAnswerGenerator]:
        workspace = tmp_path / f"workspace-{len(pools)}"
        workspace.mkdir()
        launcher = Launcher(workspace, *flags, startup_timeout=startup_timeout)
        pool = SessionPool(launcher, size=size)
        pools.append(pool)
        generator = CodexAnswerGenerator(
            pool,
            workspace=workspace,
            interrupt_grace_seconds=interrupt_grace_seconds,
        )
        return launcher, pool, generator

    yield _make

    for pool in pools:
        await pool.aclose()


async def collect(generator: CodexAnswerGenerator, *, timeout_seconds: float = 5.0) -> list[str]:
    return [chunk async for chunk in generator.generate("질문", timeout_seconds=timeout_seconds)]


def child_is_running(session: AppServerSession) -> bool:
    """자식 프로세스가 아직 살아 있는가. 누수를 확인하는 자리다."""
    return session._process.returncode is None  # noqa: SLF001 — 누수 확인에는 이것뿐이다


# ── 지연 기동 ────────────────────────────────────────────────────────────


async def test_풀과_생성기_생성은_프로세스를_띄우지_않는다(generators):
    """부팅 경로가 CLI 를 건드리지 않는다는 계약이 여기서부터 성립한다.

    어댑터를 만드는 것만으로 프로세스가 뜨면 자격증명 없는 환경의 기동이 흔들린다."""
    launcher, pool, _ = generators()

    assert launcher.starts == 0
    assert pool.idle == 0


# ── 정상 경로 ────────────────────────────────────────────────────────────


async def test_델타를_글자_그대로_흘린다(generators):
    """조각을 쪼개지도 합치지도 않는다. 서버가 내보낸 단위가 그대로 나온다."""
    _, pool, generator = generators()

    chunks = await collect(generator)

    assert chunks == list(DEFAULT_DELTAS)
    assert pool.idle == 1, "정상 종료한 세션이 반납되지 않았다"


async def test_두_번째_요청은_새_프로세스를_띄우지_않는다(generators):
    """세션 재사용이 이 표면을 고른 이유의 절반이다 — 실측에서 기동이 8초다."""
    launcher, _, generator = generators()

    await collect(generator)
    await collect(generator)

    assert launcher.starts == 1


async def test_죽은_세션은_빌려지지_않는다(generators):
    """반납 시점에 살아 있던 세션도 그 사이에 죽는다.

    확인 없이 빌려주면 원인이 사망인데 사유가 지연으로 기록된다."""
    launcher, pool, generator = generators()
    await collect(generator)
    await launcher.sessions[0].close()  # 반납된 세션이 밖에서 죽는다

    chunks = await collect(generator)

    assert chunks == list(DEFAULT_DELTAS)
    assert launcher.starts == 2


# ── 실패 경로 ────────────────────────────────────────────────────────────


async def test_핸드셰이크_실패는_생성_실패다(generators, tmp_path: Path):
    """`initialize` 에 아무도 답하지 않는 상황 — 실물로는 만들 수 없는 실패다.

    세션 객체가 남지 않아 자식 회수를 PID 로 확인한다. 좀비는 신호 0 에 응답해 여기서 걸린다."""
    pidfile = tmp_path / "no-handshake.pid"
    launcher, _, generator = generators(
        "--no-handshake", "--pidfile", str(pidfile), startup_timeout=0.3
    )

    with pytest.raises(LlmGenerationFailed):
        await collect(generator)

    assert launcher.starts == 1
    if sys.platform != "win32":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text(encoding="utf-8")), 0)
    # 자리를 돌려주지 않으면 다음 요청이 영원히 대기한다 — 상한이 조용히 0 이 된다.
    with pytest.raises(LlmGenerationFailed):
        await asyncio.wait_for(collect(generator), timeout=5)


async def test_턴_도중_사망은_생성_실패다(generators):
    launcher, pool, generator = generators("--die-mid-turn")

    with pytest.raises(LlmGenerationFailed):
        await collect(generator)

    assert pool.idle == 0, "죽은 세션이 반납됐다"
    assert not child_is_running(launcher.sessions[0])


async def test_인증_부재는_다른_예외로_즉시_끝난다(generators):
    """`willRetry: true` 를 기다리면 18.6초다. 첫 401 을 보는 즉시 끊는다."""
    launcher, _, generator = generators("--auth-401")

    with pytest.raises(LlmUnauthenticated):
        await asyncio.wait_for(collect(generator, timeout_seconds=10), timeout=10)

    assert launcher.starts == 1


# ── 타임아웃과 중단 ─────────────────────────────────────────────────────


async def test_상한을_넘기면_시간_초과이고_프로세스는_살아_있다(generators):
    """타임아웃이 프로세스를 죽이지 않는다 — 프로세스가 풀 자산이기 때문이다.

    턴만 끊고, 유예 안에 종료가 확인되면 세션을 그대로 반납한다."""
    launcher, pool, generator = generators("--hang")

    with pytest.raises(LlmTimeout):
        await collect(generator, timeout_seconds=0.3)

    assert pool.idle == 1, "중단이 확인된 세션까지 폐기했다"
    assert child_is_running(launcher.sessions[0])
    assert launcher.starts == 1


async def test_중단을_무시하는_세션은_폐기된다(generators):
    """애매하면 버린다. 반쯤 끝난 턴을 반납하면 다음 요청이 이전 턴의 델타를 받는다."""
    launcher, pool, generator = generators("--hang", "--ignore-interrupt")

    with pytest.raises(LlmTimeout):
        await collect(generator, timeout_seconds=0.3)

    assert pool.idle == 0
    assert not child_is_running(launcher.sessions[0]), "타임아웃 뒤에 자식이 남았다"


async def test_순회를_중간에_멈추면_정리된다(generators):
    """취소는 순회 종료로 표현된다 — 사용자가 답을 보다 말고 새 질문을 보내는 흔한 조작이다.

    정리하지 않으면 그때마다 어중간한 세션이 하나씩 남는다."""
    launcher, pool, generator = generators("--slow-deltas", "0.05")

    stream = generator.generate("질문", timeout_seconds=5.0)
    first = await anext(stream)
    await stream.aclose()

    assert first == DEFAULT_DELTAS[0]
    # 중단이 확인되면 반납, 확인되지 않으면 폐기다. 어느 쪽이든 어중간하게 남지 않는다.
    assert pool.idle + len([s for s in launcher.sessions if not s.alive]) == 1
    assert await asyncio.wait_for(collect(generator), timeout=10) == list(DEFAULT_DELTAS)


# ── 상한 ─────────────────────────────────────────────────────────────────


async def test_상한에_걸린_요청은_실패하지_않고_대기한다(generators):
    """수집과 같은 규율이다. 상한을 오류로 바꾸면 부하가 조금 몰릴 때마다 답변이 실패한다.

    대기 중에도 `sources` 는 이미 나가 있으므로 사용자에게는 느린 생성과 구분되지 않는다."""
    launcher, _, generator = generators("--slow-deltas", "0.02", size=1)

    both = await asyncio.gather(collect(generator), collect(generator))

    assert both == [list(DEFAULT_DELTAS), list(DEFAULT_DELTAS)]
    # 상한이 1 이므로 두 요청이 같은 세션을 차례로 썼다는 뜻이다.
    assert launcher.starts == 1


# ── 환경 ─────────────────────────────────────────────────────────────────


async def test_환경을_상속하지_않는다(generators, monkeypatch: pytest.MonkeyPatch):
    """컨테이너 환경변수에는 저장소 접속 정보가 들어 있다.

    넘기는 것은 `HOME`·`PATH`·`CODEX_HOME` 뿐이라는 결정을 여기서 확인한다."""
    monkeypatch.setenv("APP_QA_SENTINEL", "새어 나가면 안 되는 값")
    launcher, _, generator = generators()

    await collect(generator)

    reported: Sequence[str] = launcher.sessions[0].server_info["env"]
    assert "APP_QA_SENTINEL" not in reported
