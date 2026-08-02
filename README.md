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
| 문서 수집 — 추출 → 청킹 → 임베딩 → 벡터 저장 (`POST /documents`) | 구현됨 |
| 문서 목록·상세·삭제 (`GET`/`DELETE /documents`) | 구현됨 |
| 재업로드 교체·재색인, 기동 시 저장소 정리 | 구현됨 |
| 벡터 검색 (retrieval) | 미구현 |
| LLM 답변 생성, 스트리밍 | 미구현 |
| 응답 캐싱, 캐시 무효화 | 미구현 |

계획은 [`openspec/changes/`](./openspec/changes/)에 change별로 있습니다.

## 실행

```bash
make up
```

Docker와 Docker Compose만 있으면 됩니다. 컨테이너 셋(API · 벡터 스토어 · 캐시 저장소)이 함께 뜨고, 벡터 데이터와 문서 레지스트리는 각각 `./data/chroma`·`./data/registry`에 남습니다(형상관리 제외).

| 서비스 | 이미지 | 호스트 포트 | 비고 |
|---|---|---:|---|
| `api` | 이 리포 | 8000 | |
| `vector-store` | `chromadb/chroma:1.5.9` | 8001 | 클라이언트와 같은 버전으로 고정 |
| `cache` | `redis:7-alpine` | — | |
| `vector-store-ui` | `fengzhichao/chromadb-admin:0.0.2` | 3001 | **기본 기동에 포함되지 않습니다** — `docker compose --profile gui up` 으로만 뜹니다 |

Chroma 서버에는 자체 UI가 없어(루트 경로가 404) 비공식 admin UI를 프로필 뒤에 두었습니다. 그 이미지가 arm64 전용이라 기본 기동에 넣으면 amd64 환경에서 `docker compose up`이 깨집니다.

> **`docker compose up`을 직접 쓰지 마세요.** `make up`은 컨테이너를 띄우기 전에 [자격증명 동기화](#llm-자격증명-동기화)를 먼저 수행합니다. compose를 직접 호출하면 그 단계가 통째로 생략됩니다.

정지는 `make down`, 로그는 `make logs`입니다.

<details>
<summary>도커 없이 로컬에서 실행하기</summary>

Python 3.11 이상이 필요합니다. 캐시 저장소가 없으면 헬스가 503을 반환하지만 서비스 자체는 뜹니다.

```bash
make test          # 가상환경 생성 + 의존성 설치 (테스트까지 1회 실행)
make vector-store  # 벡터 스토어만 도커로 띄운다 (localhost:8001)
.venv/bin/uvicorn --app-dir src --factory app.main:create_app
```

벡터 스토어는 별도 서비스라 도커 없이 대체할 수 없습니다. 없이 띄우면 서비스는 정상 기동하고 헬스가 `unavailable`로 보고하며, **새 내용의 업로드**는 `503 storage_unavailable`이 됩니다. 이미 수집된 것과 바이트가 같은 재업로드는 `200 unchanged`로 끝납니다 — 저장할 것이 없어 벡터 스토어에 닿지 않기 때문입니다. 목록·상세 조회도 레지스트리만 보므로 그대로 동작합니다.

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

### 문서 API

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| `POST` | `/documents` | 업로드 → 추출 → 청킹 → 임베딩 → 벡터 저장. 최초 수집 `201`, 그 외 `200` |
| `GET` | `/documents` | 수집된 문서 목록 (최근 수집 순) |
| `GET` | `/documents/{document_id}` | 문서 한 건. 없으면 `404` |
| `DELETE` | `/documents/{document_id}` | 문서와 그 청크를 전부 제거. `204`, 없으면 `404` |

```bash
curl -s -X POST http://127.0.0.1:8000/documents -F "file=@sample-docs/company-policy.txt"
```

`.txt` · `.md` · `.pdf`를 받습니다. 응답에 청크 본문은 싣지 않습니다 — 청크는 벡터 스토어에 저장되며, 여기서는 그 결과만 보고합니다.

```json
{"document_id":"b166d4ad-...","filename":"company-policy.txt","format":"txt",
 "revision":"2ccbdc60...","index_signature":"e78999af0e98a7d4","index_status":"indexed",
 "chunk_count":3,"byte_size":1708,"ingested_at":"2026-08-02T04:11:52.913Z",
 "status":"created","previous_revision":null}
```

#### `status` — 이번 요청이 무엇을 했는가

| 값 | 상태 | 언제 |
|---|---:|---|
| `created` | 201 | 처음 수집된 문서 |
| `replaced` | 200 | 같은 파일명, **내용이 다름**. 이전 리비전 청크를 제거하고 교체하며 `previous_revision`이 함께 옵니다 |
| `reindexed` | 200 | 같은 파일명, 내용도 같지만 **색인 구성이 달라짐**. `revision`은 그대로, `index_signature`가 바뀝니다 |
| `unchanged` | 200 | 내용과 색인 구성이 **둘 다** 같음. 임베딩을 다시 하지 않고 기존 값을 그대로 돌려줍니다 |

문서 식별자는 파일명에서, 리비전은 내용에서 결정됩니다. 파일명 비교는 대소문자를 구분하지 않으므로 `Policy.TXT`와 `policy.txt`는 같은 문서입니다.

#### `index_status` — 그 문서가 지금 검색 가능한가

`index_signature`는 임베딩 모델의 정체성과 청킹 구성(전략·버전·크기·겹침)에서 유도되는 값입니다. 이 구성을 바꾸면 기존 벡터는 다른 의미 공간의 값이 되어 검색에 섞이면 안 됩니다.

원본 바이트를 보관하지 않으므로 자동 재색인은 불가능합니다. 그래서 **기동할 때** 서명이 달라진 문서의 청크를 제거하고 `index_status`를 `stale`로 표시합니다. 그 문서는 목록·상세에 계속 보이지만 `chunk_count`가 0이고 검색되지 않습니다.

```
{"level":"WARNING","message":"색인 구성이 바뀌어 청크를 제거했습니다 — 다시 업로드해야 검색됩니다", ...}
```

**복구는 같은 파일을 다시 업로드하는 것뿐입니다.** 그러면 `status: "reindexed"`로 현재 구성에 맞게 다시 색인되고 `index_status`가 `indexed`로 돌아옵니다.

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

업로드는 무엇이 어떻게 저장됐는지도 한 줄로 남깁니다. **문서 본문이나 청크 내용은 싣지 않습니다** — 로그로 새어 나가면 그 자체가 유출입니다.

```json
{"level":"INFO","logger":"app.api.routes.documents","message":"문서 업로드 완료","request_id":"df09ee03...","document_id":"b166d4ad-...","document_filename":"handbook.pdf","format":"pdf","revision":"662b78b2c395","byte_size":15013,"page_count":3,"chunk_count":5,"ingestion_status":"created"}
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
- 호스트 UID가 1000이 아닌 리눅스에서 컨테이너가 마운트한 파일(자격증명, `./data/*`)을 읽거나 쓰지 못하면, `docker-compose.yml`의 `api` 서비스에 `user: "${DOCKER_UID:-1000}:${DOCKER_GID:-1000}"` 를 추가하세요. macOS Docker Desktop은 소유권을 매핑해 주므로 불필요합니다.

## 테스트

```bash
make test
```

깨끗한 체크아웃에서 이 한 줄이면 됩니다 — 가상환경이 없으면 만들고 의존성을 설치한 뒤 실행합니다. Docker도, 실행 중인 서비스도, 자격증명도 필요 없습니다.

의존성 상태는 대역을 주입해 결정론적으로 구성합니다. 실제 컨테이너를 죽여가며 상태를 만들면 느리고 불안정하기 때문입니다. **임베딩 모델도 대역입니다** — 실제 가중치를 받으면 이 한 줄이 수백 MB 다운로드에 묶입니다. 실제 모델과의 계약(차원·역할 접두사·서명)은 가중치가 이미 캐시된 환경에서만 도는 테스트가 따로 확인하고, 없으면 건너뜁니다.

문서 레지스트리(SQLite)는 임시 파일에 실물로 띄워 검증합니다. 반면 **벡터 스토어는 별도 서버**라, 실물 어댑터 테스트는 서버가 없으면 건너뜁니다. 그것까지 돌리려면:

```bash
make test-all      # 벡터 스토어를 띄운 뒤 전부 실행
```

대역으로 대체하지 않은 이유는 그 테스트가 확인하려는 것이 "우리 메타데이터·필터·id 규약이 *실제 Chroma*에서 성립하는가"이기 때문입니다 — 대역으로 바꾸면 확인 대상 자체가 사라집니다.

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
| `APP_VECTOR_STORE_URL` | 벡터 스토어(Chroma 서버) 주소 | `http://localhost:8001` |
| `APP_PROBE_TIMEOUT_SECONDS` | 의존성 점검 개별 상한(초) | `2.0` |
| `APP_HEALTH_TOTAL_TIMEOUT_SECONDS` | 헬스 점검 전체 상한(초) | `5.0` |
| `APP_REGISTRY_PATH` | 문서 레지스트리(SQLite) 경로 | `./data/registry.sqlite3` |
| `APP_EMBEDDING_MODEL` | 임베딩 모델 이름 | `intfloat/multilingual-e5-small` |
| `APP_CHUNK_STRATEGY` | 분할 전략 | `recursive` |
| `APP_CHUNK_SIZE` | 청크 크기 상한(문자) | `600` |
| `APP_CHUNK_OVERLAP` | 인접 청크 겹침(문자). `0` 불가 | `100` |
| `APP_EMBEDDING_BATCH_SIZE` | 임베딩·저장 배치 크기 | `64` |
| `APP_MAX_UPLOAD_BYTES` | 업로드 크기 상한 | `20971520` (20 MiB) |
| `APP_INGESTION_CONCURRENCY` | 동시 수집 상한 | `2` |

구현되지 않은 `APP_CHUNK_STRATEGY` 값(현재 구현된 것은 `recursive` 하나입니다)이나 청크 크기 이상의 겹침을 넣으면 **기동에 실패합니다.** 잘못된 색인 구성으로 조용히 뜨는 것보다 낫기 때문입니다.

`APP_EMBEDDING_MODEL`·`APP_CHUNK_STRATEGY`·`APP_CHUNK_SIZE`·`APP_CHUNK_OVERLAP`은 `index_signature`의 재료입니다. 바꾸면 기존 문서가 다음 기동에서 [`stale`](#index_status--그-문서가-지금-검색-가능한가)이 되어 재업로드가 필요합니다. 나머지 값(배치 크기·업로드 상한·동시성)은 저장된 벡터를 바꾸지 않으므로 서명에 영향을 주지 않습니다 — 성능 튜닝이 전면 재색인을 유발하지 않습니다.

`.env` 파일도 읽습니다. 환경을 직접 조회하는 곳은 `src/app/config.py` 하나뿐이며, 다른 모듈의 직접 조회는 린트 규칙으로 막혀 있습니다.

## 기술 선택

| 계층 | 선택 | 이유 |
|------|------|------|
| 언어 / 프레임워크 | Python 3.11+ / FastAPI | 과제 고정 조건. SSE 스트리밍과 async 파이프라인이 네이티브 |
| LLM SDK | `claude-code-sdk` | API 키 없이 구독으로 동작하고, 파이썬 네이티브라 subprocess 래핑 없이 async 스트리밍·타임아웃 제어가 가능 |
| 임베딩 | sentence-transformers (`intfloat/multilingual-e5-small`) | 로컬 오픈소스 모델이라 테스트가 LLM 구독 없이 실행됨. 다국어·512 토큰 창 |
| 문서 레지스트리 | SQLite (표준 라이브러리) | "지금 유효한 리비전이 무엇인가"의 단일 답. 컨테이너를 늘리지 않고 벡터 스토어와 같은 볼륨에 놓임 |
| 벡터 DB | Chroma (**서버 모드**, 별도 컨테이너) | 메타데이터 필터와 문서 단위 삭제를 지원해 리비전 교체·캐시 무효화 연동이 가능. 저장소를 앱 프로세스 밖으로 빼 API 재배포와 수명이 분리됨 |
| PDF 파싱 | PyMuPDF | 쪽 단위 텍스트 추출이 정확하고 빠름. **AGPL-3.0**이므로 배포 형태를 바꿀 때 재검토가 필요 |
| 캐시 DB | Redis | 정확 매치는 키 조회, 유사 질문은 질문 임베딩 유사도로 판정. TTL·태그 기반 무효화가 자연스러움 |
| 린터 | ruff | 포매팅과 린팅을 한 도구로 통일. 레이어 경계도 린트 규칙으로 강제 |

> `claude-code-sdk`는 **아직 호출하는 코드가 없습니다** — 답변 생성 change에서 도입됩니다. Redis도 현재는 헬스 점검에만 쓰입니다. sentence-transformers·Chroma·SQLite·PyMuPDF는 수집 경로에서 실제로 쓰입니다.
>
> 이미지에는 CLI가 설치되어 있고 자격증명도 주입되지만, **컨테이너 안에서 CLI가 실제로 답변을 생성하는지는 아직 호출해 보지 않았습니다.** 이런 미검증 가정은 [`docs/SPIKES.md`](./docs/SPIKES.md)에 모아 두었고, 각각 다음 change의 첫 태스크가 됩니다.

설계 근거는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)에 있습니다.

## 회고

`git commit`마다 Retrobot이 작업 로그를 분석해 KPT 회고를 [`retros/`](./retros/)에 생성합니다. 활성화:

```bash
git config core.hooksPath .githooks
```
