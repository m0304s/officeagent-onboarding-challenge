"""알림 파싱 — 저장된 실물 샘플(`tests/fixtures/codex/`) 위에서 고정한다.

손으로 지어낸 샘플은 우리가 상상한 형식을 검증할 뿐이다. `parse_message` 와 `TurnReader`
가 순수해 프로세스도 루프도 없이 문자열 단언만으로 선다.
"""

import json
from pathlib import Path

import pytest

from app.adapters.llm import TurnReader, parse_message
from app.adapters.llm.codex import http_status
from app.core.exceptions import LlmGenerationFailed, LlmUnauthenticated

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
ANSWERABLE = FIXTURES / "app_server_answerable.jsonl"
UNAUTHENTICATED = FIXTURES / "app_server_unauthenticated.jsonl"


def notifications(path: Path):
    """픽스처에서 알림만 뽑는다 — 응답(`id` 가 있는 메시지)은 세션이 알아서 짝짓는다."""
    for line in path.read_text(encoding="utf-8").splitlines():
        message = parse_message(line)
        if message and isinstance(message.get("method"), str):
            yield message["method"], message.get("params") or {}


def completed_text(path: Path) -> str:
    """그 회차의 `item/completed` 에 실린 답변 전문."""
    for method, params in notifications(path):
        item = params.get("item") or {}
        if method == "item/completed" and item.get("type") == "agentMessage":
            return item["text"]
    raise AssertionError("픽스처에 답변 아이템이 없다")


# ── 정상 회차 ────────────────────────────────────────────────────────────


def test_델타를_이어_붙이면_완성본과_같다():
    """이 성질이 표면을 바꾼 이유 그 자체다.

    `exec` 에는 없던 것이라, 여기서 깨지면 스트리밍이 사라졌다는 뜻이다."""
    reader = TurnReader()
    chunks: list[str] = []

    for method, params in notifications(ANSWERABLE):
        chunks.extend(reader.feed(method, params))

    assert reader.completed
    assert len(chunks) > 1, "델타가 하나로 뭉쳐 왔다 — 스트리밍이 아니다"
    assert "".join(chunks) == completed_text(ANSWERABLE)


def test_판정_줄이_델타_경계에_걸쳐_온다():
    """분리기(`core.prompting.VerdictSplitter`)가 왜 필요한지의 근거를 실물로 고정한다."""
    deltas = [
        params["delta"]
        for method, params in notifications(ANSWERABLE)
        if method == "item/agentMessage/delta"
    ]

    assert deltas[0] != "VERDICT: ANSWERABLE\n"
    assert "".join(deltas).startswith("VERDICT: ANSWERABLE\n")


def test_프롬프트가_답변으로_새지_않는다():
    """우리가 보낸 프롬프트가 `userMessage` 아이템으로 되돌아온다.

    `item.type` 으로 거르지 않으면 형식이 멀쩡한 채 프롬프트가 답변 자리에 앉는다."""
    reader = TurnReader()
    chunks: list[str] = []

    for method, params in notifications(ANSWERABLE):
        chunks.extend(reader.feed(method, params))

    joined = "".join(chunks)
    assert "[근거]" not in joined
    assert "질의응답" not in joined


def test_델타가_0건이면_완성본을_한_번_내보낸다():
    """델타가 사라지는 회귀의 안전판. 빈 답변이 아니라 `exec` 시절 동작으로 퇴화한다."""
    reader = TurnReader()

    emitted = reader.feed(
        "item/completed",
        {"item": {"type": "agentMessage", "id": "msg-1", "text": "VERDICT: ANSWERABLE\n답변"}},
    )

    assert emitted == ["VERDICT: ANSWERABLE\n답변"]


def test_델타가_있었으면_완성본을_다시_내보내지_않는다():
    """또 내보내면 답변이 두 번 적힌다."""
    reader = TurnReader()
    reader.feed("item/agentMessage/delta", {"itemId": "msg-1", "delta": "답변"})

    emitted = reader.feed(
        "item/completed", {"item": {"type": "agentMessage", "id": "msg-1", "text": "답변"}}
    )

    assert emitted == []


# ── 인증 실패 회차 ───────────────────────────────────────────────────────


def test_401_샘플은_인증_예외가_된다():
    reader = TurnReader()

    with pytest.raises(LlmUnauthenticated):
        for method, params in notifications(UNAUTHENTICATED):
            reader.feed(method, params)


def test_401_은_턴이_끝나기_훨씬_전에_판정된다():
    """CLI 의 자체 재시도를 기다리면 19초, 첫 401 을 보면 약 2초다.

    초가 아니라 "몇 번째 알림에서 알 수 있었는가"로 고정한다 — 순서는 프로토콜의 성질이다."""
    all_notifications = list(notifications(UNAUTHENTICATED))
    reader = TurnReader()
    seen = 0

    with pytest.raises(LlmUnauthenticated):
        for method, params in all_notifications:
            seen += 1
            reader.feed(method, params)

    assert seen < len(all_notifications) / 2
    assert not reader.completed


def test_codexErrorInfo_가_문자열이어도_죽지_않는다():
    """마지막 `error` 알림에서는 `"other"` 라는 문자열로 온다.

    객체를 가정한 체이닝은 인증 실패 경로에서만 죽어 정상 회차로는 잡히지 않는다."""
    params = {
        "error": {"message": "unexpected status 401", "codexErrorInfo": "other"},
        "willRetry": False,
    }

    assert http_status(params["error"]) is None
    with pytest.raises(LlmGenerationFailed):
        TurnReader().feed("error", params)


def test_자체_재시도_중인_오류는_예외가_아니다():
    """`willRetry` 가 붙은 비-401 오류까지 태우면 재시도가 이중으로 걸린다."""
    reader = TurnReader()

    params = {"error": {"message": "Reconnecting... 1/5", "codexErrorInfo": {}}, "willRetry": True}

    emitted = reader.feed("error", params)

    assert emitted == []


# ── 종료 판정 ────────────────────────────────────────────────────────────


def test_실패한_턴도_turn_completed_로_온다():
    """`turn/failed` 라는 알림은 없다. 메서드 이름만 보면 실패를 성공으로 센다."""
    reader = TurnReader()

    with pytest.raises(LlmGenerationFailed):
        reader.feed("turn/completed", {"turn": {"id": "t", "status": "failed"}})

    # 실패해도 턴은 끝났다 — 세션 반납의 조건은 성패가 아니라 종료 여부다.
    assert reader.completed


def test_성공한_턴은_완료로_표시된다():
    reader = TurnReader()

    assert reader.feed("turn/completed", {"turn": {"id": "t", "status": "completed"}}) == []
    assert reader.completed


# ── 깨진 입력 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "이건 JSON 이 아니다",
        '{"method": "item/agentMessage/delta"',  # 잘린 줄
        "[1, 2, 3]",  # 객체가 아니다
        "",
        "   ",
    ],
)
def test_깨진_줄은_None_이고_예외가_아니다(line: str):
    """한 줄 때문에 세션 전체가 사망으로 판정되면 정상 요청이 실패한다.

    실험 단계 표면이라 모르는 줄이 섞일 수 있고, 버리는 대가는 그 줄의 정보뿐이다."""
    assert parse_message(line) is None


def test_깨진_줄_다음_줄은_정상_파싱된다():
    """파서가 죽지 않는다는 것은 다음 줄을 계속 읽는다는 뜻이다."""
    good = json.dumps({"method": "turn/completed", "params": {"threadId": "t"}})

    assert parse_message("{절반만") is None
    assert parse_message(good) == json.loads(good)
