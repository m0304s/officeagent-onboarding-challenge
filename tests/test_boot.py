"""기동 경로 — 무엇이 기동 조건이고 무엇이 아닌가.

두 묶음이다. **LLM 제공자 자격증명**은 기동 경로가 아예 건드리지 않아야 하고(specs
"서비스는 외부 LLM 제공자 인증 없이 기동한다"), **임베딩 모델 선로딩**은 기동 경로에
있지만 실패해도 기동을 막지 않아야 한다.

앞 묶음: 인증 정보가 없거나 손상되어 있어도 기동과 헬스 보고가 성립해야 한다.

자격증명을 **읽을 수 없는 상태**로 만들어 두는 것이 이 테스트의 핵심이다. 누군가 나중에
부팅 경로에 자격증명 파싱을 끼워 넣으면 여기서 터진다. "지금은 안 읽는다"를 눈으로
확인하는 것과, 앞으로도 안 읽는 것을 강제하는 것은 다르다.
"""

import json
import logging
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.llm import AppServerSession
from app.config import Settings
from app.main import create_app
from tests.conftest import booted
from tests.stubs import FakeEmbedder, ScriptedGenerator


@pytest.fixture
def home_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """자격증명이 아예 없는 홈 디렉터리."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    return home


@pytest.fixture
def home_with_corrupted_credentials(home_without_credentials: Path) -> Path:
    """자격증명이 존재하지만 판독 불가능한 홈 디렉터리."""
    target = home_without_credentials / ".codex" / "auth.json"
    target.write_text("{ 이건 JSON 이 아니다 \x00\xff", encoding="utf-8", errors="replace")
    return home_without_credentials


async def _get_health(settings: Settings, probes) -> tuple[int, dict]:
    app = create_app(settings=settings, probes=probes)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    return response.status_code, response.json()


async def test_기동은_자격증명_없이_성립한다(settings, healthy_probes, home_without_credentials):
    status_code, body = await _get_health(settings, healthy_probes)

    assert status_code == 200
    assert body["status"] == "ok"


async def test_기동은_자격증명이_손상되어도_성립한다(
    settings, healthy_probes, home_with_corrupted_credentials
):
    status_code, body = await _get_health(settings, healthy_probes)

    assert status_code == 200
    assert body["status"] == "ok"


async def test_헬스는_LLM_제공자를_의존성으로_보고하지_않는다(
    settings, healthy_probes, home_with_corrupted_credentials
):
    """자격증명이 손상되어 있어도 헬스가 그것을 근거로 불가용을 보고하지 않는다.

    LLM 제공자는 답변 생성 시점에만 필요하다. 헬스 의존성 목록에 끼면 인증이 없는 평가자
    환경에서 서비스가 항상 불가용으로 보인다.
    """
    _, body = await _get_health(settings, healthy_probes)

    assert set(body["dependencies"]) == {"cache", "vector_store"}


@pytest.mark.parametrize("home", ["home_without_credentials", "home_with_corrupted_credentials"])
async def test_기동_중에는_생성기가_불리지_않는다(make_app, request, home):
    """자격증명 상태가 어느 쪽이든 기동 경로는 생성기에 닿지 않는다.

    닿으면 인증이 없는 평가자 환경에서 기동이 실패하거나 느려진다 — 그 실패는 답변
    기능 하나가 아니라 서비스 전체를 잃는 형태로 나타난다.
    """
    request.getfixturevalue(home)
    generator = ScriptedGenerator()

    async with booted(make_app(generator=generator)) as client:
        assert (await client.get("/health")).status_code == 200

    assert generator.calls == 0, "기동 경로가 생성기를 불렀다"
    assert generator.open_turns == 0


async def test_기본_배선의_풀은_첫_요청까지_세션을_띄우지_않는다(
    settings, healthy_probes, home_without_credentials, monkeypatch
):
    """지연 기동을 **세션 기동 자체를 금지해** 확인한다.

    대역을 주입하면 "우리가 준 대역을 안 불렀다"까지만 확인되고, 기본 배선이 무엇을
    하는지는 미확인으로 남는다 — 정작 평가자 환경에서 도는 것은 그쪽이다.
    """

    async def refuse(_launch):
        raise AssertionError("기동 경로가 app-server 세션을 띄웠다")

    monkeypatch.setattr(AppServerSession, "start", refuse)

    app = create_app(settings=settings, probes=healthy_probes)
    async with booted(app) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert app.state.session_pool is not None, "기본 배선에 세션 풀이 없다"
    assert app.state.session_pool.idle == 0


async def test_종료가_세션_풀을_회수한다(settings, healthy_probes, home_without_credentials):
    """회수하지 않으면 컨테이너에 자식 프로세스가 남는다.

    닫힌 풀이 세션을 더 빌려주지 않는다는 사실로 관측한다 — 프로세스가 뜬 적 없는
    상태에서도 성립하는 유일한 창이다."""
    app = create_app(settings=settings, probes=healthy_probes)
    async with booted(app) as client:
        assert (await client.get("/health")).status_code == 200

    with pytest.raises(RuntimeError):
        await app.state.session_pool.acquire()


async def test_기본_배선은_외부_서비스가_없어도_헬스에_응답한다(settings, home_without_credentials):
    """대역이 아니라 **실제 프로브**로 배선한 앱을, 아무 외부 서비스 없이 띄운다.

    프로브가 생성자에서 연결을 맺으면 캐시가 뜨기 전에는 앱이 만들어지지 않는다. 그러면
    "의존성이 불능이어도 헬스는 응답한다"는 요구사항 자체가 성립할 수 없다. 나머지
    헬스 테스트는 전부 대역을 쓰므로, 실제 배선을 확인하는 곳은 여기뿐이다.
    """
    status_code, body = await _get_health(settings, probes=None)

    # 캐시가 없으므로 불가용이다. 중요한 것은 **응답이 돌아왔다는 사실**이다.
    assert status_code == 503
    assert set(body["dependencies"]) == {"cache", "vector_store"}
    assert body["dependencies"]["cache"]["status"] == "unavailable"


# ── 임베딩 모델 선로딩 ───────────────────────────────────────────────────
#
# 미리 올리지 않으면 비용이 사라지는 게 아니라 **첫 업로드에게 청구된다.** 다만 선로딩은
# 기동 *조건*이 아니다 — 실패해도 서비스는 뜨고, 지연 로딩이 백스톱으로 남는다.


async def test_startup_warms_up_the_embedder(make_app):
    """`isinstance` 로 구체 어댑터를 확인해 부르면 배선이 구현체를 알게 된다.

    프로토콜 메서드로 두었으므로 대역도 같은 요청을 받는다. 그 사실을 여기서 고정한다.
    """
    embedder = FakeEmbedder()

    async with booted(make_app(embedder=embedder)) as client:
        assert (await client.get("/health")).status_code == 200

    assert embedder.warm_ups == 1, "기동 훅이 선로딩을 부르지 않았다"


@contextmanager
def capture(logger_name: str):
    """지정한 로거의 레코드를 모은다.

    `caplog` 를 쓰지 않는 이유: `create_app` 이 `configure_logging` 으로 루트 핸들러를
    **비우므로**, 테스트 본문에서 앱을 만드는 순간 캡처 핸들러가 함께 걷힌다. 앱을
    픽스처에서 만드는 다른 테스트들은 그 영향을 받지 않지만, 여기서는 임베더를 바꿔
    앱을 직접 만들어야 한다.
    """
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


async def test_a_failing_warm_up_does_not_stop_startup(make_app):
    """가중치가 없거나 디스크가 부족해도 서비스는 떠야 한다.

    `openspec/specs/service-health/spec.md` 의 "설정을 전혀 제공하지 않아도 기동에
    성공한다"가 선로딩이 생겼다고 약해지지 않는다. 그리고 **첫 업로드는 여전히
    성공해야 한다** — 지연 로딩이 백스톱이기 때문이다.
    """
    embedder = FakeEmbedder(warm_up_error=RuntimeError("가중치를 찾지 못했습니다"))
    # 앱을 먼저 만든다 — `create_app` 이 로깅을 다시 구성하므로, caplog 안에서 만들면
    # 캡처 핸들러가 그 자리에서 걷힌다.
    app = make_app(embedder=embedder)

    with capture("app.main") as records:
        async with booted(app) as client:
            health = await client.get("/health")
            upload = await client.post(
                "/documents",
                files={"file": ("policy.txt", "교육비를 지원합니다.".encode(), "text/plain")},
            )

    assert health.status_code == 200
    assert upload.status_code == 201, "선로딩 실패가 수집까지 막았다 — 백스톱이 없다"
    assert embedder.warm_ups == 1
    assert any("선로딩에 실패" in record.getMessage() for record in records), (
        "무엇이 준비되지 않았는지가 로그에 남지 않았다"
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-credentials.sh"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

SAMPLE_BLOB = json.dumps(
    {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {"id_token": "test-id", "access_token": "test-access", "account_id": "acct"},
    }
)

#: 스크립트가 컨테이너 안에서 보게 되는 경로. `docker-compose.yml` 의 `auth` 서비스가
#: 거는 마운트와 같아야 한다 — 아래 계약 테스트가 그 일치를 지킨다.
HOST_MOUNT = "/host-codex"
SECRETS_MOUNT = "/secrets"

pytestmark_posix = pytest.mark.skipif(
    sys.platform == "win32",
    reason="동기화 스크립트는 리눅스 컨테이너에서 도는 sh 다 — 검증도 POSIX 셸에서 한다",
)


def _run_sync(tmp_path: Path, host_auth: str | None, existing_copy: str | None):
    """동기화 스크립트를 컨테이너와 같은 모양으로 돌린다.

    스크립트는 `/host-codex`·`/secrets` 절대경로를 쓰므로 임시 디렉터리를 그대로 줄 수
    없다. 두 상수만 임시 경로로 치환한 사본을 만들어 돌린다 — 로직은 손대지 않는다.
    """
    host_dir = tmp_path / "host-codex"
    secrets_dir = tmp_path / "secrets"
    host_dir.mkdir()

    if host_auth is not None:
        (host_dir / "auth.json").write_text(host_auth, encoding="utf-8")
    if existing_copy is not None:
        secrets_dir.mkdir()
        (secrets_dir / "auth.json").write_text(existing_copy, encoding="utf-8")

    body = SYNC_SCRIPT.read_text(encoding="utf-8")
    body = body.replace(HOST_MOUNT, str(host_dir)).replace(SECRETS_MOUNT, str(secrets_dir))
    script = tmp_path / "sync.sh"
    script.write_text(body, encoding="utf-8")

    result = subprocess.run(
        ["sh", str(script)], capture_output=True, text=True, check=False, cwd=tmp_path
    )
    return result, secrets_dir


@pytestmark_posix
def test_동기화는_호스트의_기존_자격증명을_꺼내온다(tmp_path: Path):
    result, secrets_dir = _run_sync(tmp_path, host_auth=SAMPLE_BLOB, existing_copy=None)

    assert result.returncode == 0, result.stdout + result.stderr
    target = secrets_dir / "auth.json"
    assert json.loads(target.read_text(encoding="utf-8"))["tokens"]["access_token"] == "test-access"
    # 자격증명 파일이므로 소유자 외에는 아무도 못 읽어야 한다.
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700


@pytestmark_posix
def test_동기화는_자격증명이_없어도_기동을_막지_않는다(tmp_path: Path):
    """인증 부재가 기동을 막으면 안 된다.

    `api` 가 `service_completed_successfully` 로 `auth` 에 의존한다. 스크립트가 0이 아닌
    코드로 끝나면 자격증명이 없는 평가자 환경에서 **서비스가 아예 뜨지 않는다.**
    """
    result, secrets_dir = _run_sync(tmp_path, host_auth=None, existing_copy=None)

    assert result.returncode == 0, result.stdout + result.stderr
    # 마운트 지점은 자격증명이 없어도 있어야 한다. 없으면 도커가 root 소유로 만든다.
    assert secrets_dir.is_dir()
    assert not (secrets_dir / "auth.json").exists()


@pytestmark_posix
def test_동기화는_호스트에_파일이_없다고_기존_사본을_지우지_않는다(tmp_path: Path):
    """호스트에서 로그아웃했거나 홈 경로가 안 잡힌 경우까지 인증을 잃으면 안 된다.

    "없으면 지운다"로 짜기 쉬운 자리다. 한 번 성공한 뒤 호스트 상태가 바뀌었다는 이유로
    돌던 서비스의 LLM 기능이 죽는 것은 회귀다.
    """
    result, secrets_dir = _run_sync(tmp_path, host_auth=None, existing_copy=SAMPLE_BLOB)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (secrets_dir / "auth.json").read_text(encoding="utf-8") == SAMPLE_BLOB


def test_동기화는_새_토큰을_발급하지_않는다():
    """design 결정 5-1의 제약을 스크립트 본문에 고정한다.

    `codex login` 은 새 토큰을 발급하므로 "기존 자격증명 재사용" 제약을 어긴다. 편의를
    위해 슬쩍 들어가기 쉬운 명령이라 회귀로 막아둔다.
    """
    body = SYNC_SCRIPT.read_text(encoding="utf-8")
    executable_lines = [
        line for line in body.splitlines() if line.strip() and not line.strip().startswith("#")
    ]

    assert not [line for line in executable_lines if "codex login" in line]


def _compose() -> dict:
    import yaml

    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]


def test_compose_는_자격증명_경로를_api_까지_연결한다():
    """자격증명이 붙는 경로는 파이썬 코드가 아니라 이 YAML 에만 적혀 있다.

    `auth` 가 꺼내 둔 `.secrets/codex` 를 `api` 가 Codex CLI 의 홈으로 마운트하지 않으면,
    동기화는 성공하는데 CLI 는 인증되지 않은 상태로 뜬다. 어느 파이썬 테스트도 그걸
    알아채지 못하는 자리라 여기서 계약으로 고정한다.
    """
    services = _compose()

    auth_mounts = services["auth"]["volumes"]
    assert f"./.secrets/codex:{SECRETS_MOUNT}" in auth_mounts
    assert any(mount.endswith(f":{HOST_MOUNT}:ro") for mount in auth_mounts), (
        "호스트 ~/.codex 가 읽기 전용으로 붙지 않았다"
    )
    assert "./scripts/sync-credentials.sh:/sync.sh:ro" in auth_mounts

    # `:ro` 로 붙이면 만료된 토큰을 CLI 가 갱신하지 못해 그 시점에 인증이 끊긴다.
    assert "./.secrets/codex:/home/app/.codex" in services["api"]["volumes"]


def test_compose_는_인증_준비가_끝난_뒤에_api_를_띄운다():
    services = _compose()

    assert services["api"]["depends_on"]["auth"] == {"condition": "service_completed_successfully"}
    # 한 번 돌고 끝나야 하는 서비스다. 재시작이 붙으면 위 조건이 영원히 성립하지 않는다.
    assert services["auth"]["restart"] == "no"
