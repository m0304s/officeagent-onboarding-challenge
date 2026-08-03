# Codex app-server 알림 스트림 샘플

**손으로 지어낸 파일이 아닙니다.** 컨테이너 안에서 실물 `codex app-server`를 stdio JSON-RPC로
띄우고 stdout을 줄 단위로 그대로 뜬 것입니다. 알림 파서를 이 파일들 위에서 만들 것이므로,
이 디렉터리가 곧 "우리가 무엇을 보고 파서를 만들었는가"의 증거입니다 (design 결정 10).

이전에 있던 `codex exec --json` 픽스처 4건은 [지웠습니다](#exec-픽스처를-지운-이유).

## 채취 조건

| 항목 | 값 |
|------|-----|
| 채취일 | 2026-08-03 |
| CLI 버전 | `codex-cli 0.146.0` (`api` 이미지 안. 호스트는 0.145.0) |
| 채취 위치 | `docker compose exec api` — 즉 **런타임과 같은 이미지** |
| 표면 | `codex app-server` (stdio JSON-RPC, `[experimental]` 표기) |
| 인증 (성공 회차) | `.secrets/codex/auth.json` 볼륨 마운트 → `CODEX_HOME=/home/app/.codex` |
| 인증 (401 회차) | `CODEX_HOME=/tmp/codex-noauth` — `auth.json`이 없는 빈 디렉터리 |

프로세스는 어댑터가 실제로 쓸 형태로 띄웠습니다 — 환경을 상속하지 않고 세 개만 넘기고,
작업 디렉터리는 빈 임시 디렉터리입니다.

```sh
env -i HOME=/home/app PATH=/usr/local/bin:/usr/bin:/bin CODEX_HOME=/home/app/.codex \
  codex app-server        # cwd = 빈 임시 디렉터리
```

주고받은 메시지는 이 넷이 전부입니다. 프롬프트는 argv가 아니라 `turn/start`의 `input`으로
들어갑니다.

```jsonc
--> {"jsonrpc":"2.0","id":1,"method":"initialize",
     "params":{"clientInfo":{"name":"officeagent-capture","version":"0.0.0"}}}
--> {"jsonrpc":"2.0","method":"initialized"}                       // 알림, 응답 없음
--> {"jsonrpc":"2.0","id":2,"method":"thread/start",
     "params":{"cwd":"<빈 디렉터리>","sandbox":"read-only",
               "approvalPolicy":"never","ephemeral":true}}
--> {"jsonrpc":"2.0","id":3,"method":"turn/start",
     "params":{"threadId":"<위 응답의 thread.id>",
               "input":[{"type":"text","text":"<프롬프트 전문>"}]}}
```

프롬프트는 RAG 모양 그대로입니다 — 규칙(근거 밖 조회 금지·판정 줄·`[n]` 마커) + `[근거]` 블록
2건(`sample-docs/company-policy.txt`에서 잘라 온 것) + `[질문]`. **픽스처 안에
`item/started`·`item/completed`의 `userMessage`로 전문이 그대로 들어 있습니다.**

## 파일

| 파일 | 무엇을 고정하는가 | 줄 수 |
|------|------------------|------:|
| `app_server_answerable.jsonl` | 정상 답변. `item/agentMessage/delta` **38건**이 흐르고 `turn/completed`의 `turn.status == "completed"` | 58 |
| `app_server_unauthenticated.jsonl` | 인증 없는 상태. `error` 알림 10건 뒤 `turn/completed`의 `turn.status == "failed"`. 델타는 **0건** | 23 |

## 파서가 이 파일들에서 읽어야 할 것

- **답변은 델타로 온다.** `item/agentMessage/delta`의 `params.delta`를 도착 순서대로 이어 붙이면
  같은 아이템의 `item/completed` 텍스트와 **정확히 일치**합니다 (77자 / 델타 38건).
  `exec` 표면에는 없던 성질이고, 이것이 표면을 바꾼 이유입니다.
- **`item/completed`의 완성본을 출력에 쓰면 답변이 두 번 적힙니다.** 델타가 0건인 아이템에
  한해서만 안전판으로 씁니다 (design 결정 5).
- **`item/*` 알림은 `agentMessage`만 있는 게 아닙니다.** 우리가 보낸 프롬프트가 `userMessage`
  아이템으로 되돌아옵니다. `item.type`으로 거르지 않으면 프롬프트를 답변으로 착각합니다.
- **인증 실패는 구조화된 값입니다** — `params.error.codexErrorInfo.responseStreamDisconnected.httpStatusCode == 401`.
  문구 매칭이 필요 없습니다.
- **`codexErrorInfo`가 항상 객체인 것은 아닙니다.** 마지막 `error` 알림에서는 문자열 `"other"`로
  옵니다. `.get("codexErrorInfo", {}).get(...)`처럼 객체를 가정하면 `AttributeError`로 죽습니다.
- **`turn/failed`라는 메서드는 없습니다.** 실패해도 `turn/completed`가 오고 성패는
  `params.turn.status`(`completed` / `failed`)에 있습니다. 메서드 이름만 보면 실패를 성공으로 셉니다.
- **`threadId`가 모든 알림에 실리지는 않습니다.** 두 파일을 통틀어
  `configWarning`·`remoteControl/status/changed`·`thread/started`·`account/rateLimits/updated`
  넷은 `params.threadId`가 없습니다(`thread/started`는 `params.thread.id`에만 있습니다).
  `threadId`로 갈라 큐에 넣는 라우팅은 **세션 수준 알림을 큐에 넣지 않고 로그로만 남기는 규칙**과
  함께여야 성립합니다. 델타·`item/*`·`error`·`turn/*`은 전부 `threadId`를 싣습니다.
- `thread/tokenUsage/updated`·`account/rateLimits/updated`의 사용량은 소비자가 없습니다. 읽지 않습니다.

## 실측 시각

같은 회차의 상대 시각입니다. 파일에는 `emittedAtMs`(절대 epoch ms)만 있어 여기 함께 적습니다.

| 지점 | 성공 회차 | 401 회차 |
|------|---------:|--------:|
| `initialize` 응답 | 0.25초 | 0.12초 |
| `thread/start` 응답 | **7.87초** | 0.17초 |
| `turn/started` | 8.63초 | 0.18초 |
| 첫 `item/agentMessage/delta` | 15.60초 | — (없음) |
| 첫 401 `error` | — | **2.19초** |
| `turn/completed` | 16.65초 | 18.98초 |

**`thread/start`의 8초가 인증·세션 준비입니다** — 인증이 없는 회차에서는 0.17초에 끝납니다.
프로세스를 살려 두면 이 8초가 사라진다는 것(두 번째 `thread/start` 0.07초)이 세션 풀의 근거고,
그 값은 `ARCHITECTURE.md`에 있습니다.

**401은 2.2초에 알 수 있는데 CLI에 맡기면 19초입니다.** CLI가 WebSocket 5회 → HTTPS 5회로
자체 재시도하고(`willRetry: true`), 그동안 우리는 아무것도 얻지 못합니다. 첫 401을 보는 즉시
끊는 이유입니다 (design 결정 7).

## stderr는 픽스처에 없습니다

stdout만 떴습니다. stderr에는 401마다 한 줄씩 `ERROR ... 401 Unauthorized`가 쌓이고, 그 외에
`Codex could not find bubblewrap on PATH ... will use the bundled bubblewrap` 경고가 매번 옵니다.
**응답에 싣지 않고 로그로만 남기되 반드시 읽어야 하는 스트림입니다** — 세션이 오래 살면 버퍼가
차서 어느 순간 조용히 멎습니다.

## 자격증명은 들어 있지 않습니다

커밋 전에 확인했습니다. 두 파일 어디에도 토큰·`Authorization` 헤더·이메일이 없습니다.
`installationId`와 `threadId`는 회차마다 새로 생기는 UUID입니다.

## 다시 뜨는 방법

CLI 버전이 올라 프로토콜이 바뀌면 이 파일들이 낡습니다. `[experimental]` 표면이라 그럴 확률이
`exec`보다 높습니다. 위 "채취 조건"의 네 메시지를 순서대로 보내고 stdout을 그대로 받아 적으면
재현됩니다 — `initialize` 응답을 기다렸다가 `initialized`를 보내고, `thread/start` 응답의
`result.thread.id`를 `turn/start`의 `threadId`로 넘긴 뒤, `turn/completed`가 올 때까지 읽습니다.
401 회차는 `CODEX_HOME`만 빈 디렉터리로 바꿉니다.

형식이 바뀌었으면 **`ARCHITECTURE.md`의 실측 절도 함께** 고칩니다.

## exec 픽스처를 지운 이유

`answerable.jsonl`·`insufficient.jsonl`·`unauthenticated.jsonl`·`tool_use.jsonl` 4건은
`codex exec --json`의 출력이었습니다. 표면을 `app-server`로 바꾸면서 **그 파일을 읽는 테스트가
하나도 남지 않았습니다.** 안 쓰는 픽스처는 CLI 버전이 올라도 아무도 고치지 않고, 그러면 나중에
읽는 사람이 "이게 현재 형식"으로 착각합니다 — 이 리포가 가장 경계하는 문서-코드 불일치입니다.
실측의 **결론**(왜 `exec`를 안 쓰는가)은 `ARCHITECTURE.md` 산문에 남아 있고, 파일 자체는
`git show 8e503fc:tests/fixtures/codex/answerable.jsonl`로 꺼낼 수 있습니다.
