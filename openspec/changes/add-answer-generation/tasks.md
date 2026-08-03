## 1. 실측 — CLI가 실제로 무엇을 내는지 먼저 확인한다

> 설계가 `ARCHITECTURE.md`의 이전 실측 위에 서 있다(델타 없음, JSONL 이벤트 4종). 어댑터를 쓰기 전에 설치된 버전에서 다시 재고, **그 출력을 파일로 떠서 픽스처로 남긴다** — 손으로 지어낸 샘플은 우리가 상상한 형식을 검증할 뿐이다. design 결정 9·15, Open Questions.

- [x] 1.1 컨테이너 안에서 `codex exec --json`을 실제 프롬프트로 돌려 stdout 전체를 파일로 저장하고, 이벤트 종류·순서·답변 텍스트가 실린 위치를 기록한다
      → 4줄 JSONL: `thread.started` → `turn.started` → `item.completed` → `turn.completed`. 답변은 `item.completed` 중 **`item.type == "agent_message"`** 인 것의 `item.text`. 도구를 쓰는 회차에는 `command_execution` 아이템이 섞이므로 `item.completed`만 보고 꺼내면 안 된다
- [x] 1.2 **토큰 델타 이벤트가 있는지** 판정한다 — 답변이 한 이벤트로 오는지, 여러 이벤트로 나뉘어 오는지. 결과가 `answer` 이벤트 횟수의 근거가 된다
      → **델타 없음.** 4,808자 답변도 한 이벤트로 통째로 왔다. 다만 한 턴에 `agent_message`가 **여러 건**일 수 있다(도구 사용 회차 3건) — "0회 이상" 계약이 그대로 유효하다
- [x] 1.3 프롬프트를 **stdin**으로 넘기는 호출 형태를 확정한다 (플래그·인자 형태). argv 경로는 쓰지 않는다
      → `codex exec --json ... -` (`[PROMPT]` 자리에 `-`) + stdin
- [x] 1.4 에이전트를 좁히는 인자들을 확정한다 — 작업 디렉터리 지정, 읽기 전용 샌드박스, 비대화형 승인. 각각이 실제로 먹히는지 확인한다
      → `--cd <빈 디렉터리>` · `--sandbox read-only` · `-c approval_policy=never` · `--ephemeral` · `--ignore-user-config` · `env -i`(HOME·PATH·CODEX_HOME). **`--skip-git-repo-check`가 없으면 빈 디렉터리에서 아예 못 돈다.** 읽기 전용 샌드박스의 효과는 단독 확인 불가 — 이 컨테이너는 bwrap 네임스페이스가 막혀 **모든** 도구 실행이 실패한다(`ARCHITECTURE.md`)
- [x] 1.5 **인증 없는 상태**로 같은 명령을 돌려 종료 코드와 stderr 문구를 기록한다. 일반 실패(예: 잘못된 인자)와 구분되는지 확인한다
      → **종료 코드로는 구분되지 않는다** (인증 없음·자격증명 깨짐·신뢰되지 않은 디렉터리 모두 1, 잘못된 인자만 2). 판별은 문구로 하고, 다행히 인증 실패 문구가 **stdout JSONL 안에** 있다 — `turn.failed`의 `error.message`에 `401 Unauthorized` / `Missing bearer or basic authentication`
- [x] 1.6 첫 응답까지 걸린 시간과 전체 소요 시간을 재고, `qa_llm_timeout_seconds` 기본값 60초가 타당한지 판정한다
      → RAG 모양 6회: 첫 이벤트 1.0~8.5초, 답변 8.0~18.3초, 종료 12.0~18.9초. **60초 유지** (최악값이 상한의 1/3). 근거 제약 없는 장문 회차는 85.4초로 넘겼으나 우리 프롬프트에서는 그 모양이 나오지 않는다
- [x] 1.7 저장한 stdout 샘플을 `tests/fixtures/` 아래에 픽스처로 커밋한다 — 성공 1건, 인증 실패 1건 이상
      → `tests/fixtures/codex/` 4건: `answerable` · `insufficient` · `unauthenticated` · `tool_use`. 채취 조건과 파서가 읽어야 할 것은 같은 디렉터리 `README.md`
- [x] 1.8 1.1~1.6의 결과를 `ARCHITECTURE.md`의 "LLM SDK 통합 방식"에 적는다 (이전 실측 표를 갱신하고, **아직 호출 코드가 없다**는 문장은 이 change 끝에서 지운다)
      → "컨테이너 안에서의 실측" 절 추가. `docs/SPIKES.md` S-2의 남은 조각(uid 1000·`/home/app/.codex` 경로)도 여기서 함께 닫혔다. "호출 코드가 없다"는 문장은 예정대로 7.1에서 지운다

## 1-B. 실측 2회차 — `codex app-server`로 표면을 바꾼다

> **1장의 결론이 뒤집혔다.** `exec`에 델타가 없는 것은 사실이지만 그게 CLI의 유일한 표면이 아니었다. `codex app-server`(stdio JSON-RPC)는 토큰 델타를 낸다 — 209자 답변에 112건. 덤으로 요청당 지연이 12~19초에서 4.5초로 줄고 인증 판정이 문구 매칭에서 `httpStatusCode == 401`로 바뀐다. 근거와 설계는 [`docs/superpowers/specs/2026-08-03-codex-app-server-streaming-design.md`](../../../docs/superpowers/specs/2026-08-03-codex-app-server-streaming-design.md). **1장을 지우지 않는 이유는 exec 숫자가 곧 갈아탄 근거이기 때문이다.**

- [x] 1B.1 `codex app-server`에 토큰 델타가 있는지 판정한다 — `initialize` → `thread/start` → `turn/start`를 보내고 도착하는 알림을 기록한다
      → **있다.** `item/agentMessage/delta` `{delta, itemId, threadId, turnId}`. 209자 답변에 112건. 기능 플래그 불필요, 기본 설정에서 동작
- [x] 1B.2 세션 재사용의 값을 잰다 — 같은 프로세스에서 두 번째 턴을 돌려 무엇이 절약되는지 확인한다
      → `thread/start` 첫 응답 8.11초, 두 번째 **0.07초**. 턴 자체는 4.5초. **기동 비용은 첫 요청에만 붙는다** → 세션 풀이 정당화된다
- [x] 1B.3 인증 없는 상태에서 실패가 어떤 모양으로 오는지 기록한다
      → `error` 알림의 `codexErrorInfo.responseStreamDisconnected.httpStatusCode == 401`. **구조화된 값이라 문구 매칭이 필요 없다.** 첫 401이 약 2초(CLI 자체 재시도를 끝까지 기다리면 18.6초)
- [x] 1B.4 종료 판정의 함정을 확인한다
      → **`turn/failed` 라는 알림은 없다.** 실패해도 메서드는 `turn/completed`이고 성패는 `turn.status`에 있다. `ErrorNotification`에 `willRetry`가 있어 CLI 자체 재시도와 우리 정책을 가를 수 있다
- [x] 1B.5 app-server 알림 스트림을 `tests/fixtures/codex/` 아래에 픽스처로 뜬다 — 성공 1건, 401 1건. 채취 조건을 README에 남긴다
      → `app_server_answerable.jsonl`(58줄, 델타 38건) · `app_server_unauthenticated.jsonl`(23줄, `error` 10건). **이어 붙인 델타 77자가 `item/completed` 전문과 바이트 단위로 일치**함을 확인. 채취 중 파서 함정 셋을 새로 발견 — ① `codexErrorInfo`가 객체가 아니라 문자열 `"other"`로 오는 회차가 있다 ② 우리 프롬프트가 `userMessage` 아이템으로 되돌아와 `item.type` 필터가 필수다 ③ **`threadId` 없는 알림이 넷 있다**(`configWarning`·`remoteControl/status/changed`·`thread/started`·`account/rateLimits/updated`) — 3.5의 라우팅은 "세션 수준 알림은 큐에 넣지 않는다"와 함께여야 성립한다
- [x] 1B.6 exec 픽스처 4건을 지운다 — 새 설계에서 읽는 테스트가 없다. 안 쓰는 픽스처는 CLI 버전이 올라도 아무도 고치지 않고, 나중에 읽는 사람이 "이게 현재 형식"으로 착각한다. 실측의 **결론**은 `ARCHITECTURE.md` 산문에 남고 파일은 git 이력에 있다
      → 4건 삭제. 꺼내는 방법(`git show 8e503fc:...`)을 픽스처 README에 남겼다
- [x] 1B.7 `ARCHITECTURE.md`의 실측 절을 다시 쓴다 — exec 표를 "왜 이걸 안 쓰는가"로 프레이밍하고 app-server 실측을 주 절로 올린다
      → "컨테이너 안에서의 실측"을 네 소절로 재편: 두 표면의 대비 → 주 표면(app-server) → 참고 표면(exec, 쓰지 않음) → bwrap(양 표면 공통). 1B.5에서 새로 나온 파서 함정 셋과 시간표를 주 소절에 넣었다. 함께 정정한 것 — `docs/superpowers/specs/2026-08-03-...md`의 "모든 알림이 threadId를 싣는다"가 1B.5 실측으로 거짓이 되어 그 자리에 정정을 남겼다
- [x] 1B.8 `docs/SPIKES.md` S-3에 한 줄 덧붙인다 — 세션이 요청 사이에 살아 있어 자격증명 갱신을 만날 확률이 일회성 프로세스보다 높다
      → 완화책은 유효하나 창의 길이가 "요청 수명"에서 "컨테이너 수명"으로 바뀌었다는 점까지 적었다

## 2. 도메인 값 객체와 프롬프트

- [x] 2.1 `core/answers.py` — `FinishReason`(`stop`·`no_evidence`·`insufficient_evidence`), `Citation`, 조립된 답변 값 객체. 표준 라이브러리만
      → `Answer.__post_init__`가 스펙의 두 규칙("빈 답변은 `no_evidence`뿐", "거절에 인용 없음")을 **불변식으로** 지킨다 — 조립을 잘못하면 응답이 나가기 전에 터진다. `elapsed_ms`는 답변이 아니라 요청의 측정값이라 넣지 않았다(`done` 조립 때 서비스가 합친다). 마커→근거 대응(1-base ↔ 0-base)은 `build_citations` 한 곳에 가뒀다
- [x] 2.2 `core/prompting.py` — `PROMPT_VERSION` 상수와 `build_prompt(question, sources)`. 문맥 블록은 `[n] 파일명 (위치)` + 본문, 지시문에 "제공된 근거 밖을 조회하지 말라"와 출력 형식(`VERDICT:` 첫 줄, 근거 문장 끝 `[n]`)을 명시
      → `PROMPT_VERSION = "qa-ko-1"`. 위치 표기는 PDF `(3쪽)` · 그 외 `(문자 120–540)` — PDF 의 문자 오프셋은 쪽 안의 값이라 적으면 오해를 부른다. **근거 0건이면 `ValueError`** 로 막아 결정 6("근거 없으면 부르지 않는다")을 구조로 만들었다
- [x] 2.3 `core/prompting.py` — `parse_answer(raw, source_count)`. 판정 줄 파싱(누락 시 `ANSWERABLE` + 경고), 마커 추출(등장 순서·중복 제거·범위 밖 폐기), 버려진 마커 수 반환
      → 경고는 `core/`가 띄우지 않고 `verdict_line_present` 플래그로 넘긴다(로깅 규약은 서비스가 안다). **중복 제거가 범위 검증보다 먼저** — 없는 번호를 세 번 쓴 답변의 `dropped`는 3이 아니라 1이어야 그 수가 프롬프트 열화의 신호로 쓸 수 있다
- [x] 2.4 `core/prompting.py` — `VerdictSplitter`. 스트리밍용 판정 줄 분리 상태 기계(design 결정 10). 개행이 오면 첫 줄을 파싱하고, 누적 문자열이 두 후보의 접두사가 아니게 되면 **즉시 전량 방출**한다. 버퍼 상한 21자
      → 상한을 `MAX_VERDICT_LINE_CHARS`로 유도해 상수를 손으로 적지 않았다. 설계에 없던 `finish()`를 더했다 — 개행 없이 끝나는 출력(판정 줄만 낸 회차, 짧은 한 줄 답변)이 `feed`만으로는 버퍼에 갇힌다. `parse_answer`와 **판정 줄 인식 규칙(`_VERDICT_LINES`)·앞쪽 공백 처리(`lstrip`)를 공유**해 두 경로의 본문이 일치한다 — 2.7이 임의 분할로 그 일치를 단언한다
- [x] 2.5 `tests/test_prompting.py` — 프롬프트 조립 회귀: 문맥에 모든 근거가 번호와 함께 들어가는가, 파일명·위치가 실리는가, 질문이 들어가는가, 출력 형식 지시가 있는가. **LLM 없이 문자열 단언으로만**
      → 목록에 없던 것을 하나 더했고 그게 이 묶음에서 제일 중요하다 — **프롬프트가 지시한 판정 줄을 파서가 실제로 인식하는가**를 프롬프트에서 정규식으로 뽑아 `parse_answer`에 먹여 확인한다. 둘이 갈리면 모델은 시킨 대로 쓰는데 서버가 못 알아듣고, 그 증상은 오류가 아니라 "판정 줄 없음" 경로로 흡수돼 `INSUFFICIENT`가 영원히 안 나오는 형태로만 드러난다
- [x] 2.6 `tests/test_prompting.py` — 파서 경계: 판정 두 값, 판정 줄 누락, 마커 없음, 중복 마커, 범위 밖 마커, 빈 본문. design 결정 4의 표를 그대로 덮는다
      → 표 네 행 + 마커 다섯 경우. 엄격한 판정 줄 인식(대소문자·공백·콜론 뒤 공백)을 `parametrize` 여섯 건으로 고정했다 — 관대하게 받으면 분리기가 버퍼 상한을 가질 수 없다는 것이 그 대가의 근거다
- [x] 2.7 `tests/test_prompting.py` — `VerdictSplitter` 경계. **비동기도 페이크도 없이 표 하나로 덮는다**: 판정 줄과 본문이 한 조각 / 실측 모양(`VER`·`DICT`·`: ANSW`·`ERABLE`·`\n`·본문) / 판정 줄 없는 출력이 **즉시** 나가고 첫 글자가 안 잘림 / `INSUFFICIENT` / 판정 줄만 오고 끝남(본문 없음) / 접두사 이탈
      → 목록 전부 + 버퍼 상한(한 글자씩 먹이며 붙들린 길이가 `MAX_VERDICT_LINE_CHARS`를 넘지 않음)과 **분리기 ↔ 파서 동치**: 출력 12종 × 1·2·3조각 모든 분할에서 이어 붙인 본문이 `parse_answer(...).body`와 같다. 이것이 "조각을 이어 붙인 것 = `done.answer`" 스펙 불변식의 근거다. 컨테이너에서 **131 passed in 0.14s**, `ruff check`·`format --check` 통과
- [x] 2.8 `PROMPT_DESIGN.md` 작성 — **PRD §4 필수 산출물**. 프롬프트의 각 구성 요소가 무엇을 막으려는 것인지, 환각 억제 전략(근거 밖 조회 금지·판정 줄·마커 강제), 거절 두 갈래의 분담, `PROMPT_VERSION`을 두는 이유. 실제 프롬프트 문자열을 그대로 인용한다
      → 프롬프트 전문은 `build_prompt`를 **실제로 실행해 받은 문자열**을 그대로 붙였다 — 손으로 옮기면 그 순간 문서-코드가 갈린다. 상단에 범위 노트를 달아 "재시도 정책·이벤트 시퀀스·인용 조립은 3~6장의 동작이고 지금 확인 가능한 것은 프롬프트와 파서까지"를 명시했다(허위 기재가 불합격 기준이라 없는 코드를 있다고 적을 자리를 먼저 닫았다). `--output-schema`를 안 쓰는 이유와 형식 위반 표도 함께 옮겼다. `README.md`에서의 링크는 7.3이 붙인다

## 3. LLM 어댑터 — Codex app-server

> 부품이 셋이다(design 결정 9). `session.py`가 프로세스 하나와 JSON-RPC를, `pool.py`가 여러 세션의 수명을, `codex.py`가 턴 하나를 맡는다. **`AnswerGenerator` 계약은 바뀌지 않는다** — `services/`도 `specs/`도 손대지 않는 것이 이 장의 합격 조건이다.

- [x] 3.1 `adapters/protocols.py`에 `AnswerGenerator` 추가 — `generate(prompt, *, timeout_seconds) -> AsyncIterator[str]`. 기존 프로토콜은 건드리지 않는다
      → 계약 한 줄이 그대로 들어갔다. docstring에 "쪼개지도 합치지도 않는다"와 "취소는 순회 종료"를 못 박았다 — 계약을 열어 둔 값을 이미 회수했다는 사실(exec → app-server 전환에 이 줄이 안 바뀜)도 함께 적었다
- [x] 3.2 `core/exceptions.py`에 `ErrorCode.LLM_UNAVAILABLE`·`LLM_UNAUTHENTICATED`와 `LlmTimeout`·`LlmUnauthenticated`·`LlmGenerationFailed` 추가 (기존 값 불변)
      → `LlmTimeout`과 `LlmGenerationFailed`가 **같은 코드**를 쓴다(`llm_unavailable`). 타임아웃은 그 자체로 스트림을 끝내지 않아 종료 코드가 될 자격이 없고, 구분이 필요한 자리는 `error` 이벤트의 사유다
- [x] 3.3 `api/errors.py`의 `_CODE_TO_STATUS`에 두 코드를 `503`으로 등록한다 (오늘 타는 경로는 없다 — 이유는 design 결정 12)
      → 표에 "오늘 이 매핑을 타는 경로는 없다"는 사실과 등록하는 이유(표에 없는 코드는 `500`)를 주석으로 남겼다
- [x] 3.4 `adapters/llm/session.py` — 프로세스 기동과 핸드셰이크. `codex app-server`를 띄우고 `initialize` → `initialized`. **환경은 상속하지 않고 `HOME`·`PATH`·`CODEX_HOME`만** 넘긴다. 기동+핸드셰이크 상한을 건다
      → `SessionLaunch.env`에 **기본값을 두지 않아** 상속 금지를 구조로 만들었다(값을 고르는 일은 6.2 배선의 몫 — `core/`·어댑터가 환경을 직접 읽는 것은 린트로도 막혀 있다). 설계에 없던 것 하나: `limit=4MiB`. 기본 64 KiB로는 app-server가 **되돌려 주는 `userMessage`**(우리 프롬프트 전문, 최악 30 KB대)에서 `readline`이 죽는데, 그 실패는 큰 문맥에서만 난다
- [x] 3.5 `session.py` — JSON-RPC 송수신. stdout을 **줄 단위로** 읽는 백그라운드 태스크가 `id` 있는 메시지는 대기 중인 응답에 꽂고, `method` 있는 메시지는 **`params.threadId`로 갈라** 큐에 넣는다
      → 1B.5가 경고한 대로 **`threadId` 없는 알림은 큐에 넣지 않고 로그로만** 남긴다. 넣으면 턴 하나가 남의 알림을 읽는다. 요청 상한을 `request`가 아니라 **호출자**가 걸게 했다 — `thread/start`와 `turn/start`가 한 시도의 예산 하나를 나눠 쓰므로, 요청마다 상한을 주면 합이 예산을 넘는다
- [x] 3.6 `session.py` — 생존 판정과 회수. stdout EOF·프로세스 종료를 사망으로 본다. 회수는 `terminate()` → 유예 → `kill()` → **반드시 `wait()`**
      → 사망을 **기다리는 모두에게** 알린다(대기 중인 응답 + 열려 있는 큐에 `None` 센티널). 안 그러면 즉시 알 수 있는 사실이 각자의 타임아웃으로 둔갑해 재시도가 잘못된 사유로 돈다. `wait()` 누락은 3.18의 PID 단언이 잡는다 — 좀비는 신호 0에 여전히 응답한다
- [x] 3.7 `session.py` — stderr를 별도로 소비해 로그에만 남긴다(응답에 싣지 않는다). **세션이 오래 살아 버퍼가 쌓이므로 읽지 않으면 어느 순간 조용히 멎는다**
      → 마지막 20줄만 들고 있다(무한정 들고 있으면 오래 사는 세션에서 그 자체가 누수다). 폐기 시 마지막 한 줄만 디버그 로그로 남긴다
- [x] 3.8 `adapters/llm/pool.py` — 지연 기동 풀. 상한은 `qa_concurrency`. 상한에 걸리면 **실패가 아니라 대기**. 빌려줄 때와 반납할 때 **양쪽에서 생존을 확인**하고, 죽었으면 폐기 후 새로 기동한다
      → 자리 반납을 `discard`의 `finally`에 두었다. 프로세스 하나를 못 죽인 대가가 "그 뒤로 동시 생성이 하나 줄어든 서비스"가 되면 안 된다 — 상한이 조용히 0으로 수렴하는 경로를 닫았다
- [x] 3.9 `adapters/llm/codex.py` — 턴 하나. 세션을 빌려 `thread/start`(빈 임시 `cwd`·`sandbox: read-only`·`approvalPolicy: never`·`ephemeral: true`) → `turn/start`(프롬프트는 `input`, argv 아님) → 델타 yield
      → **구독이 `turn/start`보다 먼저**여야 한다(뒤로 가면 그 사이 델타가 주인 없는 알림으로 버려진다 — 첫 글자가 사라지는 형태로만 드러난다). 빈 작업 디렉터리는 **첫 호출에** 만든다: 배선이 어댑터를 만드는 것만으로 파일시스템을 건드리면 3.15가 깨진다
- [x] 3.10 `codex.py` — 델타를 **글자 그대로** yield 한다. `item.completed`의 완성본은 출력에 쓰지 않되, **그 아이템에 델타가 0건일 때만** 한 번 yield 한다(델타 회귀 시 빈 답변이 아니라 exec 시절 동작으로 degrade)
      → 판정 전부를 `TurnReader`라는 **순수 상태 기계**에 모았다(`VerdictSplitter`와 같은 배치). 프로세스도 큐도 모르므로 3.17이 저장된 실물 알림을 그대로 먹여 회귀를 고정한다. `item.type != "agentMessage"` 필터가 없으면 우리 프롬프트가 답변 자리에 앉는다
- [x] 3.11 `codex.py` — 타임아웃. 상한 초과 시 `turn/interrupt` → 유예 안에 `turn/completed`가 오면 세션 반납, 아니면 폐기. 어느 쪽이든 `LlmTimeout`. **`finally`에 두어 취소(순회 중단)에도 같은 정리가 돈다**
      → 취소 경로에 함정이 하나 있었다 — 취소된 요청의 `finally`에서 그냥 `await`하면 그 대기도 즉시 취소되어 **중단 요청을 보내기도 전에** 정리가 끝난다. `shield`로 감쌌다(취소는 받아들이되 정리는 끝까지 돈다). `turn/interrupt`는 응답을 기다리지 않는다 — 중단의 성공은 뒤따라오는 `turn/completed`로 관측되므로 기다리면 같은 유예를 두 번 쓴다
- [x] 3.12 `codex.py` — 인증 판정. `error` 알림의 `codexErrorInfo...httpStatusCode == 401`이면 **`willRetry`와 무관하게 즉시** `turn/interrupt` + `LlmUnauthenticated`. `willRetry`는 401이 **아닌** 오류에만 적용해 로그로만 남긴다
      → 변형 이름(`responseStreamDisconnected`)을 상수로 박지 않고 한 겹 안의 값들을 훑는다 — 실험 단계 표면에서 가장 먼저 바뀔 부분이고, 우리가 아는 사실은 "상태 코드가 한 겹 안에 있다"까지다. `codexErrorInfo`가 문자열 `"other"`로 오는 회차를 3.17이 직접 덮는다
- [x] 3.13 `codex.py` — 종료 판정. `turn/failed`는 **없다**. `turn/completed`의 `turn.status`를 보고 실패면 `LlmGenerationFailed`
      → 실패한 턴도 `completed = True`로 표시한다. 반납의 조건은 성패가 아니라 **확실히 끝났는가**이기 때문이다(3.14)
- [x] 3.14 반납 불변식 — `turn/completed`를 받았을 때만 반납하고 **그 외에는 전부 폐기**. 애매한 세션을 반납하면 다음 요청이 이전 턴의 델타를 받는다
      → `_settle` 한 곳에 가뒀다 — 정상 종료·타임아웃·취소·인증 실패가 전부 같은 함수를 지난다. 경로마다 판정을 복사하면 그중 하나만 관대해져도 답변이 섞인다
- [x] 3.15 지연 초기화 확인 — 어댑터·풀 **생성**이 CLI를 건드리지 않는다. 기존 `tests/test_boot.py`가 계속 통과하는 것이 합격 조건
      → `test_boot.py` 통과(전체 578 passed). 여기에 더해 "풀과 생성기를 만들어도 기동 횟수가 0"을 3.18의 카운터로 직접 단언했다 — 배선은 6.2가 붙이므로, 그때 이 성질이 깨지면 이 테스트가 먼저 깨진다
- [x] 3.16 `tests/fake_app_server.py` — 같은 줄 단위 JSON-RPC를 말하는 가짜 서버. 인자로 망가지는 방식을 지시받는다: `--no-handshake`·`--die-mid-turn`·`--hang`·`--ignore-interrupt`·`--auth-401`·`--slow-deltas N`
      → 목록 전부 + `--pidfile`. 핸드셰이크에 실패한 회차는 **파이썬 쪽에 세션 객체가 남지 않아** 자식 회수를 확인할 통로가 그것뿐이다. 실물처럼 `threadId` 없는 `thread/started`를 함께 보내 3.5의 라우팅 규칙을 실제로 태운다
- [x] 3.17 `tests/test_llm_session.py` — 1B.5의 저장된 알림 샘플로 파싱 검증: 델타에서 답변 텍스트가 나오는가, 401 샘플이 `LlmUnauthenticated`가 되는가, **깨진 JSON 줄이 파서를 죽이지 않는가**
      → 목록 전부 + 1B.5가 경고한 함정 셋(프롬프트가 `userMessage`로 되돌아옴 / `codexErrorInfo`가 문자열 / `turn/failed`는 없음)을 각각 한 테스트로. **401을 몇 번째 알림에서 알 수 있었는가**를 단언한다 — 초를 재면 기계 속도에 묶이지만 순서는 프로토콜의 성질이라, "CLI에 맡기면 19초, 첫 401을 보면 2초"가 회귀로 고정된다
- [x] 3.18 `tests/test_llm_pool.py` — 가짜 서버로 수명 검증: 핸드셰이크 실패, 턴 도중 사망, 타임아웃 시 자식이 남지 않는가, `--ignore-interrupt`에서 세션이 폐기되는가, 순회를 중간에 멈추면 정리되는가, 상한에 걸린 요청이 **실패하지 않고 대기**하는가
      → 목록 전부 + "환경을 상속하지 않는다"(가짜 서버가 `initialize` 응답에 자기 환경을 되비쳐 주고, 부모에 심어 둔 센티널이 거기 없음을 단언). 타임아웃 회차는 **프로세스가 살아 있어야** 통과한다 — 상한이 세션을 죽이지 않는다는 결정이 테스트로 고정됐다. 29건 전부 구독·네트워크 없이 3.5초
- [x] 3.19 `tests/test_llm_pool.py` — 재사용: 두 번째 요청이 새 프로세스를 띄우지 않는가(기동 횟수 카운터), 죽은 세션이 **빌려지지 않는가**
      → 기동 횟수가 재사용을 관측하는 **유일한** 창이다(두 번째가 빨랐다는 것만으로는 프로세스를 다시 띄웠는지 알 수 없다). 상한 대기 테스트도 같은 카운터로 판정한다 — 상한 1에 두 요청이 모두 성공하고 기동이 1회면 차례로 썼다는 뜻이다

## 4. QA 서비스 — 오케스트레이션과 정책

- [ ] 4.1 `services/qa.py` — `QaEvent` 값 객체 4종(`sources`·`answer`·`done`·`error`)과 `QaContext`
- [ ] 4.2 `prepare(question, top_k)` — 검색까지. `RetrievalService`를 호출하고 예외를 그대로 올린다 (design 결정 3)
- [ ] 4.3 `stream(context)` — `sources` 먼저 yield → 근거 0건이면 생성기 호출 없이 `no_evidence`로 종료(**`answer` 이벤트 0회, `done.answer`는 빈 문자열 — 서비스가 문구를 만들지 않는다**) → 아니면 프롬프트 조립 후 생성
- [ ] 4.4 **판정 줄 버퍼링** — 2.4의 `VerdictSplitter`를 **구동만** 한다(로직은 `core/`에 있다). 확정 뒤 조각은 변형 없이 그대로 전달한다(다시 모으지도, 합치지도, 쪼개지도 않는다). 판정 줄은 `answer` 이벤트와 `done.answer` 어디에도 실리지 않으며, 판정 줄이 없는 출력은 전체가 본문이라 첫 줄이 잘리지 않는다
- [ ] 4.5 재시도 정책 — 최대 시도 수, 지수 백오프, 재시도 대상/비대상 분류(design 결정 7의 표). **`answer` 이벤트를 하나라도 내보낸 뒤에는 재시도하지 않는다**
- [ ] 4.6 종료 조립 — 판정에 따른 `finish_reason`, 마커 검증 결과로 `citations`와 `dropped_markers`, `elapsed_ms`. `INSUFFICIENT`면 마커를 무시한다. **본문이 빈 생성 출력은 성공이 아니라 생성 실패로 분류해 4.5의 재시도에 태운다** — `stop`·`insufficient_evidence`로 끝나지 않는다
- [ ] 4.7 시도 소진·인증 부재를 `error` 이벤트로 변환 — `code`·`message`·`attempts`·`reason`
- [ ] 4.8 동시 생성 세마포어(`qa_concurrency`) — 상한에 걸리면 실패가 아니라 대기. 수집과 같은 규율
- [ ] 4.9 로깅 — 요청 식별자·근거 수·대상 문서 수·종료 사유·인용 수·버려진 마커 수·시도 횟수·소요 시간. **질문·근거 본문·답변 본문은 남기지 않는다**
- [ ] 4.10 `tests/stubs.py` — 스크립트된 페이크 생성기: 조각 목록, 조각 사이 지연, N번째 호출에서 던질 예외, **호출 횟수 카운터**. 스펙의 THEN 대부분이 이 카운터로 관측된다
- [ ] 4.11 `tests/test_qa_service.py` — 이벤트 시퀀스: 순서, 종료 이벤트가 정확히 하나, 조각을 이어 붙이면 `done.answer`와 같음, 서버가 조각을 더 쪼개지 않음
- [ ] 4.12 `tests/test_qa_service.py` — **판정 줄 버퍼링**: 판정 줄과 본문이 한 조각일 때 이벤트 1회, 판정 줄이 두 조각에 걸치면 확정 전 이벤트 0회·확정 후 조각 수만큼, 확정 뒤 조각이 글자 그대로 전달되는가, 판정 줄 없는 출력의 첫 줄이 보존되는가
- [ ] 4.13 `tests/test_qa_service.py` — 거절 두 갈래: 근거 0건이면 생성기 호출 0회·`no_evidence`·`answer` 이벤트 0회·`done.answer`가 빈 문자열·이벤트가 `sources`와 `done` 둘뿐, 근거 있고 판정이 `INSUFFICIENT`면 `insufficient_evidence`·`answer`가 비어 있지 않음·`citations` 비어 있음·`sources`는 그대로 나가고 거절 문구에서 판정 줄이 제거됨
- [ ] 4.14 `tests/test_qa_service.py` — **빈 `answer`의 유일성**: `stop`·`insufficient_evidence`의 `answer`가 비어 있지 않은가, 판정 줄만 오는 출력이 재시도에 태워지는가(첫 시도만 그러면 호출 2회로 `stop`, 전 시도가 그러면 `error`로 끝나고 `done`이 없음)
- [ ] 4.15 `tests/test_qa_citations.py` — 인용 검증: 등장 순서, 중복 1건 처리, 범위 밖 폐기와 `dropped_markers`, 본문에 마커가 남음, 인용 값이 `sources` 항목과 일치, 마커 없는 답변도 버려지지 않음
- [ ] 4.16 `tests/test_qa_retry.py` — 재시도: 첫 시도 타임아웃 후 성공(호출 2회), 전 시도 실패 시 `error`·`attempts`·`reason`, 백오프 두 번째가 첫 번째보다 김, **조각을 내보낸 뒤 실패하면 호출 1회로 끝남**, 인증 부재는 호출 1회에 다른 코드

## 5. API — SSE 엔드포인트

- [ ] 5.1 `api/sse.py` — `QaEvent` → SSE 프레임(`event:`/`data:`/빈 줄) 직렬화와 하트비트 주석(`: keep-alive`) 생성기
- [ ] 5.2 `api/routes/qa.py` — 요청 모델(`question`, `top_k`). 검증은 `/search`와 **같은 코드·같은 형식**을 쓴다 (`empty_query`·`query_too_long`·`invalid_top_k`·`validation_error`)
- [ ] 5.3 `POST /qa` — `prepare`를 await 한 뒤 `StreamingResponse`를 만든다. 검증·저장소 실패가 스트림 **밖에서** 상태 코드로 끝나는 것이 이 태스크의 합격 조건
- [ ] 5.4 하트비트 배선 — 이벤트 사이 간격이 설정값을 넘으면 주석을 내보낸다. 이벤트로 해석되지 않아야 한다
- [ ] 5.5 클라이언트 연결 종료 시 생성이 중단되고 자식 프로세스가 정리되는지 확인한다
- [ ] 5.6 `tests/qa_harness.py` — SSE 응답을 이벤트 목록으로 파싱하는 헬퍼(주석은 별도로 센다). 시퀀스 단언이 전부 이 헬퍼 위에 선다
- [ ] 5.7 `tests/test_qa_api.py` — 성공 경로: `200`, `text/event-stream`, 이벤트 이름 순서, `sources`가 검색 응답과 같은 모양
- [ ] 5.8 `tests/test_qa_api.py` — 스트림 밖 실패: 빈 질문·상한 초과 질문·상한 초과 `top_k`·저장소 실패가 각각 `422`/`422`/`422`/`503`이고 콘텐츠 타입이 `text/event-stream`이 **아니며**, 생성기가 호출되지 않는다
- [ ] 5.9 `tests/test_qa_api.py` — `sources`가 생성보다 먼저 도착하는가(생성 지연을 페이크로 만들고 시각 차로 관측), 하트비트가 수신되지만 이벤트 목록에는 없는가
- [ ] 5.10 `tests/test_concurrency_api.py` 보강 — 생성이 진행 중일 때 `/health`가 생성 완료 전에 응답하는가

## 6. 배선과 설정

- [ ] 6.1 `config.py` — `qa_llm_timeout_seconds`·`qa_llm_max_attempts`·`qa_llm_retry_backoff_seconds`·`qa_sse_heartbeat_seconds`·`qa_concurrency`·`qa_llm_model`·`qa_llm_interrupt_grace_seconds`·`qa_llm_session_startup_timeout_seconds` 추가. 전부 기본값 있음
- [ ] 6.2 `main.py` — 세션 풀·생성기와 `QaService` 배선, `/qa` 라우터 등록. **부팅 경로에서 CLI를 건드리지 않는다** — 풀은 지연 기동이라 첫 요청까지 프로세스가 뜨지 않는다. 종료 시 풀의 세션을 회수한다
- [ ] 6.3 `tests/test_config.py` 보강 — 새 설정이 기본값으로 로딩되고 환경변수로 덮이는가
- [ ] 6.4 `tests/test_boot.py` 보강 — 자격증명이 **없는** 상태와 **판독 불가능한** 상태 양쪽에서 기동·`/health` 200이 유지되고, 기동 중 생성기가 호출되지 않는가

## 7. 문서 정합

> 문서-코드 불일치는 감점, 허위 기재는 불합격이다. 각 문서는 대응 구현이 끝난 직후에 고친다.

- [ ] 7.1 `ARCHITECTURE.md` — 상단 "현재 구현 범위"에서 "답변 생성은 아직 없습니다"를 걷어내고, LLM SDK 칸의 "**아직 없음**"을 실제 사용처로 바꾼다
- [ ] 7.2 `ARCHITECTURE.md` — 답변 생성 절 추가: 이벤트 시퀀스, 스트림 안/밖 실패 경계, 거절 두 갈래, 인용 검증, 재시도 정책, 세션 풀과 에이전트를 좁히는 조치들. **1장·1-B장의 실측 숫자를 함께 적는다**(1B.7에서 실측 절을 이미 고쳤다면 그와 어긋나지 않게)
- [ ] 7.3 `README.md` — `/qa` 사용법(`curl -N` 예시와 실제 이벤트 출력), 인증 없는 환경에서 무엇이 되고 무엇이 안 되는지, 실물 CLI 테스트를 포함해 돌리는 명령
- [ ] 7.4 `openspec/project.md` — 기술 스택 표와 외부 의존성 표가 아직 `claude-code-sdk`로 적혀 있다. Codex로 맞추고 **갈아탄 이유를 한 줄로 남긴다**(`ARCHITECTURE.md` 참조)
- [ ] 7.5 `PROMPT_DESIGN.md`가 실제 프롬프트와 일치하는지 다시 확인한다 (2.8 이후 프롬프트를 손봤다면 반영)
- [ ] 7.6 `docs/superpowers/specs/2026-08-03-codex-app-server-streaming-design.md`가 최종 구현과 어긋나지 않는지 확인한다 — 구현 중 설계를 바꿨다면 그 문서에도 남긴다

## 8. 마무리 검증

- [ ] 8.1 `pytest` 한 줄이 **구독·네트워크 없이** 전부 통과하는지 확인한다. 실물 CLI 테스트가 기본 실행에서 빠지는지도 함께 확인
- [ ] 8.2 `ruff` 통과. 특히 `core/`가 표준 라이브러리만 쓰는지, 계층 역전 import가 없는지
- [ ] 8.3 `docker compose up` 후 `sample-docs/` 두 건을 올리고 `/qa`를 실제로 호출해 답변과 인용을 확인한다 — 실물 경로가 도는 것을 눈으로 본다
- [ ] 8.4 자격증명을 치운 상태로 같은 절차를 반복해 기동·`/health`·`/documents`·`/search`가 정상이고 `/qa`만 `llm_unauthenticated`로 끝나는지 확인한다
- [ ] 8.5 `openspec validate --strict`로 이 change의 산출물을 검증한다
