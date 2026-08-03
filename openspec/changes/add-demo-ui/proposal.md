## Why

지금 이 서버의 동작을 눈으로 확인할 방법이 `curl` 뿐이다. 특히 PRD §2가 요구하는 **SSE 스트리밍**은
`curl -N` 으로는 "조각이 언제 도착했는가"가 드러나지 않아, 스트리밍이 실제로 1급 경로인지
평가자가 확인하기 어렵다. 출처 표기(citation)와 마커의 대응 관계, 근거 없는 질문의 거절 동작도
JSON 을 눈으로 읽어 대조해야 한다.

문서 업로드 → 근거 검색 → 스트리밍 답변 → 출처 확인을 한 화면에서 밟을 수 있는 데모 UI 를 둔다.
PRD §2(스트리밍·출처·환각 억제)의 **검증 수단**이지 새 백엔드 기능이 아니다.

## What Changes

- `demo-ui/` 에 Vite + React + TypeScript 단일 페이지 앱을 새로 만든다. 화면은 하나이고
  좌측 문서 패널 + 우측 Q&A 콘솔로 나뉜다.
- **문서 패널** — `POST /documents` 업로드(드래그 앤 드롭 + 파일 선택), `GET /documents` 목록,
  `DELETE /documents/{id}` 삭제. 업로드 응답의 `status`(최초 수집·교체·재색인·무변경)와
  `index_status` 를 그대로 드러낸다.
- **Q&A 콘솔** — 질문 입력 → `POST /qa` 를 `fetch` + `ReadableStream` 으로 소비해
  `sources`·`answer`·`done`·`error` 네 이벤트를 화면에 반영한다. 본문의 `[n]` 마커는
  `done` 이벤트의 `citations` 와 묶어 근거 카드로 연결한다.
- **근거 없음 처리** — `finish_reason` 이 `insufficient_evidence` 이거나 `sources` 가 0건이면
  답변 영역이 아니라 전용 안내 상태로 보여준다. 환각 억제가 UI 에서도 드러나야 한다.
- **헬스 표시** — `GET /health` 를 폴링해 의존성(cache·vector-store) 상태를 상단에 띄운다.
  LLM 미인증(`llm_unauthenticated`) 은 `error` 이벤트로만 드러나므로 그 경로도 별도 문구로 처리한다.
- `demo-ui/DESIGN.md` 에 토큰·컴포넌트 상태 규칙·접근성 수용 기준·QA 체크리스트를 문서화하고,
  토큰은 `src/styles/tokens.css` 의 CSS 커스텀 프로퍼티 한 곳에서만 정의한다.
- Vite dev 서버가 `/api` 를 `http://localhost:8000` 으로 프록시한다. **백엔드는 건드리지 않는다** —
  CORS 미들웨어를 추가하지 않는 이유는 데모 하나 때문에 API 의 배포 표면을 넓히지 않기 위해서다.
- `README.md` 에 데모 UI 실행 방법을 한 줄로 추가한다.

## Capabilities

### New Capabilities

- `demo-console`: 브라우저에서 문서를 수집하고, 질문에 대한 답변이 스트리밍으로 도착하는 과정과
  그 근거를 확인하는 데모 화면의 관측 가능한 동작.

### Modified Capabilities

없음. 기존 `document-ingestion`·`retrieval`·`service-health` 스펙과 `add-answer-generation` 의
`answer-generation` 스펙이 정의한 서버 동작은 그대로다. 이 변경은 그 동작을 소비하기만 한다.

## Impact

- **신규**: `demo-ui/` (Vite 프로젝트, Node.js 20+ 필요), `demo-ui/DESIGN.md`.
- **수정**: `README.md` (실행 절차에 데모 UI 항목 추가), `.gitignore` (`demo-ui/node_modules`, `demo-ui/dist`).
- **불변**: `src/app/**` — API 코드에 변경이 없다. 데모 UI 는 공개 계약(`/documents`·`/search`·`/qa`·`/health`)만 쓴다.
- **평가 영향**: 데모 UI 는 PRD 필수 산출물이 아니라 §2 검증 보조 수단이다. `docker compose up`
  기동 경로와 `pytest` 실행 경로에 끼어들지 않으므로, UI 가 깨져도 불합격 기준에 닿지 않는다.
- **의존성**: Node.js 20+ / npm. 프런트엔드 자동화 테스트는 두지 않는다 — PRD 의 테스트 배점은
  백엔드 핵심 3경로를 대상으로 하고, 여기에 프런트 테스트를 더해도 배점이 옮겨가지 않는다.
- **브랜드 브리프 조정**: 제공된 디자인 브리프는 표면을 "e-commerce storefront"·"online shoppers" 로
  적었지만 브리프 자체가 그 추론의 신뢰도가 낮다고 표기했고, 이 리포는 상거래 화면이 아니다.
  토큰·접근성 규칙·문서 구조는 그대로 따르고 표면만 "문서 Q&A 데모 콘솔"로 바꿔 적용한다.
