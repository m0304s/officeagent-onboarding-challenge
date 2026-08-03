"""Codex app-server 위의 답변 생성 어댑터.

부품이 셋이고 경계가 분명하다.

    session.py  프로세스 하나 = 세션 하나. 기동·핸드셰이크·JSON-RPC 송수신·알림 라우팅·
                생존 판정·회수
    pool.py     세션 풀. 지연 기동, 상한, 빌려주기/반납, 죽은 세션 폐기와 교체
    codex.py    `AnswerGenerator` 구현. 턴 하나의 수명·델타 파싱·타임아웃·인증 판정

**세션이 독립 부품인 이유**는 거기에만 비동기 흐름이 둘이기 때문이다 — 우리가 보내는
요청과, 우리와 무관하게 흘러드는 알림. 이 라우팅이 어댑터에 섞이면 "델타를 어떻게 텍스트로
바꾸는가"와 "메시지를 누구에게 주는가"가 한 함수에서 얽힌다. **풀이 독립 부품인 이유**는
이 표면에서 새로 생기는 위험이 전부 거기 있어서다 — 죽은 세션, 상한 대기, 기동 실패.

`codex exec` 가 아니라 `app-server` 인 근거는 실측이다(`ARCHITECTURE.md`) — 토큰 델타가
있고, 세션을 살려 두면 요청당 지연이 12~19초에서 4.5초로 줄며, 인증 실패가 문구가 아니라
`httpStatusCode == 401` 이라는 구조화된 값으로 온다.
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
