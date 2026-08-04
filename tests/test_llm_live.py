"""실물 층 — 설치된 `codex app-server` 가 실제로 답을 만드는가.

기본 실행에서 빠진다(`-m llm`). 나머지 층이 페이크와 저장된 샘플 위에 서 있어, 프로세스
인자 형태와 JSON-RPC 스키마 변화는 여기서만 드러난다.
"""

import os
from pathlib import Path

import pytest

from app.adapters.llm import AppServerSession, CodexAnswerGenerator, SessionLaunch, SessionPool
from app.config import llm_environment
from app.core.documents import ChunkLocation, DocumentFormat
from app.core.prompting import Verdict, build_prompt, parse_answer
from app.core.retrieval import ScoredChunk

pytestmark = pytest.mark.llm

QUESTION = "교육비는 얼마까지 지원되나요?"

SOURCE = ScoredChunk(
    document_id="doc-live",
    revision="live",
    index_signature="live",
    chunk_index=0,
    text="임직원은 연간 최대 200만원까지 직무 관련 교육비를 지원받을 수 있습니다.",
    location=ChunkLocation(char_start=0, char_end=40),
    filename="company-policy.txt",
    format=DocumentFormat.TXT,
    score=0.9,
)


def credentials_are_present() -> bool:
    """자격증명 파일이 있는지만 본다. CLI 는 부르지 않는다."""
    home = os.environ.get("CODEX_HOME") or f"{os.environ.get('HOME', '')}/.codex"
    return Path(home, "auth.json").is_file()


needs_credentials = pytest.mark.skipif(
    not credentials_are_present(),
    reason=(
        "codex 자격증명이 없습니다 — `docker compose up` 이 한 번 돌아야 "
        "`.secrets/codex/auth.json` 이 생깁니다"
    ),
)


@pytest.fixture
async def live(tmp_path: Path):
    """실물 세션 풀 위의 생성기. 상한 1이라 프로세스가 하나만 뜬다."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launch = SessionLaunch(env=llm_environment(), cwd=workspace, startup_timeout_seconds=60.0)
    pool = SessionPool(lambda: AppServerSession.start(launch), size=1)
    try:
        yield CodexAnswerGenerator(pool, workspace=workspace), pool
    finally:
        await pool.aclose()


@needs_credentials
async def test_실물_CLI가_근거_안에서_답을_만든다(live):
    """계약 넷을 한 번에 본다 — 델타가 여럿 오는가, 형식을 지키는가, 근거를 인용하는가.

    나눠 두면 턴을 그만큼 더 돌게 되고, 한 턴이 5~17초라 실행 시간이 그대로 곱해집니다."""
    generator, _ = live
    prompt = build_prompt(QUESTION, [SOURCE])

    pieces = [piece async for piece in generator.generate(prompt, timeout_seconds=120.0)]

    raw = "".join(pieces)
    parsed = parse_answer(raw, source_count=1)

    assert len(pieces) > 1, f"델타가 한 조각으로 왔다 — 스트리밍이 성립하지 않는다: {raw!r}"
    assert parsed.verdict_line_present, f"판정 줄이 없다: {raw!r}"
    assert parsed.verdict is Verdict.ANSWERABLE, raw
    assert parsed.has_body
    assert "200" in parsed.body, f"근거에 있는 금액이 답변에 없다: {parsed.body!r}"
    assert parsed.markers == (1,), f"근거를 인용하지 않았다: {parsed.body!r}"


@needs_credentials
async def test_두_번째_턴은_세션을_재사용한다(live):
    """기동 비용이 첫 요청에만 붙는다는 것이 세션 풀의 근거다.

    반납된 세션이 그대로 다시 쓰이는지는 풀의 유휴 수로만 관측됩니다."""
    generator, pool = live
    prompt = build_prompt(QUESTION, [SOURCE])

    async for _ in generator.generate(prompt, timeout_seconds=120.0):
        pass
    assert pool.idle == 1, "첫 턴이 끝났는데 세션이 반납되지 않았다"

    second = [piece async for piece in generator.generate(prompt, timeout_seconds=120.0)]

    assert "".join(second).strip(), "두 번째 턴이 빈 답변으로 끝났다"
    assert pool.idle == 1
