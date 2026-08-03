## Context

동작 계약은 `specs/demo-console/spec.md`, 동기는 `proposal.md`에 있다. 여기서는 그 계약을 만족시키는
구조를 정한다.

설계를 좌우하는 기존 사실 넷:

1. **`/qa`는 `POST` + `text/event-stream`이다.** 브라우저의 `EventSource`는 `GET`만 보내므로 쓸 수 없다.
2. **스트림은 `sources` → `answer`* → (`done` | `error`) 네 이벤트로만 말한다.** 종료 이벤트는 정확히
   하나이며, 검색 실패는 스트림 밖(HTTP 상태 코드)에서, 생성 실패는 스트림 안에서 온다. 클라이언트는
   **두 실패 경로를 모두** 다뤄야 한다.
3. **서버는 CORS 미들웨어가 없다.** `src/app/main.py`는 `RequestLoggingMiddleware` 하나만 붙인다.
4. **캐시는 아직 없다.** `adapters/cache/`에는 헬스 프로브만 있다. `cache_hit`을 화면에 둘 근거가 없다.

## Goals / Non-Goals

**Goals:**

- SSE 조각의 **도착 시점**이 화면에서 드러나는 답변 영역.
- 답변 본문의 `[n]` 마커와 서버가 검증한 `citations` 사이의 대응을 클라이언트가 **다시 만들지 않고 연결**만 하기.
- 토큰 → 컴포넌트 한 방향 의존. 컴포넌트가 원시 색·간격 값을 갖지 않는다.
- 백엔드 무수정. 데모가 API 의 배포 표면을 넓히지 않는다.

**Non-Goals:**

- 프로덕션 정적 배포. `npm run build`는 동작하지만 서빙·프록시 구성은 만들지 않는다. 데모는 dev 서버로 돈다.
- 프런트엔드 자동화 테스트. 검증은 `demo-ui/DESIGN.md`의 QA 체크리스트를 사람이 밟는다.
- 상태 영속화·라우팅·다중 세션. 새로고침하면 대화는 사라진다. 서버 상태(문서·색인)만 남는다.
- 라이트 테마. 브리프의 토큰이 어두운 표면 기준 한 벌이라 한 테마만 둔다.

## Decisions

### D1. 동일 출처 문제는 Vite dev 프록시로 푼다 (백엔드 CORS 아님)

`vite.config.ts`의 `server.proxy`가 `/api/*` → `http://localhost:8000/*`로 넘긴다. 브라우저는 5173
한 출처만 보므로 프리플라이트도 CORS 헤더도 필요 없다.

- **대안 A: 서버에 `CORSMiddleware` 추가.** 데모 하나 때문에 API 의 공개 표면과 미들웨어 체인이
  영구히 넓어진다. 허용 출처를 잘못 열면 그건 데모가 아니라 서버의 결함이 된다. 기각.
- **대안 B: 브라우저 확장·`--disable-web-security`.** 평가자에게 요구할 수 없다. 기각.
- **대가**: 프록시는 dev 서버에만 있다. `dist/`를 정적으로 열면 API 호출이 실패한다 — Non-Goal 로 못박고
  `README.md`에도 dev 서버 경로만 적는다.

프록시 대상은 환경변수 `VITE_API_TARGET`으로 덮을 수 있게 두되 기본값을 `http://localhost:8000`으로
둔다. 기본값이 있어야 평가자가 아무 설정 없이 한 줄로 띄운다.

### D2. SSE 소비는 `fetch` + `ReadableStream` 수동 파서

`EventSource`가 `POST`를 못 보내므로 직접 판다. 파서는 순수 함수 하나(`parseSseChunk`)로 분리해 뷰와
섞지 않는다.

```
fetch("/api/qa", {method:"POST", signal})            // AbortController = 중단·교체 수단
  → res.body.getReader() → TextDecoder(stream:true)
  → 버퍼에서 "\n\n" 경계로 프레임 잘라내기
  → ":" 로 시작하는 줄은 버린다 (서버의 ": keep-alive" 주석)
  → "event:" / "data:" 를 모아 {name, data} 로
```

주의점 셋:

- **디코딩은 `stream: true`.** 한글이 UTF-8 3바이트라 청크 경계가 글자 가운데를 자른다. 옵션을 빠뜨리면
  조각 경계마다 깨진 문자가 섞이는데, 짧은 답변에서는 재현되지 않아 늦게 발견된다.
- **부분 프레임을 버퍼에 남긴다.** `"\n\n"`을 못 찾으면 다음 청크를 기다린다.
- **스트림이 종료 이벤트 없이 끝나면 실패로 간주한다.** 리더가 `done`을 주었는데 `done`/`error` 이벤트를
  본 적 없으면 "연결 끊김" 상태로 확정한다. 그러지 않으면 화면이 영원히 진행 중으로 남는다 (spec 참조).

**대안: `@microsoft/fetch-event-source` 같은 라이브러리.** 재연결·재시도를 얹어 주지만 이 서버는
재연결을 계약에 두지 않았고(질문 하나 = 스트림 하나), 자동 재연결은 같은 질문을 두 번 생성시킨다.
의존성 하나를 넣어 계약과 어긋나는 동작을 얻는 거래라 기각.

### D3. 한 질문의 수명을 상태 기계 하나로 (`useQaStream`)

```
idle → preparing → streaming → done | insufficient | error | aborted
                 ↘ error (HTTP 4xx/5xx: 검증 실패·서버 미기동)
```

- `preparing`: `fetch` 를 보냈고 첫 이벤트 전. 여기서 끝나는 실패가 **스트림 밖 실패**다.
- `streaming`: `sources` 를 받은 뒤. 여기서 끝나는 실패가 **스트림 안 실패**다.
- `insufficient`: `done` 인데 `finish_reason === "insufficient_evidence"` 이거나 `sources`가 0건.
  `done`과 갈라 두는 이유는 화면이 답변 영역을 아예 열지 않아야 하기 때문이다 (spec: 환각 억제).
- 훅은 요청 하나당 `AbortController` 하나를 들고, 새 질문이 오면 **이전 것을 먼저 abort** 한 뒤 상태를
  통째로 갈아끼운다. 조각이 섞이는 사고는 전부 "이전 컨트롤러를 안 끊었다"에서 나온다.
- `answer` 조각은 배열에 push 하고 렌더에서 `join("")` 한다. 문자열 누적을 상태에 두면 조각마다
  O(n) 복사가 쌓인다.

`useState` + `useReducer` 로 충분하다. 화면이 하나고 전역 공유 상태가 없어 상태 라이브러리를 넣지 않는다.

### D4. 마커 연결은 렌더 시점 파싱, 인용은 서버 것만 믿는다

`done`의 `citations[].marker` 집합을 만들고, 본문을 `/\[(\d+)\]/g` 로 쪼개 **그 집합에 있는 번호만**
`<a href="#citation-n">` 으로 만든다. 집합에 없는 `[n]` 은 평문으로 남긴다 — 서버가 이미
`dropped_markers` 로 세었고, 화면이 지우면 흘러간 문장과 최종 문장이 달라진다.

스트리밍 중에는 인용이 아직 없으므로 마커가 전부 평문이다. `done` 이 오면 같은 본문이 링크가 붙은
모습으로 한 번 다시 그려진다. 이 전환은 의도된 것이고, 스트리밍 중에 마커를 미리 링크로 만들면
검증되지 않은 마커까지 근거인 척하게 된다.

### D5. 토큰은 CSS 커스텀 프로퍼티 2층 (원시 → 의미)

`src/styles/tokens.css` 한 파일. 브리프의 값을 **원시 토큰**으로 그대로 옮기고, 컴포넌트는
**의미 토큰**만 참조한다.

```
--color-surface-base: #000000;              /* 원시: 브리프 값 그대로 */
--color-bg-page: var(--color-surface-base); /* 의미: 컴포넌트가 쓰는 이름 */
```

두 층으로 나누는 이유는 브리프의 이름이 역할이 아니라 **모양**을 가리키기 때문이다(`surface.raised`가
`#dddddd` — 어두운 화면에서 "올라온 면"이 아니라 밝은 카드다). 컴포넌트가 원시 이름을 직접 쓰면
값을 바꿀 때 이름이 거짓말을 시작한다.

브리프에서 **그대로 쓸 수 없어 조정한 것들** — 전부 `demo-ui/DESIGN.md`에 근거와 함께 적는다:

| 브리프 | 문제 | 처리 |
|---|---|---|
| `color.border.muted` = `rgb(23,30,44) rgb(229,231,235)` | 값이 둘이라 단일 토큰이 될 수 없다 | 어두운 표면용 `#171e2c`를 채택. 밝은 값은 `--color-border-on-raised`로 분리 |
| `font.size.md=56px`, `lg=86px` | 본문 위계가 아니라 디스플레이 크기다. 16→18 다음이 56이면 카드 제목·섹션 제목에 쓸 단이 없다 | 원시는 보존하고 의미 층에 `--font-size-title: 28px`, `--font-size-heading: 20px`를 **파생 토큰**으로 추가 |
| `font.weight.base=600` | 본문 전체 600은 장문 가독성이 떨어진다 | 본문은 400, `--font-weight-emphasis: 600`을 브리프 값으로 유지 |
| 강조색 없음 | 기본 액션·성공·경고·오류를 색으로 구분할 수 없다 | 상태 색 4종을 신규 정의하고 대비를 측정해 문서에 기록 |
| `motion.duration.instant=300ms` | 300ms는 "instant"가 아니다 | 값은 유지하되 이름을 `--motion-duration-base`로 쓰고, 마이크로 상태 전환용 `--motion-duration-fast: 120ms` 추가 |

`space.6=80px` 이상은 페이지 여백 전용으로만 쓴다. 컴포넌트 내부 간격은 `space.1`~`space.5`만 쓴다 —
그 위를 컴포넌트가 쓰기 시작하면 360px 뷰포트에서 콘텐츠가 사라진다.

**대안: Tailwind·CSS-in-JS.** 토큰 규칙을 강제하는 힘은 세지만, "원시 색상 코드가 토큰 파일 밖에 없다"는
spec 시나리오는 순수 CSS 로도 `grep` 한 번이면 확인된다. 빌드 파이프라인을 늘릴 이유가 없어 기각.
컴포넌트 스타일은 CSS Modules 로 파일별 격리만 한다.

### D6. Pretendard 는 npm 패키지로 self-host

`pretendard` 패키지의 woff2 를 번들에 넣는다. CDN `@import`를 쓰지 않는 이유는 평가자가 오프라인이거나
CDN 이 막힌 환경일 때 폰트가 조용히 폴백되고, 그게 대비·줄바꿈 검증을 흔들기 때문이다.
`font-display: swap` 으로 폰트 로딩이 첫 페인트를 막지 않게 한다.

### D7. 접근성 — 스트리밍 영역의 통지 단위

답변 본문 자체를 `aria-live="polite"` 로 두면 조각마다 통지가 쏟아진다(spec 위반). 대신:

- 답변 컨테이너: `aria-busy={isStreaming}`, `role="region"`, `aria-labelledby`.
- 별도의 `aria-live="polite"` 상태 줄 하나가 **전이만** 말한다 — "근거 N건을 찾았습니다" →
  "답변을 생성 중입니다" → "답변이 완료되었습니다. 인용 N건." 조각은 통지하지 않는다.
- 실패는 같은 줄에서 `role="alert"` 로 승격한다.

초점 표시는 `:focus-visible`에 `outline: 2px solid var(--color-focus-ring); outline-offset: 2px`.
`outline: none`은 코드베이스 어디에도 두지 않는다.

`[n]` 마커는 `<a href="#citation-n">` 이다. 버튼이 아니라 앵커인 이유는 "문서 내 다른 위치로 이동"이
정확히 앵커의 의미이고, 브라우저가 키보드 활성화와 초점 이동을 공짜로 준다. 대상 근거 항목에는
`tabIndex={-1}` 을 두어 앵커 이동이 실제 초점 이동이 되게 한다 (안 두면 초점은 `body`에 남고
스크린 리더 사용자는 아무 일도 안 일어난 것으로 느낀다).

### D8. 헬스 폴링은 10초, 실패는 즉시 "알 수 없음"

`setInterval` 10초 + `document.visibilityState`가 `hidden`이면 건너뛴다. 응답이 실패하거나 abort 되면
상태를 `unknown`으로 덮는다 — 마지막 성공값을 유지하면 서버가 죽은 화면이 계속 초록으로 보인다 (spec).

`/health`는 정상 200 / 불능 503 **둘 다 본문이 같은 모양**이므로, 상태 코드가 아니라 본문의
`status`·`dependencies`를 읽는다. `503`을 예외로 던지고 끝내면 어느 의존성이 죽었는지가 사라진다.

### D9. 파일 구조

```
demo-ui/
  DESIGN.md                  토큰·컴포넌트 상태 규칙·접근성 수용 기준·QA 체크리스트
  index.html
  package.json  vite.config.ts  tsconfig.json
  src/
    main.tsx  App.tsx
    api/
      client.ts              fetch 래퍼, 오류 봉투 → AppError 변환
      sse.ts                 parseSseChunk — 프레임 파서 (순수 함수)
      types.ts               서버 응답 타입 (DocumentView·SearchResultView·QaEvent…)
    hooks/
      useDocuments.ts        목록·업로드·삭제
      useQaStream.ts         D3 의 상태 기계
      useHealth.ts           D8 의 폴링
    components/
      AppHeader.tsx  HealthBadge.tsx
      DocumentPanel.tsx  Dropzone.tsx  DocumentRow.tsx
      QaConsole.tsx  QuestionForm.tsx  SourceList.tsx
      AnswerStream.tsx  AnswerText.tsx  CitationList.tsx  StatusBanner.tsx
      ui/                    Button·Field·Badge·EmptyState — 상태 7종을 여기서만 정의
    styles/
      tokens.css  reset.css  global.css
```

`api/types.ts`는 서버 스키마를 **손으로** 옮긴다. OpenAPI 생성기를 붙이면 백엔드 기동 없이는
프런트가 빌드되지 않고, 타입 15개를 위해 파이프라인을 하나 더 두는 거래가 맞지 않는다. 대신 타입이
서버와 어긋나는 것을 막는 책임을 tasks 의 "스키마 대조" 항목에 명시적으로 둔다.

## Risks / Trade-offs

- **LLM 미인증 환경에서는 데모의 절반만 보인다** → 근거 검색·문서 수집·헬스는 정상 동작하고 답변만
  `error`로 끝난다. 이 경로를 spec 시나리오로 못박아 "고장"이 아니라 "인증 없음"으로 읽히게 한다.
  README 에도 이 상태가 정상임을 적는다.
- **프런트 자동화 테스트가 없다** → 회귀를 사람이 잡아야 한다. QA 체크리스트를 `DESIGN.md`에 두고,
  SSE 파서·마커 파서만이라도 순수 함수로 분리해 나중에 테스트를 붙일 수 있게 남긴다.
- **손으로 옮긴 타입이 서버와 어긋날 수 있다** → 어긋나면 런타임에 `undefined`가 화면에 뜬다. 대조를
  태스크로 두고, 필수가 아닌 필드는 옵셔널로 선언해 조용한 크래시 대신 빈 표시가 되게 한다.
- **dev 서버 전용이라 `dist/`가 반쪽이다** → Non-Goal 로 명시하고 README 가 dev 경로만 안내한다.
  나중에 compose 서비스로 올릴 때는 nginx 리버스 프록시를 더하면 되고, 지금 구조를 바꾸지 않는다.
- **브리프 토큰을 조정했다** → 조정 없이 쓰면 대비·위계·좁은 화면 요구를 못 지킨다. 조정한 항목과
  이유를 `DESIGN.md`에 표로 남겨 "브리프를 무시했다"와 "브리프를 적용하며 충돌을 해결했다"가 구분되게 한다.
- **Node.js 가 새 의존성이다** → 평가자 환경에 Node 20+ 이 없으면 데모가 안 뜬다. 데모는 필수 산출물이
  아니므로 README 에서 선택 절차로 분리하고, 없어도 `docker compose up` + `pytest`는 그대로 된다.

## Open Questions

- 데모 UI 를 나중에 `docker compose --profile ui`로 올릴지. 지금 결정하지 않아도 D1 의 프록시 구조와
  파일 배치가 달라지지 않는다 — nginx 설정 파일 하나와 compose 서비스 하나가 더해질 뿐이다.
