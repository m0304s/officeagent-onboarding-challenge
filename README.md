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
| Docker Compose 한 줄 실행, 호스트 자격증명 주입 | 구현됨 |
| 문서 업로드 → 텍스트 추출 → 청킹 (`POST /documents`) | 구현됨 |
| 청크 임베딩·벡터 저장·문서 목록/삭제 | 미구현 |
| 벡터 검색 (retrieval) | 미구현 |
| LLM 답변 생성, 스트리밍 | 미구현 |
| 응답 캐싱, 캐시 무효화 | 미구현 |

계획은 [`openspec/changes/`](./openspec/changes/)에 change별로 있습니다.

## 실행

```bash
make up
```

Docker와 Docker Compose만 있으면 됩니다. API와 캐시 저장소가 함께 뜨고, 벡터 스토어는 프로세스에 내장되어 볼륨 하나로 영속화됩니다.

> **`docker compose up`을 직접 쓰지 마세요.** `make up`은 컨테이너를 띄우기 전에 [자격증명 동기화](#llm-자격증명-동기화)를 먼저 수행합니다. compose를 직접 호출하면 그 단계가 통째로 생략됩니다.

정지는 `make down`, 로그는 `make logs`입니다.

<details>
<summary>도커 없이 로컬에서 실행하기</summary>

Python 3.11 이상이 필요합니다. 캐시 저장소가 없으면 헬스가 503을 반환하지만 서비스 자체는 뜹니다.

```bash
make test          # 가상환경 생성 + 의존성 설치 (테스트까지 1회 실행)
.venv/bin/uvicorn --app-dir src --factory app.main:create_app
```

> macOS의 시스템 `python3`는 3.9라 그대로 쓰면 설치가 실패합니다. `make`가 3.11 이상인 인터프리터를 골라 가상환경을 만듭니다.

</details>

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

### 문서 업로드

```bash
curl -s -X POST http://127.0.0.1:8000/documents -F "file=@sample-docs/company-policy.txt"
```

`.txt` · `.md` · `.pdf`를 받아 텍스트를 추출하고 청크로 나눕니다. 응답에는 문서 식별자와 청크 목록(본문·페이지·원문 오프셋)이 들어 있습니다.

> **아직 저장되지 않습니다.** 임베딩과 벡터 저장이 미구현이라 청크는 응답으로만 돌아오고 어디에도 남지 않습니다. 그래서 응답이 `201`이 아니라 `200`이고, `index_signature`·`status` 같은 저장 관련 필드가 없습니다. 문서 목록·삭제 엔드포인트도 없습니다.

거절되는 경우:

| 상황 | 상태 | 코드 |
|---|---:|---|
| 지원하지 않는 확장자 / 확장자 없음 | 415 | `unsupported_document_format` (지원 목록 동봉) |
| 업로드 크기 상한 초과 (기본 20 MiB, `APP_MAX_UPLOAD_BYTES`) | 413 | `document_too_large` (적용된 상한 동봉) |
| 내용이 없거나 공백뿐 | 422 | `empty_document` |
| 쪽은 있으나 텍스트 레이어 없음 (스캔본) | 422 | `no_extractable_text` (쪽 수 동봉) |
| 확장자는 맞으나 내용이 그 포맷이 아님 | 422 | `document_parse_error` |

```json
{"error":{"code":"unsupported_document_format",
          "message":"지원하지 않는 문서 포맷입니다: .docx",
          "supported_formats":["md","pdf","txt"]}}
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

업로드는 무엇이 어떻게 잘렸는지도 한 줄로 남깁니다. **문서 본문이나 청크 내용은 싣지 않습니다** — 로그로 새어 나가면 그 자체가 유출입니다.

```json
{"level":"INFO","logger":"app.api.routes.documents","message":"문서 추출 완료","request_id":"df09ee03...","document_id":"b166d4ad-...","document_filename":"handbook.pdf","format":"pdf","revision":"662b78b2c395","byte_size":15013,"page_count":3,"chunk_count":5}
```

## LLM 자격증명 동기화

`claude-code-sdk`는 HTTP 클라이언트가 아니라 **로컬 CLI를 실행**합니다. 그래서 컨테이너 안에 CLI 런타임과 인증 상태가 함께 있어야 합니다. 인증은 이미지에 굽지 않고, 호스트에 **이미 있는** 자격증명을 실행 시점에 꺼내 마운트합니다.

`make up`이 `scripts/sync-credentials.sh`를 먼저 돌립니다. 이 스크립트가 하는 일은 이게 전부입니다.

| 호스트 | 자격증명 위치 | 동기화 방법 |
|--------|---------------|-------------|
| macOS | Keychain 항목 `Claude Code-credentials` | 파일이 아니라 볼륨으로 붙일 수 없으므로 꺼내서 `.secrets/claude/.credentials.json`에 씁니다 |
| Linux | `~/.claude/.credentials.json` | 이미 파일이므로 그대로 복사합니다 |

두 포맷은 동일합니다. 결과 파일은 `0600`, 디렉터리는 `0700`이며 `.secrets/`는 형상관리에서 제외됩니다.

**새 토큰을 발급하지 않습니다.** `claude setup-token`이나 `claude login`은 쓰지 않습니다 — 호스트에 이미 있는 인증 상태를 재사용하는 것이 전부입니다. 자격증명을 찾지 못해도 스크립트는 실패로 끝내지 않고, 서비스는 그대로 기동됩니다.

### 주의사항

- **컨테이너가 가진 것은 사본입니다.** macOS에서는 Keychain을 컨테이너와 공유할 수 없습니다. 컨테이너가 토큰을 갱신해도 그 결과는 호스트 Keychain으로 돌아가지 않습니다.
- **호스트 CLI가 로그아웃될 수 있습니다.** 갱신 시 refresh token이 회전하는 방식이라면 호스트가 들고 있는 값이 무효가 됩니다. 그렇게 되면 호스트에서 `claude` 로그인을 다시 해야 합니다. **회전 여부는 아직 검증되지 않았습니다.** 완화책은 두 가지입니다 — access token 수명이 약 8시간이라 한 번의 실행 세션 안에서는 갱신이 잘 일어나지 않고, 기동할 때마다 재추출하므로 사본이 묵는 창이 짧습니다.
- 자격증명은 **쓰기 가능하게(rw)** 마운트됩니다. `:ro`로 붙이면 갱신에 실패해 약 8시간 뒤 인증이 끊깁니다.
- 호스트 UID가 1000이 아닌 리눅스에서 컨테이너가 마운트한 파일을 읽지 못하면, `docker-compose.yml`의 `user:` 줄 주석을 푸세요.

## 테스트

```bash
make test
```

깨끗한 체크아웃에서 이 한 줄이면 됩니다 — 가상환경이 없으면 만들고 의존성을 설치한 뒤 실행합니다. Docker도, 실행 중인 서비스도, 자격증명도 필요 없습니다.

의존성 상태는 프로브 대역을 주입해 결정론적으로 구성합니다. 실제 컨테이너를 죽여가며 상태를 만들면 느리고 불안정하기 때문입니다.

린트·포맷:

```bash
make lint
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
>
> 이미지에는 CLI가 설치되어 있고 자격증명도 주입되지만, **컨테이너 안에서 CLI가 실제로 답변을 생성하는지는 아직 호출해 보지 않았습니다.** 이런 미검증 가정은 [`docs/SPIKES.md`](./docs/SPIKES.md)에 모아 두었고, 각각 다음 change의 첫 태스크가 됩니다.

설계 근거는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)에 있습니다.

## 회고

`git commit`마다 Retrobot이 작업 로그를 분석해 KPT 회고를 [`retros/`](./retros/)에 생성합니다. 활성화:

```bash
git config core.hooksPath .githooks
```
