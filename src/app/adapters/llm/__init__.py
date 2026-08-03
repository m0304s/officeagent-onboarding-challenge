"""Codex app-server 위의 답변 생성 어댑터.

`session.py` 가 프로세스와 JSON-RPC 를, `pool.py` 가 세션 수명을, `codex.py` 가 턴 하나를
맡는다. 셋으로 가른 근거와 `exec` 를 쓰지 않는 실측은 `ARCHITECTURE.md` 에 있다.
"""

from app.adapters.llm.codex import CodexAnswerGenerator, TurnReader
from app.adapters.llm.pool import SessionPool
from app.adapters.llm.session import AppServerSession, SessionLaunch, parse_message

__all__ = [
    "AppServerSession",
    "CodexAnswerGenerator",
    "SessionLaunch",
    "SessionPool",
    "TurnReader",
    "parse_message",
]
