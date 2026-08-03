> 이 변경은 백엔드 핵심 3경로(ingestion·retrieval·캐시 무효화)를 건드리지 않으므로 pytest 태스크가 없다.
> 대신 각 구현 묶음 끝에 **spec 시나리오를 직접 밟는 검증 태스크**를 두고, 최종 검증은 `demo-ui/DESIGN.md`의
> QA 체크리스트로 한다. 검증 태스크 없이 넘어가는 구현 묶음은 없다.
>
> 묶음 1~2 / 3~4 / 5~6 / 7~8 이 각각 하루치 커밋 단위다.

## 1. 프로젝트 스캐폴드

- [x] 1.1 `demo-ui/`에 Vite + React + TypeScript 프로젝트를 만든다. `npm create vite` 대신 설정 파일을 직접 작성했다 — 대화형이고 지울 보일러플레이트만 만든다
- [x] 1.2 `vite.config.ts`에 `server.proxy`로 `/api` → `VITE_API_TARGET ?? "http://127.0.0.1:8000"` 프록시를 설정하고 `rewrite`로 `/api` 접두사를 벗긴다 (design D1)
- [x] 1.3 폰트를 로컬 번들로 넣는다. **`pretendard` 대신 `@fontsource/pretendard`** 를 썼다 — 웨이트별 진입점(`400.css`·`600.css`)이 문서화돼 있어 필요한 두 벌만 넣을 수 있다 (design D6)
- [x] 1.4 루트 `.gitignore`에 `node_modules/`·`demo-ui/.vite/`를 추가한다 (`dist/`는 기존 Python 규칙이 이미 잡는다). **`npm install` 전에** 했다 — 추적되지 않은 파일이 `ruff check .`를 깨뜨린 전례가 있다
- [x] 1.5 `.dockerignore`에 `demo-ui/`를 추가한다. 빼지 않으면 상시 실행하는 `docker compose run --build --rm test`의 빌드 컨텍스트에 `node_modules`가 딸려간다 (계획에 없던 항목 — 조사 중 발견)
- [x] 1.6 `npm run dev`로 화면이 5173에서 뜨고 `/api/health`가 200 JSON을 돌려주는 것을 확인한다. **접속은 `localhost:5173`** 이다 — Vite가 그 이름으로 바인딩해 `127.0.0.1`로는 닿지 않는다

## 2. 토큰과 기본 UI 프리미티브

- [x] 2.1 `src/styles/tokens.css`에 원시 토큰(브리프 값 그대로) + 의미 토큰 2층을 정의한다 (design D5)
- [x] 2.2 조정이 필요한 토큰을 처리한다. 계획한 5건에 더해 **경계 대비·마이크로 간격·마이크로 모서리 3건이 추가로 필요**했다 (총 8건, `demo-ui/DESIGN.md` §2.2)
- [x] 2.3 상태 색 4종 + 초점 링 + 조작 요소 경계색을 정의하고 대비율을 실측해 기록한다. 13개 조합 전부 계산했고 `DESIGN.md` §2.1의 수치와 일치하는지 재검증했다
- [x] 2.4 `reset.css`·`global.css`에 기본 타이포·배경·`:focus-visible`·`prefers-reduced-motion`을 넣는다. 한국어용 `word-break: keep-all`도 여기 있다 — 없으면 단어 가운데가 잘린다
- [x] 2.5 `components/ui/`에 `Button`·`Badge`·`EmptyState`를 만들고 7상태를 정의한다. **`Field`는 별도 컴포넌트로 빼지 않고** `QuestionForm`이 직접 갖는다 — 입력이 화면에 하나뿐이라 추상화할 두 번째 사용처가 없다
- [x] 2.6 `demo-ui/DESIGN.md` 작성 (7.1에서 완성)
- [x] 2.7 검증: 토큰 파일 밖에 원시 색·간격 값이 없는지 `grep`으로 확인한다. **처음엔 실패했다** — 원시 hex 2건, px 15건이 남아 있어 파생 토큰을 추가해 해소했다. 지금은 `.srOnly`의 `margin: -1px` 한 줄만 남고 그 예외를 `DESIGN.md` §2.5에 기록했다

## 3. API 클라이언트와 SSE 파서

- [x] 3.1 `src/api/types.ts`에 서버 응답 타입을 옮긴다
- [x] 3.2 검증: 각 타입을 `src/app/api/`의 Pydantic 모델과 필드 단위로 대조한다. `dependencies`가 배열이 아니라 **이름을 키로 하는 객체**이고 벡터 스토어 키가 `vector_store`(하이픈 아님)인 것을 여기서 잡았다
- [x] 3.3 `src/api/client.ts` — `fetch` 래퍼. 오류 봉투를 `AppError`로 변환하고 네트워크 실패를 `network_unreachable`로 구분한다
- [x] 3.4 `src/api/sse.ts` — 프레임 파서. `"\n\n"` 경계, 부분 프레임 버퍼 보존, `":"` 주석 폐기, CRLF 정규화 (design D2)
- [x] 3.5 `TextDecoder`를 `{stream: true}`로 쓴다
- [x] 3.6 검증: 한글 답변을 실제로 스트리밍해 조각 경계에서 깨진 문자가 없고 `: keep-alive`가 이벤트로 새지 않는 것을 확인한다. 토큰 델타가 4.27s·4.36s·4.45s처럼 각각 다른 시각에 도착하는 것도 함께 실측했다 — **프록시가 버퍼링하지 않는다는 증거**

## 4. 문서 패널

- [x] 4.1 `hooks/useDocuments.ts` — 목록·업로드·삭제, 성공 후 재조회
- [x] 4.2 `DocumentPanel`·`DocumentRow` — 파일명·형식·청크·크기·색인 상태. 삭제는 확인 단계를 거친다
- [x] 4.3 `Dropzone` — 파일 선택과 드래그 앤 드롭. `label` + 숨은 `input`이라 키보드 조작을 브라우저에서 받는다
- [x] 4.4 업로드 응답의 `status` 4종과 `previous_revision`을 구분해 표시한다
- [x] 4.5 업로드 실패 문구를 오류 코드별로 나눈다 (미지원 포맷·용량 초과·텍스트 없음·빈 문서·파싱 실패·저장소 불능)
- [x] 4.6 문서 0건 빈 상태
- [ ] 4.7 검증: spec `문서 패널이 수집·목록·삭제를 수행한다`의 시나리오 5개를 직접 밟는다 — **업로드·삭제는 사용자가 UI로 확인했고, 재업로드 "무변경"·미지원 포맷·빈 상태 3건이 남았다**

## 5. Q&A 콘솔과 스트리밍

- [x] 5.1 `hooks/useQaStream.ts` — design D3의 상태 기계. 요청당 `AbortController` 하나, 새 질문 시 이전 것 abort 후 상태 교체
- [x] 5.2 `answer` 조각을 배열에 누적하고 렌더에서 합친다
- [x] 5.3 종료 이벤트 없이 리더가 끝나면 실패로 확정한다
- [x] 5.4 `QuestionForm` — 전송·중단. Enter 전송/Shift+Enter 줄바꿈이되 **IME 조합 중 Enter는 가로채지 않는다** (한글 마지막 글자가 잘린다)
- [x] 5.5 `SourceList` — 첫 조각보다 먼저 렌더. 긴 본문 접힘 + 펼치기
- [x] 5.6 `AnswerStream`·`AnswerText` — 조각 즉시 반영, `done` 후 검증된 마커만 링크 (design D4)
- [x] 5.7 `CitationList` — `id="citation-n"` + `tabIndex={-1}`
- [x] 5.8 `dropped_markers > 0`이면 그 수를 표시한다
- [x] 5.9 거절 상태 — `no_evidence`/`insufficient_evidence`/`target_documents === 0`을 세 갈래로 가른다
- [x] 5.10 `StatusBanner` — 실패 6종을 구분된 문구로. 검증 실패는 상한 값을 함께 보여준다
- [ ] 5.11 검증: 4개 요구사항의 시나리오를 전부 밟는다 — **스트리밍 도착·마커 연결·관련 근거 없음 거절은 확인했고, 미인증·서버 미도달·연결 끊김·진행 중 새 질문 4건이 남았다.** 중단은 사용자가 UI로 확인했다

## 6. 헬스 표시와 접근성 마감

- [x] 6.1 `hooks/useHealth.ts` — 10초 폴링, 숨은 탭 건너뜀, 실패 시 즉시 `unknown`
- [x] 6.2 `HealthBadge` — 본문의 `status`·`dependencies`를 읽는다. 색만으로 구분하지 않는다
- [x] 6.3 답변 영역에 `aria-busy`·`role="region"`, 별도 `aria-live` 줄이 전이만 통지. 실패 시 `role="alert"`
- [x] 6.4 모든 조작 요소에 서술적 이름을 붙인다
- [x] 6.5 360px에서 세로 스택 + 오버플로 처리를 구현한다 (긴 파일명 줄임표, 청크 본문 `pre-wrap`, 메타 그리드 `auto-fit`)
- [ ] 6.6 검증: 마우스 없이 업로드 → 질문 → 전송 → 근거 이동 전 과정을 수행한다
- [x] 6.7 검증: 대비 13개 조합을 계산하고 `DESIGN.md` 기록값과 대조한다. **실제 렌더 화면에서의 측정은 남았다**
- [ ] 6.8 검증: OS 동작 축소를 켠 상태에서 전환 효과가 정지하는지 확인한다
- [ ] 6.9 검증: 360px 뷰포트에서 가로 스크롤이 없는지 확인한다

## 7. 문서화

- [x] 7.1 `demo-ui/DESIGN.md` 완성 — 컨텍스트·토큰(실측 대비율 포함)·컴포넌트 9종의 anatomy/variants/7상태/반응형/엣지 케이스·접근성 수용 기준 13개·문구와 톤·안티패턴·QA 체크리스트
- [x] 7.2 `README.md`에 데모 UI 절을 추가한다 — 한 줄 실행, Node 20+, `localhost` 접속, 선택 절차임, 미인증 환경에서 답변만 실패하는 것이 정상임. "현재 구현 범위" 표에도 행을 추가했다
- [x] 7.3 검증: `DESIGN.md`가 언급한 토큰 이름 20개가 전부 `tokens.css`에 정의돼 있는지 대조한다 (스크립트로 확인, 불일치 0건)
- [x] 7.4 `ARCHITECTURE.md`에 "데모 UI" 절 추가 — 백엔드 무수정, CORS 대신 dev 프록시를 쓴 이유, `EventSource`를 못 쓰는 이유, 화면이 새 사실을 만들지 않는다는 규약

## 8. 최종 검증

- [ ] 8.1 깨끗한 상태(`docker compose down -v` 후 `docker compose up`)에서 README 절차를 처음부터 한 번 밟는다
- [ ] 8.2 `demo-ui/DESIGN.md`의 QA 체크리스트를 전부 밟는다
- [x] 8.3 `npm run build`가 통과하고 타입 오류가 없는지 확인한다
- [x] 8.4 `git status`로 `src/app/` 아래에 변경된 파일이 없는지 확인한다 (변경 없음)
- [x] 8.5 ~~`python3 scripts/check_comments.py`~~ — **이 스크립트는 `.py`만 훑으므로 `demo-ui/`에는 효력이 없다.** 계획 단계의 오해였고, 주석 규칙은 사람이 읽어 확인했다
