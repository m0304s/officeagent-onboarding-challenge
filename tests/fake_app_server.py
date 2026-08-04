"""가짜 `codex app-server` — 같은 줄 단위 JSON-RPC 를 말하는 짧은 스크립트.

실물 CLI 의 대체재가 아니라 실물이 닿지 못하는 곳이다. 핸드셰이크 무응답·턴 도중 사망·
중단 요청 무시는 인위적으로 만들 수 없다. 지시 목록은 `tests/README.md` 에 있다.
"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

THREAD_ID = "fake-thread-1"
TURN_ID = "fake-turn-1"
ITEM_ID = "fake-item-1"

#: 판정 줄이 델타 경계에 걸치는 실측 모양 그대로다.
DEFAULT_DELTAS = (
    "VER",
    "DICT",
    ": ANSW",
    "ERABLE",
    "\n",
    "교육비는 연 200만원까지 지원됩니다",
    " [1]",
)

_WRITE_LOCK = threading.Lock()
_INTERRUPTED = threading.Event()


def _emit(payload: dict[str, Any]) -> None:
    with _WRITE_LOCK:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _respond(identifier: Any, result: dict[str, Any]) -> None:
    _emit({"jsonrpc": "2.0", "id": identifier, "result": result})


def _notify(method: str, params: dict[str, Any]) -> None:
    _emit({"jsonrpc": "2.0", "method": method, "params": params})


def _turn_notify(method: str, params: dict[str, Any]) -> None:
    _notify(method, {"threadId": THREAD_ID, "turnId": TURN_ID, **params})


def _run_turn(args: argparse.Namespace) -> None:
    """턴 하나를 스크립트대로 흘린다. 중단 요청과 겹칠 수 있어 별도 스레드에서 돈다."""
    _turn_notify("turn/started", {"turn": {"id": TURN_ID, "status": "inProgress"}})

    if args.auth_401:
        # 실물과 같은 모양 — 첫 401 이 `willRetry: true` 로 오고, 그것을 기다리면 CLI 의
        # 자체 재시도 10회에 18초를 쓴다. 그래서 어댑터는 즉시 끊는다.
        _turn_notify(
            "error",
            {
                "error": {
                    "message": "Reconnecting... 1/5",
                    "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 401}},
                },
                "willRetry": True,
            },
        )
        _wait_for_interrupt(args)
        return

    if args.hang:
        _wait_for_interrupt(args)
        return

    for index, delta in enumerate(DEFAULT_DELTAS):
        if _INTERRUPTED.is_set():
            _complete("failed")
            return
        if args.slow_deltas:
            time.sleep(args.slow_deltas)
        _turn_notify("item/agentMessage/delta", {"itemId": ITEM_ID, "delta": delta})
        if args.die_mid_turn and index + 1 >= 2:
            # 버퍼를 비우고 즉사한다. 정상 종료로는 "턴 도중 사망"이 재현되지 않는다.
            sys.stdout.flush()
            os._exit(1)

    _turn_notify(
        "item/completed",
        {"item": {"type": "agentMessage", "id": ITEM_ID, "text": "".join(DEFAULT_DELTAS)}},
    )
    _complete("completed")


def _wait_for_interrupt(args: argparse.Namespace) -> None:
    """침묵한 채 중단 요청만 기다린다.

    `--ignore-interrupt` 면 끝까지 침묵한다 — 어댑터가 유예를 넘겨 세션을 폐기하는 경로다."""
    if _INTERRUPTED.wait(timeout=30) and not args.ignore_interrupt:
        _complete("failed")


def _complete(status: str) -> None:
    _turn_notify("turn/completed", {"turn": {"id": TURN_ID, "status": status}})


def _handle(message: dict[str, Any], args: argparse.Namespace) -> bool:
    method = message.get("method")
    identifier = message.get("id")

    if method == "initialize":
        if args.no_handshake:
            return True
        # 환경을 그대로 되비쳐 준다 — "상속하지 않는다"를 테스트가 관측하는 창이다.
        _respond(identifier, {"userAgent": "fake-app-server/0", "env": sorted(os.environ)})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        params = message.get("params") or {}
        _respond(identifier, {"thread": {"id": THREAD_ID}, "cwd": params.get("cwd")})
        # `threadId` 없는 세션 수준 알림. 실물이 이렇게 보낸다.
        _notify("thread/started", {"thread": {"id": THREAD_ID}})
    elif method == "turn/start":
        # 턴마다 초기화한다. 실물은 턴 하나가 독립이므로, 여기서 지우지 않으면 앞 턴의
        # 중단이 다음 턴을 시작하자마자 실패시킨다 — 가짜에만 있는 상태 누수다.
        _INTERRUPTED.clear()
        _respond(identifier, {"turn": {"id": TURN_ID, "status": "inProgress"}})
        threading.Thread(target=_run_turn, args=(args,), daemon=True).start()
    elif method == "turn/interrupt":
        if identifier is not None:
            _respond(identifier, {})
        if not args.ignore_interrupt:
            _INTERRUPTED.set()
    elif method == "shutdown":
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-handshake", action="store_true")
    parser.add_argument("--die-mid-turn", action="store_true")
    parser.add_argument("--hang", action="store_true")
    parser.add_argument("--ignore-interrupt", action="store_true")
    parser.add_argument("--auth-401", action="store_true")
    parser.add_argument("--slow-deltas", type=float, default=0.0)
    # 핸드셰이크에 실패한 세션은 파이썬 쪽에 객체가 남지 않는다 — 그 회차에 자식이
    # 회수됐는지 확인할 통로가 이것뿐이다.
    parser.add_argument("--pidfile", default="")
    args = parser.parse_args()

    if args.pidfile:
        Path(args.pidfile).write_text(str(os.getpid()), encoding="utf-8")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _handle(message, args):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
