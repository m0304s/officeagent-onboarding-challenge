# Codex app-server 델타 스트리밍 — 설계

> 대상 change: `add-answer-generation`
> 작성일: 2026-08-03
> 이 문서가 바꾸는 것: 해당 change의 `proposal.md`, `design.md` 결정 7·9·10·13·14·15, `tasks.md` §1·§3·§4.
> **바꾸지 않는 것: `specs/answer-generation/spec.md`.**

## 왜 이 문서가 있는가

`add-answer-generation`의 설계는 "**Codex CLI는 토큰 델타를 주지 않는다**"는 실측 위에 서 있었다.
그 실측은 `codex exec --json`에 대해서는 **지금도 사실**이다 — 4,808자 답변도 `item.completed`
한 이벤트로 통째로 왔다.

그런데 `exec`는 CLI의 유일한 표면이 아니다. `codex app-server`(stdio 위의 JSON-RPC)는
**토큰 단위 델타를 낸다.** 실측으로 확인했다.

```
15.08  item/agentMessage/delta  delta='VER'
15.08  item/agentMessage/delta  delta='DICT'
15.08  item/agentMessage/delta  delta=':'
15.08  item/agentMessage/delta  delta=' ANSW'
15.08  item/agentMessage/delta  delta='ER'
15.08  item/agentMessage/delta  delta='ABLE'
15.23  item/agentMessage/delta  delta='\n'
15.23  item/agentMessage/delta  delta='교육'
...
델타 112건 → 이어 붙이면 209자, item.completed 의 답변 전문과 일치
```

스트리밍은 PRD의 하드 요구사항이고 평가 비중 18% 항목이다. 표면 하나를 바꿔 얻을 수 있다면
바꾸는 것이 맞다.

## 실측 근거

측정일 2026-08-03, `api` 이미지 안의 `codex-cli 0.146.0`. 런타임과 같은 이미지에서 쟀다.

### 두 표면의 대비

| | `codex exec --json` | `codex app-server` |
|---|---|---|
| 토큰 델타 | **없음** (4,808자도 한 이벤트) | **있음** (209자에 112건) |
| 요청당 지연 | 12 ~ 19초 | **4.5초** (프로세스가 따뜻할 때) |
| 프로세스 모델 | 요청당 일회성 | 세션 — 핸드셰이크 후 재사용 |
| 인증 실패 판정 | 종료 코드 1 + **문구 매칭** | `codexErrorInfo.responseStreamDisconnected.httpStatusCode == 401` |
| 인증 실패까지 | 19.3초 (CLI 자체 재시도 10회) | 첫 401 알림이 **약 2초** |
| 성숙도 | 안정 | `[experimental]` 표기 |

`exec`의 숫자를 지우지 않는 이유가 여기 있다 — 이 대비가 곧 갈아탄 근거다.

### 기동 비용은 첫 요청에만 붙는다

| 구간 | 첫 턴 | 두 번째 턴 (같은 프로세스) |
|---|---|---|
| `initialize` 응답 | 0.35초 | — |
| `thread/start` 응답 | **8.11초** | **0.07초** |
| `turn/started` | +0.76초 | +0.05초 |
| 턴 전체 | 4.79초 | **4.54초** |

`thread/start`의 8초는 인증·세션 준비다(인증 없는 상태에서는 0.16초에 끝난다).
**프로세스를 살려 두면 그 8초가 사라진다.** 이 한 줄이 세션 풀을 정당화한다.

### 프로토콜에서 확인한 사실

- ~~모든 알림이 `threadId`를 싣는다~~ → **정정**(2026-08-03, tasks 1B.5 픽스처 채취 중): 델타·`item/*`·`error`·`turn/*`은 싣지만 `configWarning`·`remoteControl/status/changed`·`thread/started`·`account/rateLimits/updated` 넷은 싣지 않는다. threadId 라우팅은 성립하되 **"threadId 없는 알림은 세션 수준이라 큐에 넣지 않고 로그로만 남긴다"는 규칙과 함께**여야 한다 (아래 결정 3의 `session.py` 몫).
- `codexErrorInfo`가 **항상 객체는 아니다** — 마지막 `error` 알림에서는 문자열 `"other"`로 온다. 객체를 가정한 체이닝은 거기서 죽는다 (같은 채취 중 발견).
- `ErrorNotification`에 **`willRetry: boolean`**이 있다 → CLI가 자체 재시도 중인지 알 수 있다.
- **`turn/failed`라는 알림은 없다.** 종료는 항상 `turn/completed`이고 성패는 `turn.status`에 있다.
  메서드 이름만 보면 실패를 성공으로 센다.
- `item/agentMessage/delta`의 모양은 `{delta, itemId, threadId, turnId}`.
- 기능 플래그가 필요 없다. 기본 설정에서 그대로 델타가 나온다.

## 결정

### 1. `codex app-server`로 간다

`[experimental]` 표기를 감수한다. 얻는 것이 스트리밍 하나가 아니라 지연 3분의 1과
구조화된 인증 판정까지 셋이고, 되돌리기 비용이 낮기 때문이다 — 계약이 안 바뀌므로
어댑터 디렉터리 하나를 되돌리면 `exec` 구현으로 복귀한다.

### 2. 세션 풀 — 지연 기동, 상한은 `qa_concurrency`

첫 요청에 프로세스를 띄워 살려 둔다. 요청마다 `thread/start`로 새 스레드만 판다
(`ephemeral: true`).

- **부팅 경로는 건드리지 않는다.** 지연 기동이라 `tests/test_boot.py`의 계약
  (자격증명 없이도 기동·`/health` 200)이 그대로 유지된다.
- **세션당 활성 턴은 하나**로 못 박는다. 풀 상한이 `qa_concurrency`와 같아 자연히 그렇게
  되지만 명시적 규칙으로 둔다 — 한 세션에서 턴 둘을 돌리면 `thread/status/changed`처럼
  `turnId`가 없는 알림의 주인이 모호해진다.
- **thread를 요청마다 새로 파는 이유**는 대화 맥락 없음(Non-Goals)을 유지하고 이전 요청의
  문서 본문이 다음 문맥에 남지 않게 하기 위해서다. 두 번째 `thread/start`가 0.07초라
  비용이 없다.

### 3. 부품 셋

```
adapters/llm/session.py   프로세스 하나 = 세션 하나. 기동 · initialize/initialized 핸드셰이크 ·
                          JSON-RPC 송수신 · id로 응답 짝짓기 · 알림을 threadId로 갈라 큐에 넣기 ·
                          살아 있는지 판정 · 회수
adapters/llm/pool.py      세션 풀. 지연 기동, 상한, 빌려주기/반납, 죽은 세션 폐기와 교체
adapters/llm/codex.py     AnswerGenerator 구현. 세션을 빌려 thread/start → turn/start →
                          델타 yield → 종료 판정. 실패를 도메인 예외로 정규화
```

**세션이 독립 부품인 이유**는 여기에만 두 개의 비동기 흐름이 있어서다 — 우리가 보내는
요청과, 우리와 무관하게 흘러드는 알림. 이 라우팅이 어댑터에 섞이면 "델타를 어떻게 텍스트로
바꾸는가"와 "메시지를 누구에게 주는가"가 한 함수에서 얽힌다.

**풀이 독립 부품인 이유**는 이번에 새로 생기는 위험이 전부 거기 있어서다 — 죽은 세션을
빌려주는 경우, 상한 대기, 기동 실패. 가짜 프로세스로 실물 CLI 없이 전부 시험할 수 있다.

### 4. `AnswerGenerator` 계약은 바뀌지 않는다

```python
def generate(self, prompt: str, *, timeout_seconds: float) -> AsyncIterator[str]: ...
```

원래 결정 8이 "조각을 yield 한다 — 완성본을 반환하지 않는다", 결정 15가 "`answer` 이벤트
0회 이상"으로 계약을 열어 둔 값을 여기서 받는다. **`services/qa.py`도 스펙도 한 줄도 바뀌지
않는다.** 바뀌는 것은 `adapters/llm/` 안쪽뿐이다.

### 5. 델타는 그대로 흘리고, `item.completed`는 출력에 쓰지 않는다

어댑터는 델타를 **글자 그대로** yield 한다. 합치지도 쪼개지도 않는다 — 결정 15의
"인위적 분할 금지"가 방향만 뒤집혀 그대로 유효하다.

`item/completed`의 완성본 텍스트는 **출력에 쓰지 않는다.** 이미 델타로 다 나갔으므로 또
yield 하면 답변이 두 번 적힌다.

**예외 하나** — 어떤 아이템에 델타가 0건인데 `item.completed`에 텍스트가 있으면 그때만 그
텍스트를 한 번 yield 한다. 델타가 사라지는 회귀가 나도 빈 답변(4.6에서 생성 실패로 분류돼
3회 헛재시도)이 아니라 `exec` 시절 동작으로 **degrade** 되게 하는 안전판이다.

한 턴에 `agent_message` 아이템이 여럿일 수 있다. 도착 순서대로 이어 붙인다.

### 6. 판정 줄 분리를 `core/`에 둔다

원래 tasks 4.4는 판정 줄 버퍼링을 `services/qa.py`에 두기로 했다. **`core/prompting.py`로
옮긴다.**

```python
class VerdictSplitter:                              # 표준 라이브러리만
    def feed(self, chunk: str) -> list[str]: ...    # 내보낼 본문 조각들
    @property
    def verdict(self) -> Verdict | None: ...
```

근거는 결정 10과 같다 — 출력 형식이 무엇을 뜻하는지는 도메인 지식이고, 순수 함수여야
비동기·페이크 없이 문자열 단언으로 회귀를 고정할 수 있다. 이 change에서 가장 틀리기 쉬운
로직인데 서비스에 두면 그 테스트가 페이크 생성기와 이벤트 시퀀스 위에 서게 된다.

**규칙**

- 개행이 오면 → 첫 줄을 판정으로 파싱. 형식이 맞으면 그 뒤가 본문, 안 맞으면
  **첫 줄까지 통째로 본문**(결정 4: 답변을 버리지 않는다).
- 누적 문자열이 `VERDICT: ANSWERABLE`·`VERDICT: INSUFFICIENT` 어느 쪽의 접두사도 아니게
  되는 순간 → 판정 없음으로 확정하고 **즉시 전부 내보낸다.**

두 번째 규칙이 없으면 판정 줄을 쓰지 않은 출력에서 첫 줄 전체(짧은 답이면 답변 전부)를
붙들게 된다. 하필 형식을 어긴 회차에서만 스트리밍이 죽는다. 이 규칙 덕에 **버퍼 상한이
21자로 고정된다.**

판정 줄은 `answer` 이벤트에도 `done.answer`에도 실리지 않는다. `done.answer`는 내보낸 본문
조각을 이어 붙인 것과 정확히 같다.

### 7. 실패 처리 넷

**인증 부재** — `httpStatusCode == 401`을 보는 **즉시** `turn/interrupt` 후
`LlmUnauthenticated`. CLI 자체 재시도(10회, 18.6초)를 기다리지 않는다. 백오프를 몇 번 돌아도
자격증명은 생기지 않는다는 결정 9의 논리가 CLI의 재시도에도 그대로 적용된다.

**`willRetry`와 401의 우선순위를 명시한다** — 401이면 `willRetry` 값과 **무관하게** 즉시
끊는다. 실측에서 첫 401 알림은 `willRetry: true`로 왔고, 그걸 "CLI가 알아서 하겠지"로 넘기면
18.6초를 기다리게 된다. `willRetry`는 **401이 아닌** `error` 알림에만 적용한다 — 그때는
로그에만 남기고 우리 재시도 정책에 태우지 않는다(태우면 재시도가 이중으로 걸린다).
`willRetry: false`인 비-401 `error`가 `LlmGenerationFailed`가 된다.

**타임아웃** — `kill`이 아니다. 프로세스가 풀 자산이기 때문이다.

```
상한 초과 → turn/interrupt → 유예 안에 turn/completed 오면 세션 반납
                          → 안 오면 프로세스 폐기 + 풀에서 제거
```

어느 쪽이든 `LlmTimeout`이 난다.

**프로세스 사망**

| 발견 시점 | 처리 |
|---|---|
| 빌릴 때 죽어 있음 | 폐기하고 새로 기동. **실패가 아니라 재기동** — 8초를 문다 |
| 턴 도중 사망(stdout EOF·exit) | 세션 폐기 + `LlmGenerationFailed` |
| 핸드셰이크 실패(`initialize` 무응답) | 세션 폐기 + `LlmGenerationFailed` |

풀은 죽은 세션을 빌려주지 않는다 — 반납할 때와 빌려줄 때 양쪽에서 생존을 확인한다.

**클라이언트 취소** — 결정 8대로 순회 종료(`aclose`)로 표현되고, `finally`에서
`turn/interrupt` 후 세션을 반납한다. 프로세스를 죽이지 않는다.

**stderr** — 별도로 읽어 로그에만 남기고 응답에 싣지 않는다. `exec` 때보다 **더 중요하다**:
일회성 프로세스는 버퍼가 차봐야 그 요청만 멈추지만, 살아 있는 세션은 계속 쌓여 어느 순간
조용히 멎는다. app-server는 401마다 stderr에 ERROR 줄을 쓴다.

### 8. 세션 반납의 불변식 — 애매하면 버린다

반납 전에 턴이 **확실히** 끝나 있어야 한다. 아니면 다음 요청이 이전 턴의 델타를 받는다.
반납 조건은 `turn/completed`를 받았을 때뿐이고, 그 외에는 전부 폐기다. 8초가 아깝지만
답변이 섞이는 것보다 훨씬 싸다.

### 9. 서비스의 재시도 분류는 그대로

| 사유 | 재시도 |
|---|---|
| `LlmTimeout` | 한다 |
| `LlmGenerationFailed` (세션 사망·핸드셰이크·파싱·빈 본문) | 한다 |
| `LlmUnauthenticated` | **안 한다** |
| 이미 `answer` 조각을 내보낸 뒤 | **안 한다** |

어댑터가 실패를 같은 세 예외로 정규화하므로 서비스는 바뀌지 않는다.

### 10. 테스트 다섯 겹

| 층 | 무엇을 고정하는가 | 무엇으로 | 실행 |
|---|---|---|---|
| 시퀀스·정책 | 이벤트 순서, 거절 두 갈래, 인용 검증, 재시도, 오류 3종 | 스크립트된 페이크 `AnswerGenerator` | 항상 |
| 판정 줄 분리 | 델타가 판정 줄을 쪼개는 전 경우 | `VerdictSplitter` 순수 함수 — 문자열 단언만 | 항상 |
| 알림 파싱 | 델타 알림 → 텍스트, 401 판정, 종료 판정 | 저장된 실물 알림 샘플 | 항상 |
| 세션·풀 수명 | 타임아웃·사망·취소·핸드셰이크 실패·상한 대기 | 가짜 app-server 스크립트 | 항상 |
| 실물 | CLI가 실제로 답을 만드는가 | 실물 CLI | 마커 격리, 기본 제외 |

앞의 네 겹이 구독도 네트워크도 없이 돈다.

**가짜 app-server(`tests/fake_app_server.py`)가 이 설계의 열쇠다.** 같은 줄 단위 JSON-RPC를
말하는 짧은 파이썬 스크립트이고, 인자로 어떻게 망가질지를 지시받는다.

| 지시 | 재현하는 실패 |
|---|---|
| (기본) | 정상 — 델타를 스크립트대로 흘리고 `turn/completed` |
| `--no-handshake` | `initialize` 무응답 → 핸드셰이크 실패 |
| `--die-mid-turn` | 델타 몇 개 뒤 프로세스 종료 |
| `--hang` | `turn/start` 후 침묵 → 타임아웃 |
| `--ignore-interrupt` | `turn/interrupt` 무시 → 유예 초과 → 세션 폐기 경로 |
| `--auth-401` | `willRetry` 붙은 401 `error` 알림 |
| `--slow-deltas N` | 델타 사이 지연 → 취소·하트비트 관측 |

실물 CLI로는 인위적으로 만들 수 없는 것들(핸드셰이크 무응답, interrupt 무시)이 여기 있다.
이 층은 실물 층의 대체재가 아니라 실물 층이 **닿지 못하는 곳**이다.

전부 즉시 응답하므로 타임아웃 테스트도 상한을 수십 ms로 두면 밀리초 단위로 끝난다.
시간 단언은 절대 시간이 아니라 관계만 본다(두 번째 백오프가 첫 번째보다 길다).

**판정 줄 분리는 표로 덮는다**

| 입력 | 기대 |
|---|---|
| `["VERDICT: ANSWERABLE\n본문"]` | 조각 1개, 본문만 |
| `["VER","DICT",": ANSW","ERABLE","\n","교육"]` | 확정 전 0개, 이후 1개 |
| `["교육비는"]` | **즉시** 1개, 첫 글자 안 잘림 |
| `["VERDICT: INSUFFICIENT\n근거가"]` | 판정 `INSUFFICIENT`, 마커 무시 |
| `["VERDICT: ANSWERABLE"]` 뒤 개행 없이 종료 | 본문 없음 → 생성 실패(재시도) |
| `["VERDICTX..."]` | 접두사 이탈 → 즉시 전량 방출 |

### 11. 설정 항목 추가

| 항목 | 기본값 | 근거 |
|---|---|---|
| `qa_llm_interrupt_grace_seconds` | 2.0 | `turn/interrupt` 후 `turn/completed`를 기다리는 유예. 넘기면 세션 폐기 |
| `qa_llm_session_startup_timeout_seconds` | 30.0 | 기동+핸드셰이크 상한. 실측 8.5초의 3배 이상 |

기존 결정 13의 항목은 그대로 유지된다. 전부 기본값이 있어 "설정 없이 기동된다"는 요구사항을
깨지 않는다.

### 12. 스펙 295줄의 독법을 명시한다

스펙은 "중단된 시도가 만든 자원(외부 프로세스 등)은 함께 정리되어야 한다 (**MUST**)"고
말한다. 풀에서는 **시도가 만든 것이 thread와 turn**이고 프로세스는 그 전부터 있다가 그
뒤에도 산다. 둘 다 정리하므로(interrupt + `ephemeral` thread 폐기) 스펙을 만족한다.

이 독법을 여기 적어 두는 이유는, 나중에 누가 "프로세스를 죽이라는 뜻"으로 읽고 풀을
해체하는 일을 막기 위해서다. 스펙 문언은 바꾸지 않는다 — 옳은 고도로 쓰여 있다.

## 기존 산출물 되감기

| 산출물 | 조치 |
|---|---|
| `specs/answer-generation/spec.md` | **변경 없음** |
| `proposal.md` | "구현체는 `codex exec --json`을 띄운다"와 "토큰 델타는 없다"를 고친다 |
| `design.md` 결정 7 | 타임아웃 처리를 `kill` → `turn/interrupt` + 조건부 폐기 |
| `design.md` 결정 9 | **다시 쓴다** — exec 어댑터 → app-server 세션·풀 |
| `design.md` 결정 10 | `VerdictSplitter` 추가 |
| `design.md` 결정 13 | 설정 두 항목 추가 |
| `design.md` 결정 14 | 세 겹 → 다섯 겹 |
| `design.md` 결정 15 | **다시 쓴다** — "델타 없음" → "델타 있음". 인위적 분할 금지는 유지 |
| `tasks.md` §1 | **지우지 않는다.** 절을 더해 app-server 실측을 넣고 결론을 바꾼다 |
| `tasks.md` §3 | app-server 기준으로 다시 쓴다 |
| `tasks.md` §4.4 | `core/`로 이동 |
| `ARCHITECTURE.md` | exec 실측 표를 "왜 이걸 안 쓰는가"로 프레이밍하고 app-server 실측을 주 절로 |
| `tests/fixtures/codex/*.jsonl` | **지운다** (아래) |

**1장을 지우지 않는 이유** — exec 숫자가 없으면 "왜 app-server인가"에 답이 없다. 요청당
12~16초 대 4.5초, 문구 매칭 대 `httpStatusCode == 401`. 이 대비가 곧 근거다.

**exec 픽스처를 지우는 이유** — 새 설계에서 이 파일을 읽는 테스트가 하나도 없다. 안 쓰는
픽스처는 CLI 버전이 올라도 아무도 고치지 않고, 그러면 나중에 읽는 사람이 "이게 현재 형식"
으로 착각한다. 그게 이 리포가 가장 경계하는 문서-코드 불일치다. 실측의 **결론**은
`ARCHITECTURE.md` 산문에 남고 파일 자체는 git 이력에 있다.

**순서** — `/opsx:update`로 아티팩트를 되감고 → 1장에 app-server 실측 절을 추가해 픽스처를
뜨고 → 2장 이후로 간다. 코드보다 문서가 먼저인 이유는, 지금 tasks 3.x가 exec 기준이라
그대로 구현하면 완료 표시가 거짓말이 되기 때문이다.

## 위험과 대가

**[`app-server`가 `[experimental]`이다]** → 프로토콜이 예고 없이 바뀔 수 있다. 완화: 알림
파싱을 저장된 실물 샘플로 고정하고, 계약(`AnswerGenerator`)이 안 바뀌므로 최악의 경우
어댑터 디렉터리 하나를 `exec` 구현으로 되돌린다. 되돌리기 비용이 낮다는 것이 이 실험을
감수하는 근거다.

**[프로세스가 상태를 갖게 됐다]** → `exec`의 일회성 프로세스에는 없던 실패 모드가 생긴다 —
죽은 세션, 반쯤 끝난 턴, 이전 턴의 델타 누수. 완화: 세션당 활성 턴 하나, 반납 조건을
`turn/completed` 하나로 좁힘, 애매하면 폐기. 가짜 app-server가 이 셋을 전부 시험한다.

**[8초가 첫 요청에 몰린다]** → 첫 `/qa`는 여전히 12초대다. 완화: 하지 않는다. 기동 시
예열하면 부팅 경로가 CLI를 건드리게 되어 `tests/test_boot.py`의 계약이 깨진다 — 자격증명이
없는 환경에서 기동이 느려지거나 실패하는 쪽이 훨씬 비싸다. 첫 요청이 느린 것은 받아들인다.

**[동시 턴을 한 세션이 감당하는지 확인하지 않았다]** → 확인할 필요가 없게 설계했다. 풀 상한이
`qa_concurrency`와 같고 세션당 활성 턴이 하나라 동시 턴이 발생하지 않는다. 이 성질에 기대는
것을 명시적 규칙으로 적어 둔다.

**[테스트 대부분이 가짜 프로세스 위에서 돈다]** → 실물에서만 드러나는 실패(프로토콜 변화,
인자 형태)를 놓친다. 완화: 알림 파싱 층이 실물 샘플을 쓰고, 실물 층이 마커 뒤에 있고,
그 층을 컨테이너에서 돌리는 명령을 `README.md`에 적는다. `exec` 때와 같은 규율이다.

## 미해결

- **`app-server`의 자격증명 갱신이 호스트를 로그아웃시키는가** — `docs/SPIKES.md` S-3이
  묻는 것과 같은 질문이고, 세션이 오래 살면 갱신을 만날 확률이 `exec`보다 높아진다.
  S-3에 이 사실을 덧붙인다.
- **문맥 총량 상한** — 원 design의 Open Question 그대로. app-server로 바꾼다고 답이 달라지지
  않는다.
- **다음 change의 캐시 키** — 그대로. 다만 모델 식별자를 `turn/start`가 받으므로 키에 넣을
  값을 어디서 읽을지가 조금 더 분명해졌다.
