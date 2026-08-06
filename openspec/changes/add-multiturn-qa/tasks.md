## 1. 세션 저장소

- [ ] 1.1 `core/conversation.py` 에 값 객체를 둔다 — `Turn`(`question`, `search_query`, `answer`), `History`(턴들 + 다음 턴 번호). I/O 없는 순수 값 객체다
- [ ] 1.2 `adapters/protocols.py` 에 `ConversationStore` 프로토콜을 더한다 — `load(session_id)`, `append(session_id, turn)`, `new_session_id()`. 소비처가 생기는 지금 정의한다
- [ ] 1.3 `config.py` 에 설정 항목을 더한다 — 세션 TTL, 세션당 턴 상한, 턴당 문자 상한. 전부 기본값을 갖게 해 환경 변수 없이 기동되게 한다
- [ ] 1.4 `adapters/session/redis.py` 를 만든다 — `RPUSH` → `LTRIM(-N, -1)` → `EXPIRE` 를 파이프라인 한 번으로. 식별자는 `secrets.token_urlsafe`. 턴에 근거 청크 본문·인용·문서 식별자를 담지 않는다 (design.md 결정 1·2)
- [ ] 1.5 저장소 접근을 이벤트 루프 밖으로 돌린다 — 블로킹 호출이 남지 않게 한다 (`conversation-session`: 세션 접근이 다른 요청의 처리를 막지 않는다)
- [ ] 1.6 `main.py` 에서 배선하고, Redis 에 닿지 못하는 기동이 실패하지 않는지 확인한다
- [ ] 1.7 테스트 — TTL 만료, 마지막 활동 기준 수명 갱신, 턴 상한 초과 시 오래된 턴 폐기, 턴당 문자 상한 절단, 알지 못하는 식별자가 빈 히스토리로 처리됨, 두 세션의 식별자가 다름
- [ ] 1.8 테스트 — 저장소 조회·쓰기 실패가 `503` 이 되지 않고 축소 동작으로 끝나는지 (`conversation-session`: 세션 저장소에 닿지 못해도 질의응답은 성립한다)
- [ ] 1.9 `ARCHITECTURE.md` 에 세션 저장소 선택 근거를 적는다 — Redis LIST + EXPIRE 를 고른 이유와 SQLite·프로세스 메모리를 기각한 이유

## 2. 질의 재작성

- [ ] 2.1 `core/rewriting.py` — 재작성 프롬프트 조립(순수 함수)과 출력 파싱. 첫 번째 비어 있지 않은 줄을 취하는 "한 줄" 규약 (design.md 결정 4)
- [ ] 2.2 `config.py` 에 `qa_rewrite_timeout_seconds` 를 더한다. 답변 생성 timeout 보다 짧은 기본값
- [ ] 2.3 `services/rewriting.py` — `AnswerGenerator` 를 재사용해 조각을 문자열로 모으고, timeout·무재시도·원문 폴백 정책을 여기에 둔다 (design.md 결정 3·5)
- [ ] 2.4 재작성 결과가 `retrieval` 의 문자 수·토큰 상한을 넘으면 버리고 원문으로 폴백한다. `422` 로 거부하지 않는다 (`query-rewriting`: 재작성된 질의도 검색 질의의 상한을 지킨다)
- [ ] 2.5 테스트 — 히스토리가 비면(세션 없음·첫 턴·저장소 축소) 재작성 생성기가 한 번도 호출되지 않는지
- [ ] 2.6 테스트 — timeout 초과·생성 실패·읽을 수 없는 출력이 각각 원문 폴백으로 끝나고 요청이 `200` 인지, 재작성 호출이 정확히 한 번인지
- [ ] 2.7 테스트 — 상한을 넘는 사용자 질문은 여전히 `422 query_too_long` 이고 재작성이 호출되지 않는지
- [ ] 2.8 `PROMPT_DESIGN.md` 에 재작성 프롬프트의 설계 의도를 적는다 — 한 줄 규약을 고른 이유, 없던 사실을 만들지 않게 하는 지시, JSON 출력을 기각한 이유

## 3. 히스토리 주입

- [ ] 3.1 `core/prompting.py` 의 `build_prompt` 가 히스토리를 받아 `<대화>` 구역을 `<근거>` 앞에 놓게 한다. 히스토리 없는 호출은 지금과 글자 하나까지 같은 프롬프트를 만들어야 한다
- [ ] 3.2 지시문에 "`<대화>` 는 질문을 이해하기 위한 것이며 인용 대상이 아니다"를 더한다
- [ ] 3.3 `PROMPT_VERSION` 을 `qa-ko-1` → `qa-ko-2` 로 올린다 (design.md 결정 7)
- [ ] 3.4 마커 검증(`_validate_markers`)은 손대지 않는다 — 히스토리를 가리키는 인용이 구조적으로 만들어질 수 없음을 테스트로 고정한다
- [ ] 3.5 테스트 — 히스토리가 있어도 근거 0 건이면 생성기를 호출하지 않고 `no_evidence` 로 끝나는지, 앞 턴의 답변이 되풀이되지 않는지 (`answer-generation`: 히스토리가 있어도 근거가 없으면 답을 지어내지 않는다)
- [ ] 3.6 테스트 — 앞 턴이 인용한 문서를 삭제한 뒤 후속 질문의 `done` 에 그 문서를 가리키는 인용이 없는지
- [ ] 3.7 `PROMPT_DESIGN.md` 에 히스토리 주입 전략과 환각 억제 방어 셋(생성기 미호출·구역 분리·마커 범위 폐기)을 적는다

## 4. `/qa` 배선

- [ ] 4.1 `QaRequest` 에 `session_id: str | None` 을 더한다
- [ ] 4.2 `QaContext` 가 세션 식별자·턴 번호·검색에 쓰인 질의·축소 여부를 담게 한다
- [ ] 4.3 `QaService.prepare` 가 히스토리 조회 → 재작성 → 검색 순으로 돌게 한다. 앞 둘은 예외를 밖으로 내지 않는다 — `prepare` 의 상태 코드 의미가 달라지지 않아야 한다 (design.md 결정 6)
- [ ] 4.4 `DoneEvent` 에 `session_id`·`turn_index`·`search_query`·축소 표시를 더한다. 기존 `finish_reason`·`answer`·`citations`·`elapsed_ms` 는 그대로 둔다
- [ ] 4.5 턴 기록을 `done` 을 내보내기 **직전**에 둔다. 기록 실패는 삼키고 축소로 표시한다 (design.md 결정 6)
- [ ] 4.6 정상 종료로 답변이 나온 경우에만 기록한다 — `error`·클라이언트 중단·`no_evidence`·`insufficient_evidence` 는 기록하지 않는다
- [ ] 4.7 세션 식별자와 대화 본문의 로깅 규약을 맞춘다 — 식별자와 턴 수는 남기고 질문·답변 본문은 남기지 않는다
- [ ] 4.8 테스트 — 세션 지정 요청도 이벤트 종류·순서·횟수가 같은지, 조각을 이어 붙인 것이 `done.answer` 와 같은지
- [ ] 4.9 테스트 — 세션 미지정 요청이 세션 저장소에 접근하지 않고 이 change 이전과 같게 동작하는지 (회귀 방어선)
- [ ] 4.10 테스트 — `done` 의 식별자로 다음 턴을 보내면 턴 번호가 하나 커지는지, `error` 로 끝난 요청이 턴 번호를 진전시키지 않는지

## 5. 파이프라인 계약 불변 확인

- [ ] 5.1 `services/retrieval.py`·`adapters/embedding/`·`adapters/vector_store/`·`api/sse.py` 의 diff 가 비어 있는지 확인한다. 비어 있지 않으면 왜 손댔는지를 design.md 에 남기거나 되돌린다
- [ ] 5.2 기존 `retrieval`·`answer-generation` 테스트가 하나도 고쳐지지 않고 통과하는지 확인한다. 기존 테스트를 고쳐야 통과한다면 계약을 깬 것이다

## 6. 멀티턴 통합 테스트

- [ ] 6.1 `sample-docs/` 두 문서로 지시어 해소 시나리오를 건다 — 교육비를 물어 답을 받은 뒤 `"그거 더 자세히 알려줘"` 가 근거를 찾고 그 근거가 `company-policy.txt` 에서 오는지
- [ ] 6.2 주제 전환 시나리오 — 복리후생을 물은 뒤 `"코드 리뷰는 승인이 몇 명 필요해?"` 의 1위 근거가 `development-guide.md` 에서 오고, 검색 질의가 앞 턴에 끌려가지 않는지
- [ ] 6.3 답변 생성 LLM 과 재작성 LLM 을 모두 스텁으로 둔 채 전체 스위트가 구독 없이 `pytest` 한 줄로 통과하는지 확인한다

## 7. 문서와 검증

- [ ] 7.1 `README.md` 에 `session_id` 사용법(발급·이어 쓰기·만료)과 인증이 필요한 배포에는 별도 change 가 필요하다는 사실을 적는다
- [ ] 7.2 `ARCHITECTURE.md` 에 재작성 단계가 `prepare`/`stream` 경계 어디에 놓이는지와 그 이유를 적는다
- [ ] 7.3 `python3 scripts/check_comments.py` 로 주석 규칙을 통과시킨다
- [ ] 7.4 `docker compose run --build --rm test` 로 전체 테스트를, `docker compose run --build --rm test ruff check .` 로 린트를 확인한다. **`--build` 를 빠뜨리면 직전 이미지를 검사한다**
- [ ] 7.5 `docker compose up -d --build --wait` 로 띄우고 `./data` 를 비운 뒤 `sample-docs/` 두 건만 올려 멀티턴 대화를 실물로 한 번 밟는다
