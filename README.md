# Document Q&A API

문서를 업로드하면 텍스트를 추출·색인하고, 사용자 질문에 대해 **업로드된 문서만을 근거로** 답변을 생성하는 RAG 기반 REST API 서버.

과제 원문은 [`docs/PRD.md`](./docs/PRD.md)에 있습니다.

## 현재 구현 범위

change 단위로 점진적으로 구현합니다. **아래 표에서 "구현됨"인 것만 실제로 존재합니다.**

| 기능 | 상태 |
|------|------|
| 서비스 기동, 헬스 리포팅 (`GET /health`) | 구현됨 |
| 레이어 구조, 어댑터 프로토콜, 설정 로딩 | 구현됨 |
| 테스트 하네스 | 구현됨 |
| 공통 오류 응답 형식, 구조화 로깅 | 구현됨 |
| Docker / Docker Compose | 미구현 |
| 문서 수집 (ingestion) | 미구현 |
| 벡터 검색 (retrieval) | 미구현 |
| LLM 답변 생성, 스트리밍 | 미구현 |
| 응답 캐싱, 캐시 무효화 | 미구현 |

계획은 [`openspec/changes/`](./openspec/changes/)에 change별로 있습니다.

## 실행

Python 3.11 이상이 필요합니다.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/uvicorn --app-dir src --factory app.main:create_app
```

> Docker Compose는 아직 없습니다. 이후 change에서 한 줄 실행 명령으로 대체됩니다.

기동 후 상태 확인:

```bash
curl -s http://127.0.0.1:8000/health
```

캐시 저장소(Redis)가 떠 있지 않으면 `503`과 함께 어느 의존성이 불능인지 반환됩니다. **서비스 자체는 정상 기동합니다.**

```json
{
  "status": "unavailable",
  "dependencies": {
    "cache": { "status": "unavailable", "detail": "연결 실패 (ConnectionError)" },
    "vector_store": { "status": "ok", "detail": null }
  }
}
```

### 오류 응답

모든 오류가 같은 봉투를 씁니다. 프레임워크 기본 응답(경로 없음·메서드 불허·검증 실패)도 덮어씁니다.

```bash
curl -s http://127.0.0.1:8000/nope
# {"error":{"code":"not_found","message":"Not Found"}}
```

`/health`는 예외입니다 — 503일 때도 오류 봉투가 아니라 위의 상태 보고 본문을 그대로 씁니다. 상태 보고와 오류 통지는 다른 일이기 때문입니다.

응답에는 내부 정보(스택 트레이스·접속 문자열·자격증명)를 싣지 않습니다. 원인 추적 정보는 로그에만 남습니다.

### 로그

JSON 한 줄로 출력되며, 요청마다 `x-request-id`가 응답 헤더로 돌아옵니다. 요청 헤더로 넣어 보내면 그 값이 유지됩니다.

```json
{"level":"INFO","logger":"app.access","message":"요청 처리 완료","request_id":"c751a0a0...","method":"GET","path":"/health","status_code":503,"duration_ms":354.95}
```

## 테스트

```bash
.venv/bin/pytest
```

외부 서비스나 자격증명 없이 통과합니다. 의존성 상태는 프로브 대역을 주입해 결정론적으로 구성합니다 — 실제 컨테이너를 죽여가며 상태를 만들면 느리고 불안정하기 때문입니다.

린트·포맷:

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## 설정

모든 항목에 기본값이 있어 **환경변수를 하나도 주지 않아도 기동됩니다.** 값이 무효하면 조용히 기본값으로 넘어가지 않고 기동에 실패합니다.

| 환경변수 | 의미 | 기본값 |
|----------|------|--------|
| `APP_APP_NAME` | 서비스 이름 | `document-qa-api` |
| `APP_LOG_LEVEL` | 로그 레벨 | `INFO` |
| `APP_CACHE_URL` | 캐시 저장소 접속 URL | `redis://localhost:6379/0` |
| `APP_VECTOR_STORE_PATH` | 벡터 스토어 저장 경로 | `./data/chroma` |
| `APP_PROBE_TIMEOUT_SECONDS` | 의존성 점검 개별 상한(초) | `2.0` |
| `APP_HEALTH_TOTAL_TIMEOUT_SECONDS` | 헬스 점검 전체 상한(초) | `5.0` |

`.env` 파일도 읽습니다. 환경을 직접 조회하는 곳은 `src/app/config.py` 하나뿐이며, 다른 모듈의 직접 조회는 린트 규칙으로 막혀 있습니다.

## 기술 선택

| 계층 | 선택 | 이유 |
|------|------|------|
| 언어 / 프레임워크 | Python 3.11+ / FastAPI | 과제 고정 조건. SSE 스트리밍과 async 파이프라인이 네이티브 |
| LLM SDK | `claude-code-sdk` | API 키 없이 구독으로 동작하고, 파이썬 네이티브라 subprocess 래핑 없이 async 스트리밍·타임아웃 제어가 가능 |
| 임베딩 | sentence-transformers (로컬) | 로컬 오픈소스 모델이라 테스트가 LLM 구독 없이 실행됨 |
| 벡터 DB | Chroma (임베디드 퍼시스턴트) | 별도 컨테이너 없이 볼륨 하나로 영속화되어 한 줄 실행이 단순. 문서 단위 삭제를 지원해 캐시 무효화 연동이 가능 |
| 캐시 DB | Redis | 정확 매치는 키 조회, 유사 질문은 질문 임베딩 유사도로 판정. TTL·태그 기반 무효화가 자연스러움 |
| 린터 | ruff | 포매팅과 린팅을 한 도구로 통일. 레이어 경계도 린트 규칙으로 강제 |

> `claude-code-sdk`와 sentence-transformers는 **아직 호출하는 코드가 없습니다.** 해당 change에서 도입됩니다. Chroma와 Redis는 현재 헬스 점검에만 쓰입니다.

설계 근거는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)에 있습니다.

## 회고

`git commit`마다 Retrobot이 작업 로그를 분석해 KPT 회고를 [`retros/`](./retros/)에 생성합니다. 활성화:

```bash
git config core.hooksPath .githooks
```
