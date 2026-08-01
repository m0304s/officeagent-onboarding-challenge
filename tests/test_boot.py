"""기동 경로가 LLM 제공자 자격증명을 건드리지 않는다는 성질을 고정한다.

specs "서비스는 외부 LLM 제공자 인증 없이 기동한다"의 회귀 테스트다. 인증 정보가 없거나
손상되어 있어도 기동과 헬스 보고가 성립해야 한다.

자격증명을 **읽을 수 없는 상태**로 만들어 두는 것이 이 테스트의 핵심이다. 누군가 나중에
부팅 경로에 자격증명 파싱을 끼워 넣으면 여기서 터진다. "지금은 안 읽는다"를 눈으로
확인하는 것과, 앞으로도 안 읽는 것을 강제하는 것은 다르다.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def home_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """자격증명이 아예 없는 홈 디렉터리."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def home_with_corrupted_credentials(home_without_credentials: Path) -> Path:
    """자격증명이 존재하지만 판독 불가능한 홈 디렉터리."""
    target = home_without_credentials / ".claude" / ".credentials.json"
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


SYNC_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-credentials.sh"

SAMPLE_BLOB = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "test-access",
            "refreshToken": "test-refresh",
            "expiresAt": 0,
            "refreshTokenExpiresAt": 0,
            "scopes": [],
            "subscriptionType": "max",
        }
    }
)


def _run_sync(
    secrets_dir: Path, home: Path, keychain_blob: str | None
) -> subprocess.CompletedProcess:
    """호스트의 진짜 자격증명을 건드리지 않고 동기화 스크립트를 실행한다.

    macOS 는 Keychain 조회를, Linux 는 파일 읽기를 하므로 대역을 거는 지점이 다르다.
    """
    env = dict(os.environ, SECRETS_DIR=str(secrets_dir), HOME=str(home))

    if sys.platform == "darwin":
        # `security` 를 PATH 앞쪽의 대역으로 가린다. 블롭이 없으면 실패하는 대역이다.
        stub_dir = secrets_dir.parent / "bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub = stub_dir / "security"
        if keychain_blob is None:
            stub.write_text("#!/bin/sh\nexit 44\n", encoding="utf-8")
        else:
            stub.write_text(f"#!/bin/sh\nprintf '%s' '{keychain_blob}'\n", encoding="utf-8")
        stub.chmod(0o755)
        env["PATH"] = f"{stub_dir}:{env['PATH']}"
    else:
        source = home / ".claude" / ".credentials.json"
        if keychain_blob is not None:
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(keychain_blob, encoding="utf-8")
        env["CLAUDE_CREDENTIALS_PATH"] = str(source)

    return subprocess.run(
        ["bash", str(SYNC_SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def test_동기화는_호스트의_기존_자격증명을_꺼내온다(tmp_path: Path):
    secrets_dir = tmp_path / "secrets"

    result = _run_sync(secrets_dir, home=tmp_path / "home", keychain_blob=SAMPLE_BLOB)

    assert result.returncode == 0, result.stderr
    target = secrets_dir / ".credentials.json"
    assert json.loads(target.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"]
    # 자격증명 파일이므로 소유자 외에는 아무도 못 읽어야 한다.
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700


def test_동기화는_자격증명이_없어도_기동을_막지_않는다(tmp_path: Path):
    """인증 부재가 기동을 막으면 안 된다.

    스크립트가 0이 아닌 코드로 끝나면 `make up` 이 컨테이너를 띄우기 전에 멈춘다.
    자격증명이 없는 평가자 환경에서 서비스가 아예 뜨지 않게 되므로 실패로 끝내지 않는다.
    """
    secrets_dir = tmp_path / "secrets"

    result = _run_sync(secrets_dir, home=tmp_path / "home", keychain_blob=None)

    assert result.returncode == 0, result.stderr
    # 마운트 지점은 자격증명이 없어도 있어야 한다. 없으면 도커가 root 소유로 만든다.
    assert secrets_dir.is_dir()
    assert not (secrets_dir / ".credentials.json").exists()


def test_동기화는_새_토큰을_발급하지_않는다():
    """design 결정 5-1의 제약을 스크립트 본문에 고정한다.

    `claude setup-token`·`claude login` 은 새 토큰을 발급하므로 "기존 자격증명 재사용"
    제약을 어긴다. 편의를 위해 슬쩍 들어가기 쉬운 명령이라 회귀로 막아둔다.
    """
    body = SYNC_SCRIPT.read_text(encoding="utf-8")
    executable_lines = [
        line for line in body.splitlines() if line.strip() and not line.strip().startswith("#")
    ]

    assert not [
        line for line in executable_lines if "setup-token" in line or "claude login" in line
    ]
