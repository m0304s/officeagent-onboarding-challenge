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
| 벡터 검색 — 질의 임베딩 → 대상 필터 → 상위 K (`POST /search`) | 구현됨 |
| LLM 답변 생성, 스트리밍 | 미구현 |
| 응답 캐싱, 캐시 무효화 | 미구현 |

계획은 [`openspec/changes/`](./openspec/changes/)에 change별로 있습니다.

## 실행

```bash
docker compose up
```

Docker와 Docker Compose만 있으면 됩니다. **[LLM 자격증명 주입](#llm-자격증명-동기화)까지 이 한 줄 안에 들어 있습니다** — 윈도우·macOS·리눅스 모두 같습니다. 컨테이너 셋(API · 벡터 스토어 · 캐시 저장소)이 함께 뜨고, 벡터 데이터와 문서 레지스트리는 각각 `./data/chroma`·`./data/registry`에 남습니다(형상관리 제외).

| 서비스 | 이미지 | 호스트 포트 | 비고 |
|---|---|---:|---|
| `auth` | `alpine:3.21` | — | 호스트의 기존 Codex 자격증명을 꺼내고 **한 번 돌고 끝납니다** |
| `api` | 이 리포 | 8000 | |
| `vector-store` | `chromadb/chroma:1.5.9` | 8001 | 클라이언트와 같은 버전으로 고정 |
| `cache` | `redis:7-alpine` | — | |
| `vector-store-ui` | `fengzhichao/chromadb-admin:0.0.2` | 3001 | **기본 기동에 포함되지 않습니다** — `docker compose --profile gui up` 으로만 뜹니다 |
| `test` | 이 리포 (`test` 스테이지) | — | **기본 기동에 포함되지 않습니다** — [테스트](#테스트) 참고 |

Chroma 서버에는 자체 UI가 없어(루트 경로가 404) 비공식 admin UI를 프로필 뒤에 두었습니다. 그 이미지가 arm64 전용이라 기본 기동에 넣으면 amd64 환경에서 `docker compose up`이 깨집니다.

코드를 고친 뒤 다시 올릴 때는 `docker compose up --build`를 쓰세요. 변경이 없으면 캐시가 전부 히트해 사실상 공짜입니다.

| 하려는 것 | 명령 |
|---|---|
| 기동 | `docker compose up` |
| 정지 | `docker compose down` |
| 로그 | `docker compose logs -f api` |
| 벡터 스토어만 | `docker compose up -d --wait vector-store` |
| 벡터 스토어 GUI | `docker compose --profile gui up -d --wait vector-store-ui` |

<details>
<summary>도커 없이 로컬에서 실행하기</summary>

Python 3.11 이상이 필요합니다. 캐시 저장소가 없으면 헬스가 503을 반환하지만 서비스 자체는 뜹니다.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d --wait vector-store   # 벡터 스토어만 도커로 (localhost:8001)
.venv/bin/uvicorn --app-dir src --factory app.main:create_app
```

> macOS의 시스템 `python3`는 3.9라 그대로 쓰면 설치가 실패합니다. `python3.11` 이상을 직접 지정하세요.

벡터 스토어는 별도 서비스라 도커 없이 대체할 수 없습니다. 없이 띄우면 서비스는 정상 기동하고 헬스가 `unavailable`로 보고하며, **새 내용의 업로드**는 `503 storage_unavailable`이 됩니다. 이미 수집된 것과 바이트가 같은 재업로드는 `200 unchanged`로 끝납니다 — 저장할 것이 없어 벡터 스토어에 닿지 않기 때문입니다. 목록·상세 조회도 레지스트리만 보므로 그대로 동작합니다.

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

### 검색 API

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| `POST` | `/search` | 질의와 가까운 근거 청크를 유사도 내림차순으로 최대 K개 |

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"query":"교육비는 얼마까지 지원되나요?","top_k":3}'
```

```json
{"query":"교육비는 얼마까지 지원되나요?","top_k":3,"count":1,
 "results":[{"document_id":"b166d4ad-...","filename":"company-policy.txt","format":"txt",
             "revision":"2ccbdc60...","chunk_index":1,
             "text":"교육비는 연 200만원까지 지원합니다. ...",
             "score":0.8734,"char_start":142,"char_end":538,"page":null}]}
```

**검색은 답변을 만들지 않습니다.** 응답에 답변 필드도 거절 문구도 없습니다 — "이 근거로 답할 수 있는가"는 본문을 읽어야 하는 판정이라 답변 생성(다음 change)의 몫이고, 검색이 하는 판정은 "이 청크를 다음 단계에 보여줄 가치가 있는가"(유사도 하한)까지입니다. 그래서 이 엔드포인트는 `/qa`가 생긴 뒤에도 남습니다. **답이 이상할 때 원인이 검색인지 생성인지 가르는 관측 지점**이 이것입니다 — 근거가 애초에 안 잡혔는지, 잡혔는데 답이 틀렸는지가 여기서 갈립니다.

결과 하나가 스스로 출처를 말합니다. 인용 한 줄을 만들려고 문서를 다시 조회할 필요가 없습니다.

| 필드 | 의미 |
|---|---|
| `score` | 유사도. `[0, 1]`이고 **클수록 가깝습니다**(저장소의 거리 지표는 어댑터가 뒤집어 둡니다) |
| `char_start` · `char_end` | 원문 문자 오프셋 구간. `text`는 언제나 추출된 원문의 그 구간과 같습니다 |
| `page` | PDF에서 온 청크만 채워집니다. 이때 오프셋은 **그 쪽 안의** 위치입니다 |
| `revision` · `chunk_index` | 근거가 어느 세대 문서의 몇 번째 청크인지 |

검색 대상은 **지금 유효한 청크뿐**입니다. 이전 리비전·이전 색인 구성·삭제된 문서·[`stale`](#index_status--그-문서가-지금-검색-가능한가) 문서의 청크는 저장소에 남아 있더라도 결과에 나타나지 않습니다. 유사도 하한에 걸려 결과가 비는 것은 오류가 아니라 `200`과 빈 목록입니다.

거절되는 경우:

| 상황 | 상태 | 코드 |
|---|---:|---|
| 질의가 비었거나 공백뿐 | 422 | `empty_query` |
| 질의 길이 상한 초과 (문자 수 또는 토큰 수) | 422 | `query_too_long` (두 상한 동봉) |
| `top_k`가 `1` 미만 | 422 | `validation_error` (요청 스키마가 판정) |
| `top_k`가 설정 상한 초과 | 422 | `invalid_top_k` (적용된 상한 동봉) |
| 벡터 스토어 접근 실패 | 503 | `storage_unavailable` |

거절된 질의는 **임베딩 계산도 저장소 접근도 유발하지 않습니다.** 저장소 장애를 빈 결과로 위장하지도 않습니다 — 뭉개면 벡터 스토어가 죽은 동안 서비스가 "근거를 찾지 못했습니다"라고 답하고 아무도 장애를 눈치채지 못합니다.

**질의 길이 제한은 두 겹이고 목적이 다릅니다.**

| 상한 | 무엇을 막는가 | 값 |
|---|---|---|
| 문자 수 | 임의 길이 입력이 토크나이저에 들어가는 것 | 설정 `APP_RETRIEVAL_MAX_QUERY_CHARS` (기본 1000) |
| 토큰 수 | **조용한 절단** — 잘린 뒷부분이 검색에 반영되지 않는데 사용자는 전부 반영됐다고 믿는 실패 | **임베딩 모델이 선언한 입력 창**(현재 512). 설정 항목이 아닙니다 |

토큰 상한을 설정으로 두지 않는 이유는 손으로 적을 수 있으면 실제 모델과 어긋난 값을 넣을 수 있고, 그 순간 가드가 보증하려던 것이 사라지기 때문입니다. 문자 수 하나로 둘 다 막을 수는 없습니다 — 같은 글자 수라도 문자 종류에 따라 토큰 수가 아홉 배 가까이 달라져, 절단을 막을 만큼 낮춘 문자 상한(약 101자)은 평범한 질문까지 거부합니다. 토큰은 실제로 인코딩되는 문자열(역할 접두사·특수 토큰 포함) 기준으로 셉니다.

검색 로그도 한 줄입니다. **질의 문자열과 청크 본문은 싣지 않습니다.**

```json
{"level":"INFO","logger":"app.api.routes.search","message":"검색 요청을 처리했습니다","request_id":"9f2c...","top_k":5,"result_count":3,"top_score":0.8734,"target_documents":2}
```

`target_documents`가 있어야 빈 결과의 이유가 갈립니다 — `0`이면 올린 문서가 없거나 전부 `stale`인 것이고, 대상이 있는데 결과가 `0`이면 유사도 하한에 걸린 것입니다.

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

Codex SDK는 HTTP 클라이언트가 아니라 **로컬 CLI를 실행**합니다. 그래서 컨테이너 안에 CLI 런타임과 인증 상태가 함께 있어야 합니다. 인증은 이미지에 굽지 않고, 호스트에 **이미 있는** 자격증명을 실행 시점에 꺼내 마운트합니다.

**따로 실행할 것이 없습니다.** `docker compose up`이 `auth` 서비스로 `scripts/sync-credentials.sh`를 먼저 돌리고, 그게 끝난 뒤에야 `api`가 뜹니다. 스크립트는 호스트가 아니라 **컨테이너 안에서** 도므로 `make`도 `bash`도 필요 없고, 세 OS가 같은 경로를 탑니다.

```
호스트                                          컨테이너
  ~/.codex/auth.json ──ro──▶ auth 서비스
                                  │ 복사 (0600)
                                  ▼
  ./.secrets/codex/auth.json ──rw──▶ api:/home/app/.codex/auth.json
        ▲                                        │
        └──── 기동할 때마다 재추출        CLI 가 만료 시 갱신 (사본에만 반영)
```

| 호스트 | 자격증명 위치 |
|--------|---------------|
| Windows | `%USERPROFILE%\.codex\auth.json` |
| macOS · Linux | `~/.codex/auth.json` |

세 OS 모두 **평문 파일**이라 볼륨으로 붙일 수 있습니다. 이것이 Codex를 고른 이유입니다 — 자격증명을 OS 키체인에 넣는 CLI였다면 컨테이너가 읽을 수 없어 호스트 단계가 남았을 겁니다. 근거는 [`ARCHITECTURE.md`](./ARCHITECTURE.md#llm-sdk-통합-방식)에 있습니다.

결과 파일은 `0600`, 디렉터리는 `0700`이며 `.secrets/`는 형상관리에서 제외됩니다.

**새 토큰을 발급하지 않습니다.** `codex login`은 쓰지 않습니다 — 호스트에 이미 있는 인증 상태를 재사용하는 것이 전부입니다. 자격증명을 찾지 못해도 `auth`는 실패로 끝내지 않고, 서비스는 그대로 기동됩니다. LLM 기능만 사용할 수 없습니다.

호스트에서 아직 로그인하지 않았다면 `codex login`을 한 번 하고 다시 올리면 자동으로 붙습니다.

### 주의사항

- **컨테이너가 가진 것은 사본입니다.** 컨테이너가 토큰을 갱신해도 그 결과는 호스트로 돌아가지 않습니다. 역방향 동기화는 호스트의 인증 상태를 컨테이너가 덮어쓰는 행위라 하지 않습니다.
- 자격증명은 **쓰기 가능하게(rw)** 마운트됩니다. `:ro`로 붙이면 만료 시 갱신에 실패해 그 시점에 인증이 끊깁니다.
- 호스트의 `~/.codex`는 **읽기 전용(`:ro`)**으로만 붙습니다. 디렉터리째 붙이는 이유는, 파일 하나를 지정했을 때 그 파일이 없으면 도커가 그 자리에 **디렉터리를 만들어** 호스트의 `codex login`을 망가뜨리기 때문입니다.
- 비표준 위치에 `~/.codex`를 두었다면 `CODEX_HOME` 환경변수를 그대로 존중합니다.
- 호스트 UID가 1000이 아닌 리눅스에서 컨테이너가 마운트한 파일(자격증명, `./data/*`)을 읽거나 쓰지 못하면, `docker-compose.yml`의 `api` 서비스에 `user: "${DOCKER_UID:-1000}:${DOCKER_GID:-1000}"` 를 추가하세요. `auth`가 사본을 uid 1000으로 넘기므로 기본 구성에서는 필요하지 않습니다.

## 테스트

```bash
docker compose run --build --rm test
```

깨끗한 체크아웃에서 이 한 줄이면 됩니다. 호스트에 **파이썬도 가상환경도 자격증명도 필요 없습니다** — Docker만 있으면 됩니다.

의존성 상태는 대역을 주입해 결정론적으로 구성합니다. 실제 컨테이너를 죽여가며 상태를 만들면 느리고 불안정하기 때문입니다. **기본값은 임베딩 모델도 대역입니다** — 실제 가중치를 받으면 이 한 줄이 수백 MB 다운로드에 묶입니다. 실제 모델이 필요한 것(차원·역할 접두사·서명 같은 계약, 그리고 아래의 검색 품질)은 가중치가 이미 캐시된 환경에서만 돌고, 없으면 건너뜁니다.

문서 레지스트리(SQLite)는 임시 파일에 실물로 띄워 검증합니다. **벡터 스토어는 별도 서버**라 실물 어댑터 테스트에는 서버가 필요한데, 위 명령이 `depends_on`으로 함께 띄우므로 그 층도 실행됩니다. 대역으로 대체하지 않은 이유는 그 테스트가 확인하려는 것이 "우리 메타데이터·필터·id 규약이 *실제 Chroma*에서 성립하는가"이기 때문입니다 — 대역으로 바꾸면 확인 대상 자체가 사라집니다.

**검색 품질 테스트는 로컬 임베딩 실물을 씁니다.** "기대한 문서의 청크가 1위로 오는가"는 대역으로는 확인할 수 없습니다 — 해시 기반 페이크 벡터에는 의미가 없어 1위가 무엇이든 정상으로 보입니다. 그래서 이 층만 실제 모델을 쓰고, 가중치가 없으면 건너뜁니다. **이미지에는 가중치가 구워져 있으므로** 위 명령에서는 실행됩니다.

즉 한 줄이 전부를 덮습니다.

| 층 | `docker compose run --build --rm test` |
|------|:---:|
| 구조 층 (필터·순서·경계·오류) | ✅ |
| 실물 Chroma 어댑터 | ✅ (`depends_on`이 띄웁니다) |
| 검색 품질 (임베딩 실물) | ✅ (가중치가 이미지에 있습니다) |

**LLM 구독도 API 키도 필요 없습니다.** 건너뛴 항목이 있으면 실행 결과에 사유와 함께 표시됩니다.

호스트에서 직접 돌리고 싶다면 아래도 됩니다. 이때 실물 Chroma 층은 `docker compose up -d --wait vector-store`로 서버를 띄워야 실행되고, 검색 품질 층은 임베딩 가중치가 캐시돼 있어야 실행됩니다.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest
```

린트·포맷:

```bash
docker compose run --build --rm test ruff check .
docker compose run --build --rm test ruff format --check .
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
| `APP_RETRIEVAL_TOP_K` | 검색 기본 상위 K. 요청의 `top_k`가 덮어씁니다 | `5` |
| `APP_RETRIEVAL_MAX_TOP_K` | 요청이 지정할 수 있는 `top_k`의 상한 | `50` |
| `APP_RETRIEVAL_MIN_SCORE` | 유사도 하한. 이 값 미만인 청크는 반환되지 않습니다 | `0.82` |
| `APP_RETRIEVAL_MAX_QUERY_CHARS` | 질의 문자 수 상한 | `1000` |

구현되지 않은 `APP_CHUNK_STRATEGY` 값(현재 구현된 것은 `recursive` 하나입니다)이나 청크 크기 이상의 겹침을 넣으면 **기동에 실패합니다.** 잘못된 색인 구성으로 조용히 뜨는 것보다 낫기 때문입니다.

같은 이유로 **`APP_RETRIEVAL_TOP_K`와 `APP_RETRIEVAL_MAX_TOP_K`는 함께 검증됩니다** — 기본 K가 상한보다 크면 어떤 요청도 통과할 수 없으므로 기동을 막습니다. 두 값이 각각은 멀쩡한데 조합이 성립하지 않는 자리라, 첫 검색 요청이 아니라 기동에서 드러나야 합니다.

`APP_RETRIEVAL_MIN_SCORE`의 기본값 `0.82`는 감으로 적은 값이 아니라 **계측값**입니다 — `sample-docs`의 두 문서로 관련 질의 4개와 무관 질의 3개의 점수 분포를 실제로 재서 그 사이에 놓았습니다(관련 1위 최솟값 0.8511 / 무관 1위 최댓값 0.8134). 표본이 문서 2개·질의 7개뿐이라는 한계와 계측 절차는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)에 적어 두었습니다. 임베딩 모델을 바꾸면 점수 분포가 통째로 이동하므로 이 값도 다시 재야 합니다.

`APP_EMBEDDING_MODEL`·`APP_CHUNK_STRATEGY`·`APP_CHUNK_SIZE`·`APP_CHUNK_OVERLAP`은 `index_signature`의 재료입니다. 바꾸면 기존 문서가 다음 기동에서 [`stale`](#index_status--그-문서가-지금-검색-가능한가)이 되어 재업로드가 필요합니다. 나머지 값(배치 크기·업로드 상한·동시성)은 저장된 벡터를 바꾸지 않으므로 서명에 영향을 주지 않습니다 — 성능 튜닝이 전면 재색인을 유발하지 않습니다.

`.env` 파일도 읽습니다. 환경을 직접 조회하는 곳은 `src/app/config.py` 하나뿐이며, 다른 모듈의 직접 조회는 린트 규칙으로 막혀 있습니다.

## 기술 선택

| 계층 | 선택 | 이유 |
|------|------|------|
| 언어 / 프레임워크 | Python 3.11+ / FastAPI | 과제 고정 조건. SSE 스트리밍과 async 파이프라인이 네이티브 |
| LLM SDK | **Codex SDK** (`@openai/codex`) | API 키 없이 구독으로 동작. `claude-code-sdk`에서 갈아탔습니다 — 자격증명이 세 OS 모두 **평문 파일**이라 컨테이너가 볼륨으로 읽을 수 있고, 그래야 `docker compose up` 한 줄에 인증이 들어옵니다 ([경위](./ARCHITECTURE.md#llm-sdk-통합-방식)) |
| 임베딩 | sentence-transformers (`intfloat/multilingual-e5-small`) | 로컬 오픈소스 모델이라 테스트가 LLM 구독 없이 실행됨. 다국어·512 토큰 창 |
| 문서 레지스트리 | SQLite (표준 라이브러리) | "지금 유효한 리비전이 무엇인가"의 단일 답. 컨테이너를 늘리지 않고 벡터 스토어와 같은 볼륨에 놓임 |
| 벡터 DB | Chroma (**서버 모드**, 별도 컨테이너) | 메타데이터 필터와 문서 단위 삭제를 지원해 리비전 교체·캐시 무효화 연동이 가능. 저장소를 앱 프로세스 밖으로 빼 API 재배포와 수명이 분리됨 |
| PDF 파싱 | PyMuPDF | 쪽 단위 텍스트 추출이 정확하고 빠름. **AGPL-3.0**이므로 배포 형태를 바꿀 때 재검토가 필요 |
| 캐시 DB | Redis | 정확 매치는 키 조회, 유사 질문은 질문 임베딩 유사도로 판정. TTL·태그 기반 무효화가 자연스러움 |
| 린터 | ruff | 포매팅과 린팅을 한 도구로 통일. 레이어 경계도 린트 규칙으로 강제 |

> Codex SDK는 **아직 애플리케이션 코드에서 호출하지 않습니다** — 답변 생성 change에서 도입됩니다. Redis도 현재는 헬스 점검에만 쓰입니다. sentence-transformers·Chroma·SQLite·PyMuPDF는 수집 경로에서 실제로 쓰입니다.
>
> 실행 환경은 확인했습니다. 컨테이너 안에서 `codex login status`가 `Logged in using ChatGPT`를 반환하고 `codex exec`가 실제 답변을 생성하는 것까지 봤습니다. 남은 미검증 가정은 [`docs/SPIKES.md`](./docs/SPIKES.md)에 모아 두었고, 각각 다음 change의 첫 태스크가 됩니다.

설계 근거는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)에 있습니다.

## 회고

`git commit`마다 Retrobot이 작업 로그를 분석해 KPT 회고를 [`retros/`](./retros/)에 생성합니다. 활성화:

```bash
git config core.hooksPath .githooks
```
