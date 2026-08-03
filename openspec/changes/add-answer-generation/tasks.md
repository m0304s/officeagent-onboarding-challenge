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

## 2. 도메인 값 객체와 프롬프트

- [ ] 2.1 `core/answers.py` — `FinishReason`(`stop`·`no_evidence`·`insufficient_evidence`), `Citation`, 조립된 답변 값 객체. 표준 라이브러리만
- [ ] 2.2 `core/prompting.py` — `PROMPT_VERSION` 상수와 `build_prompt(question, sources)`. 문맥 블록은 `[n] 파일명 (위치)` + 본문, 지시문에 "제공된 근거 밖을 조회하지 말라"와 출력 형식(`VERDICT:` 첫 줄, 근거 문장 끝 `[n]`)을 명시
- [ ] 2.3 `core/prompting.py` — `parse_answer(raw, source_count)`. 판정 줄 파싱(누락 시 `ANSWERABLE` + 경고), 마커 추출(등장 순서·중복 제거·범위 밖 폐기), 버려진 마커 수 반환
- [ ] 2.4 `tests/test_prompting.py` — 프롬프트 조립 회귀: 문맥에 모든 근거가 번호와 함께 들어가는가, 파일명·위치가 실리는가, 질문이 들어가는가, 출력 형식 지시가 있는가. **LLM 없이 문자열 단언으로만**
- [ ] 2.5 `tests/test_prompting.py` — 파서 경계: 판정 두 값, 판정 줄 누락, 마커 없음, 중복 마커, 범위 밖 마커, 빈 본문. design 결정 4의 표를 그대로 덮는다
- [ ] 2.6 `PROMPT_DESIGN.md` 작성 — **PRD §4 필수 산출물**. 프롬프트의 각 구성 요소가 무엇을 막으려는 것인지, 환각 억제 전략(근거 밖 조회 금지·판정 줄·마커 강제), 거절 두 갈래의 분담, `PROMPT_VERSION`을 두는 이유. 실제 프롬프트 문자열을 그대로 인용한다

## 3. LLM 어댑터 — Codex

- [ ] 3.1 `adapters/protocols.py`에 `AnswerGenerator` 추가 — `generate(prompt, *, timeout_seconds) -> AsyncIterator[str]`. 기존 프로토콜은 건드리지 않는다
- [ ] 3.2 `core/exceptions.py`에 `ErrorCode.LLM_UNAVAILABLE`·`LLM_UNAUTHENTICATED`와 `LlmTimeout`·`LlmUnauthenticated`·`LlmGenerationFailed` 추가 (기존 값 불변)
- [ ] 3.3 `api/errors.py`의 `_CODE_TO_STATUS`에 두 코드를 `503`으로 등록한다 (오늘 타는 경로는 없다 — 이유는 design 결정 12)
- [ ] 3.4 `adapters/llm/codex.py` — 프로세스 기동. stdin으로 프롬프트 전달, 빈 임시 작업 디렉터리, 읽기 전용 샌드박스, **환경변수 최소 집합만** 전달(1.3·1.4의 결과 반영)
- [ ] 3.5 stdout을 **줄 단위로** 읽어 JSONL을 파싱하고 답변 텍스트 조각을 yield 한다. `communicate()`로 전부 모으지 않는다
- [ ] 3.6 타임아웃 — 상한 초과 시 `terminate()` → 유예 → `kill()` → `wait()`으로 회수하고 `LlmTimeout`. `finally`에 두어 취소(순회 중단)에도 같은 정리가 돌게 한다
- [ ] 3.7 stderr를 별도로 소비해 실패 시 로그에만 남긴다(응답에는 싣지 않는다). 읽지 않아 버퍼가 차는 교착을 막는 것도 겸한다
- [ ] 3.8 인증 판정 — 종료 코드 1차, 문구 매칭 2차. 문구는 상수 하나로 모으고 1.5의 실측값을 넣는다. 판정 실패 시 `LlmGenerationFailed`로 떨어진다
- [ ] 3.9 지연 초기화 확인 — 어댑터 **생성**이 CLI를 건드리지 않는다. 기존 `tests/test_boot.py`가 계속 통과하는 것이 합격 조건
- [ ] 3.10 `tests/test_llm_codex.py` — 1.7의 저장된 출력 샘플로 파싱 검증: 성공 출력에서 답변 텍스트가 나오는가, 인증 실패 출력이 `LlmUnauthenticated`가 되는가, 깨진 JSONL 줄이 파서를 죽이지 않는가
- [ ] 3.11 `tests/test_llm_codex.py` — 프로세스 수명: 타임아웃 시 자식 프로세스가 남지 않는가, 순회를 중간에 멈추면 프로세스가 정리되는가. 실제 CLI 대신 짧은 스크립트를 자식으로 띄워 검증한다

## 4. QA 서비스 — 오케스트레이션과 정책

- [ ] 4.1 `services/qa.py` — `QaEvent` 값 객체 4종(`sources`·`answer`·`done`·`error`)과 `QaContext`
- [ ] 4.2 `prepare(question, top_k)` — 검색까지. `RetrievalService`를 호출하고 예외를 그대로 올린다 (design 결정 3)
- [ ] 4.3 `stream(context)` — `sources` 먼저 yield → 근거 0건이면 생성기 호출 없이 `no_evidence`로 종료(**`answer` 이벤트 0회, `done.answer`는 빈 문자열 — 서비스가 문구를 만들지 않는다**) → 아니면 프롬프트 조립 후 생성
- [ ] 4.4 **판정 줄 버퍼링** — 판정이 확정될 때까지만 조각을 모으고, 확정되는 순간 그 조각의 본문 부분부터 내보낸다. 그 뒤 조각은 변형 없이 그대로 전달한다(다시 모으지도, 합치지도, 쪼개지도 않는다). 판정 줄은 `answer` 이벤트와 `done.answer` 어디에도 실리지 않으며, 판정 줄이 없는 출력은 전체가 본문이라 첫 줄이 잘리지 않는다
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

- [ ] 6.1 `config.py` — `qa_llm_timeout_seconds`·`qa_llm_max_attempts`·`qa_llm_retry_backoff_seconds`·`qa_sse_heartbeat_seconds`·`qa_concurrency`·`qa_llm_model` 추가. 전부 기본값 있음
- [ ] 6.2 `main.py` — 생성기와 `QaService` 배선, `/qa` 라우터 등록. **부팅 경로에서 CLI를 건드리지 않는다**
- [ ] 6.3 `tests/test_config.py` 보강 — 새 설정이 기본값으로 로딩되고 환경변수로 덮이는가
- [ ] 6.4 `tests/test_boot.py` 보강 — 자격증명이 **없는** 상태와 **판독 불가능한** 상태 양쪽에서 기동·`/health` 200이 유지되고, 기동 중 생성기가 호출되지 않는가

## 7. 문서 정합

> 문서-코드 불일치는 감점, 허위 기재는 불합격이다. 각 문서는 대응 구현이 끝난 직후에 고친다.

- [ ] 7.1 `ARCHITECTURE.md` — 상단 "현재 구현 범위"에서 "답변 생성은 아직 없습니다"를 걷어내고, LLM SDK 칸의 "**아직 없음**"을 실제 사용처로 바꾼다
- [ ] 7.2 `ARCHITECTURE.md` — 답변 생성 절 추가: 이벤트 시퀀스, 스트림 안/밖 실패 경계, 거절 두 갈래, 인용 검증, 재시도 정책, 에이전트를 좁히는 조치들. **1장의 실측 숫자를 함께 적는다**
- [ ] 7.3 `README.md` — `/qa` 사용법(`curl -N` 예시와 실제 이벤트 출력), 인증 없는 환경에서 무엇이 되고 무엇이 안 되는지, 실물 CLI 테스트를 포함해 돌리는 명령
- [ ] 7.4 `openspec/project.md` — 기술 스택 표와 외부 의존성 표가 아직 `claude-code-sdk`로 적혀 있다. Codex로 맞추고 **갈아탄 이유를 한 줄로 남긴다**(`ARCHITECTURE.md` 참조)
- [ ] 7.5 `PROMPT_DESIGN.md`가 실제 프롬프트와 일치하는지 다시 확인한다 (2.6 이후 프롬프트를 손봤다면 반영)

## 8. 마무리 검증

- [ ] 8.1 `pytest` 한 줄이 **구독·네트워크 없이** 전부 통과하는지 확인한다. 실물 CLI 테스트가 기본 실행에서 빠지는지도 함께 확인
- [ ] 8.2 `ruff` 통과. 특히 `core/`가 표준 라이브러리만 쓰는지, 계층 역전 import가 없는지
- [ ] 8.3 `docker compose up` 후 `sample-docs/` 두 건을 올리고 `/qa`를 실제로 호출해 답변과 인용을 확인한다 — 실물 경로가 도는 것을 눈으로 본다
- [ ] 8.4 자격증명을 치운 상태로 같은 절차를 반복해 기동·`/health`·`/documents`·`/search`가 정상이고 `/qa`만 `llm_unauthenticated`로 끝나는지 확인한다
- [ ] 8.5 `openspec validate --strict`로 이 change의 산출물을 검증한다
