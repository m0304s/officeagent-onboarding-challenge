# Document Q&A API

문서를 업로드하면 텍스트를 추출·색인하고, 사용자 질문에 대해 **업로드된 문서만을 근거로** 답변을 생성하는 RAG 파이프라인 REST API.

지란지교 백엔드 온보딩 과제 구현물입니다. 설계 의도와 기술 선택 근거는 [`ARCHITECTURE.md`](./ARCHITECTURE.md), 프롬프트 설계 전략은 [`PROMPT_DESIGN.md`](./PROMPT_DESIGN.md)에 있습니다. 과제 원문은 [`docs/PRD.md`](./docs/PRD.md)입니다.

---

## 빠른 실행

```bash
docker compose up
```

**Docker와 Docker Compose만 있으면 됩니다.** LLM 자격증명 주입까지 이 한 줄 안에 들어 있고, 윈도우·macOS·리눅스가 모두 같습니다.

- 서버 주소: `http://127.0.0.1:8000`
- 확인: `curl http://127.0.0.1:8000/health`
- API 문서: `http://127.0.0.1:8000/docs`
- 첫 빌드는 임베딩(471MB) + 리랭커(2.2GB) 가중치를 이미지에 굽기 때문에 시간이 걸립니다.

| 서비스 | 이미지 | 포트 | 비고 |
|---|---|---:|---|
| `auth` | `alpine:3.21` | — | 호스트 Codex 자격증명을 컨테이너로 옮기고 **한 번 돌고 끝납니다** |
| `api` | 이 리포 | 8000 | |
| `vector-store` | `chromadb/chroma:1.5.9` | 8001 | 클라이언트와 버전 고정 |
| `cache` | `redis:8-alpine` | — | 유사 매치에 벡터셋(`VADD`/`VSIM`)을 씁니다 — **Redis 8 이상 필요** |
| `vector-store-ui` | `chromadb-admin` | 3001 | `--profile gui` 로만 뜹니다 |
| `test` | 이 리포(`test` 스테이지) | — | `docker compose run` 으로만 |

| 하려는 것 | 명령 |
|---|---|
| 기동 | `docker compose up` |
| 코드 수정 후 재기동 | `docker compose up --build` |
| 정지 | `docker compose down` |
| 로그 | `docker compose logs -f api` |

> **LLM 자격증명** — `auth` 서비스가 호스트의 `~/.codex`(또는 `CODEX_HOME`)에서 인증 정보를 읽어 `.secrets/codex/`로 복사하고, `api` 컨테이너가 그것을 마운트합니다. 인증이 없어도 **서비스는 정상 기동하고 `/health`·`/documents`·`/search` 는 모두 동작합니다.** `/qa` 만 `llm_unauthenticated` 로 실패합니다 — 자격증명이 기동 조건이 아닌 것이 설계입니다.

---

## 데모 UI (선택)

```bash
cd demo-ui && npm install && npm run dev   # http://localhost:5173
```

Node 를 호스트에 두지 않았다면 컨테이너로도 띄웁니다 (`demo` 프로필). 프록시 타깃은
`VITE_API_TARGET=http://api:8000` 으로 서비스 이름을 가리키게 되어 있습니다.

```bash
docker compose --profile demo up demo-ui   # http://localhost:5173
```

### Cloudflare 터널로 공개 노출 (선택, `tunnel` 프로필)

`api` 를 Cloudflare 터널로 외부에 노출하려면 토큰을 `.env` 에 두고 프로필로 켭니다.
같은 compose 기본 네트워크라 대시보드 ingress 의 Service 는 `http://api:8000` 입니다.

```bash
echo 'CF_TOKEN=eyJhIjoi...' >> .env               # 터널 토큰 (커밋 안 됨: .env 는 gitignore)
docker compose --profile tunnel up -d api cloudflared
```

기본 `docker compose up` 에는 뜨지 않습니다 — 토큰이 없으면 크래시 루프라 프로필로 격리했습니다.

**`127.0.0.1` 이 아니라 `localhost` 입니다** — `vite.config.ts` 에 `server.host` 를 두지 않아 Vite 기본값인 이름 바인딩을 따릅니다. API 서버(`http://127.0.0.1:8000`)와 주소 표기가 다른 것은 그래서입니다.

Vite dev 서버가 `/api` 를 백엔드로 프록시합니다. **서버 코드는 한 줄도 바뀌지 않았습니다** — 이 화면은 공개 계약(`/documents`·`/search`·`/qa`·`/health`)의 소비자일 뿐입니다.

만든 이유는 스트리밍이 **관측 가능해야** 하기 때문입니다. `curl -N` 으로는 조각이 도착한 시각이 화면에 남지 않아, 답변을 모아서 한 번에 보내는 구현과 조각으로 흘리는 구현이 눈으로 구분되지 않습니다.

---

## 주요 기능

| 기능 | 설명 |
|---|---|
| 문서 수집 | PDF / TXT / Markdown 업로드 → 재귀 청킹 → 배치 임베딩 → 벡터·어휘 **두 색인**에 저장 |
| 하이브리드 검색 | 밀집(ChromaDB) + 어휘(SQLite FTS5 BM25)를 **RRF** 로 융합, 요청당 팬아웃 한 번 |
| 크로스인코더 리랭킹 | 융합 상위 후보를 `bge-reranker-v2-m3` 로 재정렬. 실패·초과 시 융합 순서로 축소 |
| LLM 답변 생성 | `codex app-server` 세션 위 턴 하나. 출처 마커 검증 + 환각 억제 |
| 스트리밍 응답 | SSE — `sources` → `answer`* → (`done` \| `error`). 종료 이벤트는 **정확히 하나** |
| 2계층 캐싱 | L1 정확 매치(SHA-256) + L2 유사 매치(코사인 0.93) + **부정 극성 게이트** |
| 캐시 무효화 | 문서 태그 무효화 + 부정 판정 집합 + **읽기 시점 현재성 재검증** 두 겹 |
| 장애 격리 | 캐시 0.2초 타임아웃 + 차단기, retriever 부분 실패 허용, 리랭커 축소 |

---

## API 명세

### 문서 API

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| `POST` | `/documents` | 업로드 → 추출 → 청킹 → 임베딩 → 두 색인 저장. 최초 수집 `201`, 그 외 `200` |
| `GET` | `/documents` | 수집된 문서 목록 (최근 수집 순) |
| `GET` | `/documents/{document_id}` | 문서 한 건. 없으면 `404` |
| `DELETE` | `/documents/{document_id}` | 문서와 그 청크 전부 제거 + 연관 캐시 무효화. `204` |

```bash
curl -s -X POST http://127.0.0.1:8000/documents \
  -F "file=@sample-docs/company-policy.txt"
```

```json
{"document_id":"0de0c0a1-311b-5fc7-a4f7-763b45bc2444","filename":"company-policy.txt",
 "format":"txt","revision":"2ccbdc608106...","index_signature":"e78999af0e98a7d4",
 "index_status":"indexed","chunk_count":1,"byte_size":1172,
 "ingested_at":"2026-08-03T11:36:21.140245Z","status":"created","previous_revision":null}
```

`status` 가 이번 요청이 무엇을 했는지 말합니다 — `created`(신규) / `replaced`(내용 변경) / `reindexed`(색인 구성 변경) / `unchanged`(둘 다 같음, 임베딩 재계산 없음).

### 검색 API

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| `POST` | `/search` | 활성 retriever 융합 결과를 최대 K개. 순서를 정한 신호는 응답의 `ordered_by` 가 밝힙니다 — 리랭킹이 돌았으면 `rerank_score`, 아니면 융합 `score` 내림차순 |

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"query":"교육비는 얼마까지 지원되나요?","top_k":3}'
```

```json
{"query":"교육비는 얼마까지 지원되나요?","top_k":3,"count":1,
 "retrievers":["dense","lexical"],"ordered_by":"rerank",
 "reranker":"BAAI/bge-reranker-v2-m3",
 "results":[{"document_id":"0de0c0a1-...","filename":"company-policy.txt",
   "format":"txt","revision":"9f2b7c1e...","chunk_index":0,
   "text":"[사내 복리후생 안내]\n\n1. 교육비 지원\n임직원은 연간 최대 200만원까지 ...",
   "score":1.0,"rerank_score":0.7388216138591319,
   "char_start":0,"char_end":527,"page":null,
   "contributions":[{"retriever":"dense","rank":1,"native_score":0.86091983},
                    {"retriever":"lexical","rank":1,"native_score":0.6117184199523331}]}]}
```

**`score` 는 유사도가 아니라 융합 점수입니다** — 활성 retriever 들이 이 청크를 얼마나 나란히 상위로 꼽았는가입니다. 둘 다 1위로 꼽으면 정의상 `1.0` 이 됩니다. 각자가 매긴 원점수는 `contributions` 에 그대로 남습니다.

`rerank_score` 는 교정되지 않은 값이라 **같은 질의 안에서만** 비교할 수 있습니다. 두 값을 함께 싣는 이유는 서로 다른 질문에 답하기 때문입니다 — `score` 는 "몇 개의 목록이 이것을 위에 뒀는가", `rerank_score` 는 "이것이 이 질문에 답하는가".

**검색은 답변을 만들지 않습니다.** 답이 이상할 때 원인이 검색인지 생성인지 가르는 관측 지점이라 `/qa` 가 생긴 뒤에도 남겼습니다.

### 답변 API (SSE)

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| `POST` | `/qa` | 문서 근거 기반 답변을 SSE로 스트리밍 |

```bash
curl -N -X POST http://127.0.0.1:8000/qa \
  -H 'content-type: application/json' \
  -d '{"question":"교육비는 얼마까지 지원되나요?"}'
```

요청 본문은 `question`(필수)과 `top_k`(선택, `/search` 와 같은 상한)입니다. `top_k` 를 주지 않으면 `APP_RETRIEVAL_TOP_K` 를 씁니다.

```
event: sources
data: {"results":[...],"count":1,"top_k":5,"target_documents":2,
       "ordered_by":"rerank","reranker":"BAAI/bge-reranker-v2-m3"}

event: answer
data: {"text":"임직원은 연간 최대 "}

event: answer
data: {"text":"200만원까지 직무 관련 교육비를 지원받을 수 있습니다"}

event: done
data: {"finish_reason":"stop","answer":"임직원은 연간 최대 200만원까지 ... [1]",
       "citations":[{"marker":1,"document_id":"0de0c0a1-...","filename":"company-policy.txt",
                     "format":"txt","revision":"9f2b7c1e...","chunk_index":0,
                     "char_start":0,"char_end":527,"page":null,"score":1.0}],
       "dropped_markers":0,"elapsed_ms":14203,
       "cache_hit":false,"cache_layer":null,"cache_similarity":null}
```

**이벤트 어휘는 넷이고 순서가 계약입니다.**

| 이벤트 | 횟수 | 내용 |
|---|---|---|
| `sources` | 정확히 1회, 항상 첫 번째 | 무엇을 근거로 답하려는가 (`/search` 와 같은 모양) |
| `answer` | 0회 이상 | 생성기가 내보낸 조각에서 첫 줄의 판정(`VERDICT:`)을 걷어낸 본문. 판정 줄 확정에 필요한 앞부분만 병합되고, **그 뒤로는 서버가 쪼개거나 합치지 않습니다** |
| `done` \| `error` | **둘을 합쳐 정확히 1회, 항상 마지막** | 종료 없는 닫힘은 없습니다 |

`error` 이벤트는 `code` · `message` 에 더해 **`attempts`**(소진한 시도 수)와 **`reason`**(`timeout` / `generation_failed` / `unauthenticated`)을 싣습니다. 코드는 종료 사건을, 사유는 그 원인을 가리킵니다 — 갈라야 운영자가 재시도할지 자격증명을 고칠지 정할 수 있습니다.

불변식: `answer` 들을 이어 붙인 것 == `done.answer`.

`finish_reason` 은 셋입니다 — `stop`(답변함) / `no_evidence`(근거 0건이라 생성기를 부르지 않음) / `insufficient_evidence`(근거는 있었지만 생성기가 그것으로 답할 수 없다고 판정). 뒤의 둘을 가르는 이유는 사용자가 할 일이 다르기 때문입니다(문서를 올린다 / 질문을 바꾼다).

`cache_hit` · `cache_layer`(`exact`/`semantic`) · `cache_similarity` 로 캐시 판정을 응답에서 바로 확인할 수 있습니다. 캐시 히트도 미스와 **같은 이벤트 어휘·순서**로 재생됩니다(`answer` 는 본문 통째 1회 — 히트에는 조각이 도착하는 사건이 없습니다) — 클라이언트가 두 화면을 그리지 않아도 됩니다.

### 오류 응답

```json
{"error":{"code":"query_too_long","message":"질의가 문자 수 상한(1000자)을 넘었습니다",
          "max_query_chars":1000,"max_query_tokens":512}}
```

`422`(질의 검증) · `404`(없는 문서) · `405`(허용되지 않은 메서드) · `413`(업로드 상한) · `415`(미지원 포맷) · `500`(처리하지 못한 예외) · `503`(저장소 장애)로 갈립니다. 어느 경우든 봉투는 같은 모양입니다. **상태 코드로 끝나는 실패와 스트림 안의 `error` 이벤트로만 알릴 수 있는 실패가 코드 구조로 갈려 있습니다** — 상태 코드는 첫 바이트와 함께 확정되기 때문입니다.

---

## 디렉토리 구조

```
src/app/
├── main.py                     # 앱 팩토리와 의존성 배선 — 전역 싱글턴 없음
├── config.py                   # 환경변수 → 설정 객체. 환경을 읽는 유일한 곳
│
├── core/                       # 도메인 — I/O 없음, 표준 라이브러리만
│   ├── .ruff.toml              #   이 경계를 강제하는 린트 규칙 (아래)
│   ├── documents.py            #   문서/청크 값 객체, 식별자·색인 서명 유도
│   ├── chunking.py             #   청킹 전략 — 재귀 분할
│   ├── retrieval.py            #   검색 결과 값 객체 — ScoredChunk 불변식
│   ├── fusion.py               #   RRF 융합 — 순위만 쓰고 점수는 안 씀
│   ├── reranking.py            #   재정렬 규칙 — 모델도 점수의 출처도 모름
│   ├── lexical.py              #   어휘 토큰화·희소도 — 색인과 질의가 같은 규약
│   ├── cache.py                #   질의 정규화, 항목 지문 유도, 부정 극성, 코사인
│   ├── prompting.py            #   프롬프트 조립 + 출력 파싱 + VerdictSplitter
│   ├── answers.py              #   답변 도메인 — 종료 사유·인용·불변식
│   ├── exceptions.py           #   도메인 예외와 안정적인 오류 코드
│   └── models.py               #   상태 값 객체 — 프로브 결과와 헬스 리포트
│
├── services/                   # 오케스트레이션 — 순서와 정책
│   ├── ingestion.py            #   파싱 → 청킹 → 토큰 가드 → 저장 → 커밋 → 정리
│   ├── retrieval.py            #   대상집합 → 팬아웃 → 융합 → 채택 → 리랭킹 → 재검증
│   ├── qa.py                   #   prepare(스트림 밖) / stream(스트림 안)
│   ├── cache.py                #   조회 순서, 현재성 재검증, 저장 정책, 차단기
│   └── health.py               #   프로브 병렬 실행 + 개별·전체 상한, 실패 격리
│
├── adapters/                   # 인프라 — 프로토콜 뒤에 있음
│   ├── protocols.py            #   어댑터 계약 전부
│   ├── embedding/local.py      #   sentence-transformers — e5 역할 접두사가 여기 갇힘
│   ├── vector_store/
│   │   ├── chroma.py           #     쓰기·삭제·집계·질의. 거리→유사도 변환이 여기 갇힘
│   │   ├── client.py           #     서버 접속 — 주소 해석과 클라이언트 생성
│   │   └── probe.py            #     헬스 프로브
│   ├── lexical/sqlite.py       #   SQLite FTS5 — BM25 부호 뒤집기가 여기 갇힘
│   ├── retrievers/
│   │   ├── dense.py            #     질의 임베딩 + 벡터 스토어 질의
│   │   ├── lexical.py          #     어휘 색인을 retriever 계약 뒤로 감쌈
│   │   └── registry.py         #     이름 → retriever 팩토리 표
│   ├── reranking/local.py      #   크로스인코더 — 서명·점수 규약·선언 대조
│   ├── registry/sqlite.py      #   SQLite 문서 레지스트리
│   ├── parsers/
│   │   ├── text.py             #     txt · md
│   │   ├── pdf.py              #     PDF 평문 — PyMuPDF
│   │   ├── pdf_markdown.py     #     PDF 구조 보존 — pymupdf4llm
│   │   ├── selection.py        #     추출 방식 → 구현 배선 (이 축의 유일한 진실)
│   │   ├── registry.py         #     파일명 → 파서 배선
│   │   └── normalization.py    #     파서들이 공유하는 텍스트 정규화
│   ├── cache/
│   │   ├── store.py            #     Redis — 페이로드·벡터·근접 색인·순서·태그
│   │   ├── memory.py           #     인메모리 — 기본 실행이 캐시 의미를 검증하는 구현
│   │   ├── null.py             #     꺼진 상태 — 언제나 미스, 저장은 무동작
│   │   ├── codec.py            #     값 객체 ↔ JSON, 질의 벡터 ↔ float32 바이트
│   │   ├── redis.py            #     연결 — 지연 생성·재사용·종료
│   │   └── probe.py            #     헬스 프로브
│   └── llm/
│       ├── session.py          #     세션 하나 — 프로세스 수명과 stdio JSON-RPC
│       ├── pool.py             #     세션 풀 — 지연 기동, 상한, 죽은 세션 교체
│       └── codex.py            #     턴 하나 — 델타 파싱, 타임아웃, 인증 판정
│
└── api/                        # 전송 — 라우터, SSE 프레이밍, 오류 봉투
    ├── routes/
    │   ├── documents.py        #     POST · GET · DELETE /documents
    │   ├── search.py           #     POST /search
    │   ├── qa.py               #     POST /qa (SSE)
    │   └── health.py           #     GET /health
    ├── queries.py              #   /search · /qa 공통 경계 — 질의 상한과 근거 뷰
    ├── sse.py                  #   QaEvent → 전송 형식, 하트비트 주석
    ├── errors.py               #   오류 봉투와 예외 → HTTP 변환
    └── logging.py              #   구조화 로깅과 request-id
```

**`core` 는 앱 안의 어느 것도 import 하지 않습니다.** 바깥으로 나가는 화살표가 하나도 없는 층이 하나 있어야 나머지 방향이 의미를 갖습니다. `adapters` 는 `core` 만 보고, `services` 와 `api` 는 구현이 아니라 **계약**(`adapters/protocols.py`)에 의존합니다 — 유일한 예외가 `services/ingestion.py` 의 `ParserRegistry` 이고, 포맷→파서 배선이 그 자리에서만 필요해 프로토콜을 하나 더 만들지 않았습니다.

강제는 컨벤션이 아니라 **`ruff` 종료 코드**입니다.

| 규칙 | 어디에 | 무엇을 막는가 |
|---|---|---|
| `flake8-tidy-imports.banned-api` | `pyproject.toml` | `os.environ` · `os.getenv` — 설정은 `app.config` 한 창구로만. `config.py` 와 `tests/*` 만 면제 |
| 같은 규칙, 더 좁게 | `src/app/core/.ruff.toml` | `core/` 에서 `fastapi` · `pydantic` · `pydantic_settings` · `chromadb` · `redis` · `httpx` · `fitz` · `pymupdf` · `pymupdf4llm` · `sentence_transformers` · `torch` · `numpy` import 금지 (12개) |

`ruff` 는 대상 파일에서 **가장 가까운 설정**을 쓰므로 `core/.ruff.toml` 은 그 디렉터리 아래에만 적용됩니다. 자식 설정의 `banned-api` 테이블이 부모 것을 덮어쓰기 때문에 전역 금지 항목(`os.environ` 등)을 그 파일에 다시 적어 두었습니다.

---

## 기술 스택 (요약)

| 영역 | 기술 | 비고 |
|---|---|---|
| 언어 / 프레임워크 | Python 3.11 + FastAPI | async/await 네이티브 |
| 벡터 DB | ChromaDB 1.5.9 (서버 모드) | 컬렉션이 벡터 차원마다 나뉨 |
| 어휘 색인 | SQLite FTS5 + BM25 | 파일 하나, 컨테이너 0개 |
| 캐시 DB | **Redis 8** | 유사 매치 후보 선택에 벡터셋 사용 |
| 임베딩 | `intfloat/multilingual-e5-small` | 384차원, 입력 창 512토큰, `query:`/`passage:` 접두사 |
| 리랭커 | `BAAI/bge-reranker-v2-m3` | 학습 언어에 한국어, **원격 코드 없음** |
| LLM | `codex app-server` (`@openai/codex`) | stdio JSON-RPC, 세션 풀 |
| 문서 레지스트리 | SQLite | 벡터 스토어와 분리 |

선택 근거와 기각한 대안은 [`ARCHITECTURE.md`](./ARCHITECTURE.md) 1절에 있습니다.

---

## 테스트

```bash
docker compose run --build --rm test
```

**호스트에 파이썬도 자격증명도 필요 없습니다.** Docker만 있으면 됩니다.

대역인 것은 **생성기 하나**입니다. 나머지는 실물이되 요구가 이 한 줄 안에서 해소됩니다 — 임베딩·리랭커 가중치는 **이미지에 구워져 있고**(그래서 첫 빌드가 깁니다), Chroma와 Redis는 `depends_on` 이 함께 띄웁니다. 그래서 검색 품질과 실물 어댑터 층까지 이 명령 하나로 돕니다.

| 마커 | 명령 | 무엇을 더 덮는가 | 추가로 필요한 것 |
|---|---|---|---|
| (기본) | `docker compose run --build --rm test` | 구조 층 + 실물 Chroma + 실물 임베딩(검색 품질) + 캐시 의미(인메모리) | 없음 |
| `redis` | `... test pytest -m redis` | Redis에서만 존재하는 것 — TTL, 지연 정리, 용량 상한이 페이로드 키까지 지우는가 | 없음 (`depends_on` 이 띄웁니다) |
| `slow` | `... test pytest -m slow -s` | 골든셋 44문항 리랭킹 **순위** 비교 | 없음. CPU에서 30분대 |
| `llm` | `... test pytest -m llm` | 실물 `codex app-server` 호출 | **Codex 구독** |
| `llm and slow` | `... test pytest -m "llm and slow"` | 골든셋 **답변** 채점 (LLM-Judge) | **Codex 구독**. 실측 44분 |

과제가 요구한 세 경로는 각각 다음이 덮습니다.

| 경로 | 테스트 |
|---|---|
| Ingestion | `test_ingestion_pipeline.py` — 업로드 → 추출 → 청킹 → 두 색인 저장 |
| Retrieval | `test_retrieval_quality.py` — **실물 임베더 + 실물 FTS5** 로 기대 청크가 상위에 오는가 |
| 캐시 무효화 | `test_cache_invalidation.py` — 문서 변경·삭제 후 연관 캐시가 실제로 사라지는가 |

무엇을 왜 테스트했는지는 [`tests/README.md`](./tests/README.md)에 있습니다. 특히 **"성패로 드러나지 않는 요구사항"** 을 어떻게 관측 가능하게 만들어 단언했는지가 거기 있습니다 — 예를 들어 동시 생성 상한은 있든 없든 모든 테스트가 통과하므로, "동시에 열려 있던 시도의 최대치" 카운터를 하네스가 기록하게 만들고 그것을 단언합니다.

---

## 실측으로 확인한 것

문서에 적힌 숫자는 **실제로 재서 얻은 값**입니다. 재는 절차와 원본 표는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)에 있습니다.

| 확인 항목 | 결과 |
|---|---|
| 서버 기동 · 헬스 (`/health`) | 통과 — 캐시·벡터 스토어에 닿지 못해도 기동됨 |
| TXT / Markdown / PDF 업로드 | 통과 |
| 재업로드 교체(`replaced`) · 재색인(`reindexed`) · 단축(`unchanged`) | 통과 |
| 하이브리드가 구제하는 질의 | 밀집 단독 **0건** → 하이브리드 1~2건 (`P3`·`P4`·`hotfix`·`develop`) |
| 리랭킹 순위 개선 (골든셋 44문항) | MRR 0.761 → **0.905**, `@5` 41 → **44** |
| 리랭킹 답변 개선 (골든셋 답변 채점) | 기준 답변과 일치 **45/47 → 49/50** (게이트가 구성마다 따로 서 분모가 다릅니다) |
| L1 정확 매치 히트 | 통과 — `cache_layer: "exact"` |
| L2 유사 매치 히트 | 통과 — `cache_layer: "semantic"`, 유사도 함께 반환 |
| **부정 극성 게이트** | 통과 — `"...할 수 있나요"` ↔ `"...할 수 없나요"`(코사인 **0.9979**)가 서로 히트하지 않음 |
| 문서 변경 시 캐시 무효화 | 통과 — 태그 무효화 + 읽기 시점 재검증 |
| SSE 스트리밍 | 통과 — 델타 단위 도착, 종료 이벤트 정확히 1회 |
| 근거 없는 질문 환각 억제 | 통과 — `finish_reason: "no_evidence"`, 생성기를 부르지 않음 |
| 자격증명 없이 기동 | 통과 — `/qa` 만 실패하고 나머지 엔드포인트는 정상 |

> **리랭킹의 값어치를 순위 지표로 읽으면 안 됩니다.** `@20` 이 양쪽 다 44/44라, 융합은 이미 근거를 전부 상위 20에 넣어 두었고 리랭킹은 재배열만 했습니다. 실제로 산 것은 **상위 5 밖에 있다가 안으로 들어온 3건**이지 `@1` 의 +9가 아닙니다. 부호검정 `p ≈ 0.064` 라 문서 1건·44문항으로는 우연을 배제하지 못합니다. 자세한 한계는 [`ARCHITECTURE.md`](./ARCHITECTURE.md) 4절에 있습니다.

---

## 설정

전 항목에 기본값이 있어 **환경변수 없이도 기동됩니다.** 값이 무효하면 조용히 기본값으로 넘어가지 않고 **기동에 실패합니다.** 자주 만지는 것을 먼저 두고, 전체 목록은 그 아래에 접어 두었습니다.

| 환경변수 | 의미 | 기본값 |
|---|---|---|
| `APP_CHUNK_SIZE` / `APP_CHUNK_OVERLAP` | 청크 크기·겹침 (문자) | `600` / `100` |
| `APP_RETRIEVAL_TOP_K` / `APP_RETRIEVAL_MAX_TOP_K` | 상위 K 기본값 / 상한 | `5` / `20` |
| `APP_RETRIEVAL_MIN_SCORE` | 밀집 코사인 하한 **(계측값)** | `0.82` |
| `APP_LEXICAL_MIN_TOKEN_RARITY` | 어휘 변별력 하한 **(계측값)** | `0.3` |
| `APP_RETRIEVERS` | 활성 retriever JSON 목록 | `dense`(필수) + `lexical`(선택) |
| `APP_RERANKER_ENABLED` | 리랭킹 on/off — **끄면 이 단계 이전과 결과가 같음** | `true` |
| `APP_CACHE_ENABLED` | 캐시 on/off — 끄면 모든 요청이 미스 | `true` |
| `APP_CACHE_SEMANTIC_THRESHOLD` | L2 코사인 임계값 **(계측값)** | `0.93` |
| `APP_QA_LLM_MODEL` | 생성 모델. 비우면 CLI 기본값 | `""` |
| `APP_QA_CONCURRENCY` | 동시 생성 상한 (= 세션 풀 크기) | `2` |

<details>
<summary><b>전체 목록 (43개)</b></summary>

| 환경변수 | 의미 | 기본값 |
|----------|------|--------|
| `APP_APP_NAME` | 서비스 이름 | `document-qa-api` |
| `APP_LOG_LEVEL` | 로그 레벨 | `INFO` |
| `APP_CACHE_URL` | 캐시 저장소 접속 URL | `redis://localhost:6379/0` |
| `APP_VECTOR_STORE_URL` | 벡터 스토어(Chroma 서버) 주소 | `http://localhost:8001` |
| `APP_PROBE_TIMEOUT_SECONDS` | 의존성 점검 개별 상한(초) | `2.0` |
| `APP_HEALTH_TOTAL_TIMEOUT_SECONDS` | 헬스 점검 전체 상한(초) | `5.0` |
| `APP_REGISTRY_PATH` | 문서 레지스트리(SQLite) 경로 | `./data/registry.sqlite3` |
| `APP_LEXICAL_INDEX_PATH` | 어휘 색인(SQLite FTS5) 경로 | `./data/lexical.sqlite3` |
| `APP_LEXICAL_MIN_TOKEN_RARITY` | 질의 토큰이 "드물다"고 인정받는 하한. 이 값을 넘는 토큰이 하나도 겹치지 않는 청크는 어휘 검색 결과에서 빠집니다 | `0.3` |
| `APP_EMBEDDING_MODEL` | 임베딩 모델 이름 | `intfloat/multilingual-e5-small` |
| `APP_PDF_EXTRACTION` | PDF 추출 방식. `markdown` 은 제목·표를 마크다운으로 보존하고, `plain` 은 쪽 단위 평문만 뽑습니다 | `markdown` |
| `APP_CHUNK_STRATEGY` | 분할 전략 | `recursive` |
| `APP_CHUNK_SIZE` | 청크 크기 상한(문자) | `600` |
| `APP_CHUNK_OVERLAP` | 인접 청크 겹침(문자). `0` 불가 | `100` |
| `APP_EMBEDDING_BATCH_SIZE` | 임베딩·저장 배치 크기 | `64` |
| `APP_MAX_UPLOAD_BYTES` | 업로드 크기 상한 | `20971520` (20 MiB) |
| `APP_INGESTION_CONCURRENCY` | 동시 수집 상한 | `2` |
| `APP_RETRIEVERS` | 활성 retriever JSON 목록 — 이름·가중치·후보 깊이·필수 여부 | `dense`(필수) + `lexical`(선택) |
| `APP_RETRIEVAL_RRF_K` | RRF 상수. 클수록 상위 순위의 우대가 약해집니다 | `60` |
| `APP_RETRIEVAL_TOP_K` | 검색 기본 상위 K. 요청의 `top_k` 가 덮어씁니다 | `5` |
| `APP_RETRIEVAL_MAX_TOP_K` | 요청이 지정할 수 있는 `top_k` 의 상한 | `20` |
| `APP_RETRIEVAL_MIN_SCORE` | **밀집 retriever의** 코사인 유사도 하한. 목록을 자르지 않고 표시만 하며, 어느 목록에서도 하한을 못 넘은 청크가 융합 뒤에 버려집니다 | `0.82` |
| `APP_RETRIEVAL_MAX_QUERY_CHARS` | 질의 문자 수 상한 | `1000` |
| `APP_RERANKER_ENABLED` | 크로스인코더 리랭킹 사용 여부. 끄면 융합 순서를 그대로 씁니다 | `true` |
| `APP_RERANKER_MODEL` | 리랭커 모델 이름. 아는 모델이 아니면 **기동에 실패합니다** | `BAAI/bge-reranker-v2-m3` |
| `APP_RERANK_CANDIDATES` | 리랭커에게 넘길 융합 상위 후보 수. `APP_RETRIEVAL_MAX_TOP_K` 이상이어야 합니다 | `30` |
| `APP_RERANKER_TIMEOUT_SECONDS` | 리랭킹 한 번의 시간 상한(초). 넘기면 융합 순서로 축소 | `15.0` |
| `APP_QA_LLM_TIMEOUT_SECONDS` | 생성 **한 시도**의 시간 상한(초) | `60.0` |
| `APP_QA_LLM_MAX_ATTEMPTS` | 최대 시도 횟수. `1` 이면 재시도하지 않음 | `3` |
| `APP_QA_LLM_RETRY_BACKOFF_SECONDS` | 재시도 백오프 기준(초). 대기가 1초 → 2초로 늘어남 | `1.0` |
| `APP_QA_SSE_HEARTBEAT_SECONDS` | 조용한 구간에 `: keep-alive` 주석을 내보내는 간격(초) | `15.0` |
| `APP_QA_CONCURRENCY` | 동시 생성 상한 = **세션 풀 크기**. 걸린 요청은 실패하지 않고 대기 | `2` |
| `APP_QA_LLM_MODEL` | 모델 식별자. 비우면 CLI 기본값 | (빈 값) |
| `APP_QA_LLM_INTERRUPT_GRACE_SECONDS` | 턴 중단 후 종료 알림을 기다리는 유예(초). 넘기면 세션 폐기 | `2.0` |
| `APP_QA_LLM_SESSION_STARTUP_TIMEOUT_SECONDS` | 생성기 프로세스 기동 + 핸드셰이크 상한(초) | `30.0` |
| `APP_CACHE_ENABLED` | 응답 캐시 사용 여부. 끄면 **모든 요청이 미스**입니다 | `true` |
| `APP_CACHE_TTL_SECONDS` | 캐시 항목의 수명(초) | `86400` |
| `APP_CACHE_MAX_ENTRIES` | 보관 항목 수 상한. 넘으면 오래된 것부터 밀려납니다 | `500` |
| `APP_CACHE_SEMANTIC_THRESHOLD` | 유사 질문으로 인정하는 코사인 유사도 하한 | `0.93` |
| `APP_CACHE_SEMANTIC_CANDIDATES` | 유사 매치가 한 요청에서 훑는 후보 수 상한 | `20` |
| `APP_CACHE_OPERATION_TIMEOUT_SECONDS` | 캐시 작업 하나의 시간 상한(초) | `0.2` |
| `APP_CACHE_CIRCUIT_BREAKER_FAILURES` | 연속 실패가 이만큼이면 캐시 호출을 건너뜁니다 | `3` |
| `APP_CACHE_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | 건너뛰는 시간(초). 지나면 자동 재개 | `30.0` |

**여섯 경우가 기동을 막습니다** — 겹침이 청크 크기 이상, 기본 K가 상한 초과, retriever 목록이 빔, `candidate_depth` 가 `APP_RETRIEVAL_MAX_TOP_K` 미만, `APP_RERANK_CANDIDATES` 가 `APP_RETRIEVAL_MAX_TOP_K` 미만, 리랭커 입력 창이 `APP_RETRIEVAL_MAX_QUERY_CHARS + APP_CHUNK_SIZE` 미만. 잘못된 구성으로 조용히 뜨는 것보다 낫기 때문입니다.

</details>

**`APP_EMBEDDING_MODEL` · `APP_CHUNK_STRATEGY` · `APP_CHUNK_SIZE` · `APP_CHUNK_OVERLAP` · `APP_PDF_EXTRACTION` 과 어휘 토큰화 구성은 `index_signature` 의 재료입니다.** 바꾸면 기존 문서가 다음 기동에서 `stale` 이 되어 재업로드가 필요합니다. 나머지 값(하한·동시성·배치 크기)은 저장된 내용을 바꾸지 않으므로 서명에 들어가지 않습니다 — **하한 조정이 전면 재색인을 유발하지 않습니다.**

> `APP_CACHE_SEMANTIC_THRESHOLD` 를 **내리기 전에** [`ARCHITECTURE.md`](./ARCHITECTURE.md) 5절 「임계값 0.93」을 반드시 읽으십시오. 부정문 쌍은 이 값으로 걸러지지 않고, 그것을 막는 것은 별도의 극성 게이트입니다.

---

## 개발 과정

`openspec/` 아래에 change 단위 제안서·설계·요구사항·작업 분해가 있고, `retros/` 에 커밋 시점 회고가 있습니다. 기각한 설계와 **되돌린 결정**도 함께 남겼습니다 — 다음 사람을 막는 것은 채택 이유가 아니라 기각 이유이기 때문입니다.

| 아카이브된 change | 무엇을 |
|---|---|
| `bootstrap-runtime-skeleton` | 레이어·프로토콜·설정·헬스 |
| `add-document-ingestion` | 수집 파이프라인, 색인 서명 |
| `add-qa-retrieval` | 벡터 검색, 하한 계측 |
| `add-answer-generation` | SSE, 세션 풀, 프롬프트 |
| `add-response-cache` | L1/L2, 무효화 |
| `add-rrf-algorithm-spec` | 하이브리드 + RRF |
| `add-cross-encoder` | 리랭킹, 골든셋 평가 |
| `add-pymupdf4llm-parser` | PDF 추출 교체 |
| `fix-retrieval-cache-defects` | 게이트 비대칭·부정 극성·후보 선택 |

아카이브되지 않은 change 가 둘 있고 상태가 서로 다릅니다.

- `openspec/changes/add-demo-ui/` — **구현은 됐고**(`demo-ui/`) 아카이브만 남았습니다. 백엔드 기능이 아니라 스트리밍·출처·거절 동작의 검증 수단이라 평가 산출물과 수명이 다릅니다.
- `openspec/changes/add-multiturn-qa/` — **설계까지 하고 구현하지 않은 change** 입니다. 왜 이 창에서 만들지 않았는지는 그 안의 `proposal.md` 에 있습니다.

---

## 과제 필수 산출물

- [`README.md`](./README.md) : 실행 방법 + 개요
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) : 기술 스택 선택 근거, LLM SDK 통합, 레이어 설계
- [`PROMPT_DESIGN.md`](./PROMPT_DESIGN.md) : 프롬프트 설계 의도와 할루시네이션 억제 전략
- [`tests/`](./tests/) : 자동화 테스트 (`docker compose run --build --rm test`)
- [`retros/`](./retros/) : AI 협업 과정 회고
