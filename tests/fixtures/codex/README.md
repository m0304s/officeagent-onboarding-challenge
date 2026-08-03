# Codex CLI 출력 샘플

**손으로 지어낸 파일이 아닙니다.** 전부 컨테이너 안에서 실물 `codex exec --json` 을 돌려
stdout 을 그대로 뜬 것입니다. 파서를 이 파일들 위에서 만들었으므로, 이 디렉터리가 곧
"우리가 무엇을 보고 파서를 만들었는가"의 증거입니다 (design 결정 14).

## 채취 조건

| 항목 | 값 |
|------|-----|
| 채취일 | 2026-08-03 |
| CLI 버전 | `codex-cli 0.146.0` (`api` 이미지 안. 호스트는 0.145.0) |
| 채취 위치 | `docker compose exec api` — 즉 **런타임과 같은 이미지** |
| 인증 | `.secrets/codex/auth.json` 볼륨 마운트 (`Logged in using ChatGPT`) |

명령은 어댑터가 실제로 쓸 형태 그대로입니다 — 프롬프트는 **stdin**, 작업 디렉터리는 빈 임시
디렉터리, 샌드박스는 읽기 전용, 환경은 상속하지 않고 최소 집합만:

```sh
env -i HOME=/home/app PATH=/usr/local/bin:/usr/bin:/bin CODEX_HOME=/home/app/.codex \
  codex exec --json --skip-git-repo-check --cd <빈 임시 디렉터리> --sandbox read-only \
    --ephemeral --ignore-user-config -c approval_policy=never - < prompt.txt
```

## 파일

| 파일 | 무엇을 고정하는가 | 종료 코드 |
|------|------------------|----------|
| `answerable.jsonl` | 정상 답변. `thread.started` → `turn.started` → `item.completed`(`agent_message`) → `turn.completed` 4줄. 답변 전체가 **한 이벤트**에 실린다 | 0 |
| `insufficient.jsonl` | 근거가 질문을 뒷받침하지 않을 때 모델이 `VERDICT: INSUFFICIENT` 를 내는 실제 출력. 유도하지 않고 관측된 것이다 | 0 |
| `unauthenticated.jsonl` | 인증 없는 상태(`CODEX_HOME` 에 `auth.json` 없음). `error` 이벤트 10회 뒤 **`turn.failed`** 로 끝나고 `agent_message` 가 하나도 없다. 문구는 `401 Unauthorized` | 1 |
| `tool_use.jsonl` | 도구를 쓰라고 대놓고 시킨 프롬프트. `command_execution` 아이템이 섞이고 `agent_message` 가 **3건** 나온다 — 파서가 `item.type` 으로 걸러야 하는 이유 | 0 |

## 파서가 이 파일들에서 읽어야 할 것

- 답변 텍스트는 `item.completed` **중에서도** `item.type == "agent_message"` 인 것의 `item.text` 에 있다.
  `item.completed` 만 보고 꺼내면 `tool_use.jsonl` 에서 셸 명령 기록을 답변으로 착각한다.
- **한 턴에 `agent_message` 가 여러 건일 수 있다** (`tool_use.jsonl` 3건). 계약이 `answer` 이벤트를
  "0회 이상"으로 열려 있는 이유가 이것이다.
- 인증 실패는 stdout 에도 나온다 — `turn.failed` 와 `error` 이벤트의 `message`. stderr 만 보지 않아도 된다.
- `turn.completed` 의 `usage` 는 소비자가 없다. 읽지 않는다.

## 다시 뜨는 방법

CLI 버전이 올라 형식이 바뀌면 이 파일들이 낡습니다. 채취 스크립트는 남기지 않았고
(일회성 실측이었습니다), 위 명령과 `openspec/changes/add-answer-generation/tasks.md` 1장의
절차로 재현합니다. 형식이 바뀌었으면 **`ARCHITECTURE.md` 의 실측 표도 함께** 고칩니다.
