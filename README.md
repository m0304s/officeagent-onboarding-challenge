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
| 문서 수집 — 추출 → 청킹 → 임베딩 → 벡터·어휘 색인 저장 (`POST /documents`) | 구현됨 |
| 문서 목록·상세·삭제 (`GET`/`DELETE /documents`) | 구현됨 |
| 재업로드 교체·재색인, 기동 시 저장소 정리 (두 색인 모두) | 구현됨 |
| 어휘 색인 — SQLite FTS5, BM25 순위, 한국어 토큰화 | 구현됨 (검색 경로에 연결됨) |
| 벡터 검색 — 질의 임베딩 → 대상 필터 → 상위 K (`POST /search`) | 구현됨 |
| 하이브리드 검색 — 밀집·어휘 팬아웃 + RRF 융합 | 구현됨 |
| 크로스인코더 리랭킹 — 융합 뒤 상위 후보 재정렬, 실패 시 융합 순서로 축소 | 구현됨 (설정으로 끌 수 있음) |
| LLM 답변 생성 + SSE 스트리밍 — 출처 표기·환각 억제·재시도 (`POST /qa`) | 구현됨 |
| 데모 UI — 문서 패널 + 스트리밍 Q&A 콘솔 ([`demo-ui/`](./demo-ui/)) | 구현됨 (선택 실행) |
| 응답 캐싱 — 정확 매치(L1) + 유사 매치(L2), TTL·총량 상한, 타임아웃·차단기 | 구현됨 |
| 캐시 무효화 — 문서 태그 + 부정 판정 집합, 읽기 시점 현재성 재검증 | 구현됨 |

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
{"document_id":"0de0c0a1-311b-5fc7-a4f7-763b45bc2444","filename":"company-policy.txt","format":"txt",
 "revision":"2ccbdc608106...","index_signature":"e78999af0e98a7d4","index_status":"indexed",
 "chunk_count":1,"byte_size":1172,"ingested_at":"2026-08-03T11:36:21.140245Z",
 "status":"created","previous_revision":null}
```

> 이 문서의 응답 예시는 **실제로 실행해 받은 값**입니다 — `sample-docs/` 두 건을 올리고
> 아래 검색을 그대로 호출한 한 번의 실행에서 나왔습니다. `document_id`는 파일명에서,
> `revision`은 내용에서 유도되므로 같은 파일을 올리면 같은 값이 나옵니다.
> `score`는 임베딩 실수 연산이라 플랫폼에 따라 끝자리가 다를 수 있습니다.

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
| `POST` | `/search` | 활성 retriever 들의 융합 결과를 점수 내림차순으로 최대 K개 |

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"query":"교육비는 얼마까지 지원되나요?","top_k":3}'
```

```json
{"query":"교육비는 얼마까지 지원되나요?","top_k":3,"count":1,"retrievers":["dense","lexical"],
 "ordered_by":"rerank","reranker":"BAAI/bge-reranker-v2-m3",
 "results":[{"document_id":"0de0c0a1-311b-5fc7-a4f7-763b45bc2444","filename":"company-policy.txt","format":"txt",
             "revision":"5baf958963e1...","chunk_index":0,
             "text":"[사내 복리후생 안내]\n\n1. 교육비 지원\n임직원은 연간 최대 200만원까지 직무 관련 교육비를 지원받을 수 있습니다.\n신청은 매월 15일까지 HR팀에 신청서를 제출해야 하며, 승인 후 비용이 환급됩니다. ...",
             "score":1.0,"rerank_score":0.7388216138591319,"char_start":0,"char_end":527,"page":null,
             "contributions":[{"retriever":"dense","rank":1,"native_score":0.86091983},
                              {"retriever":"lexical","rank":1,"native_score":0.6117184199523331}]}]}
```

문서 둘을 올렸는데 결과가 하나인 것은 오류가 아닙니다. 기본 청크 크기(600자)에서 `company-policy.txt`는 청크 하나가 되고, `development-guide.md`의 청크 둘은 이 질의에서 **양쪽 하한에 모두 걸려 떨어집니다** — 밀집 쪽은 코사인 하한, 어휘 쪽은 변별력 판정입니다. 검색이 하는 판정이 바로 이것입니다.

점수가 정확히 `1.0`인 것도 오류가 아닙니다. **이 값은 유사도가 아니라 합의의 정도**라서, 활성 retriever 둘이 모두 이 청크를 1위로 꼽으면 정의상 최댓값이 됩니다. 실제로 각자가 매긴 원점수는 `contributions`에 그대로 남아 있습니다(밀집 0.861, 어휘 0.612).

`score`와 `rerank_score`가 함께 실리는 것도 의도한 것입니다. **두 값은 서로 다른 질문에 답합니다** — `score`는 "몇 개의 목록이 이것을 위에 뒀는가", `rerank_score`는 "이것이 이 질문에 답하는가"입니다. 그래서 하나를 다른 하나로 갈아치우지 않았습니다. 결과가 하나뿐인 이 응답에서 `rerank_score`가 `1.0`에 한참 못 미치는 `0.739`인 것도 정상입니다 — 이 값은 교정되지 않았고 질의마다 척도가 달라, **같은 질의 안에서 다른 결과와 비교할 때만** 뜻이 있습니다.

**검색은 답변을 만들지 않습니다.** 응답에 답변 필드도 거절 문구도 없습니다 — "이 근거로 답할 수 있는가"는 본문을 읽어야 하는 판정이라 [답변 생성](#답변-api-sse)의 몫이고, 검색이 하는 판정은 "이 청크를 다음 단계에 보여줄 가치가 있는가"(각 retriever의 하한)까지입니다. 그래서 이 엔드포인트는 `/qa`가 생긴 뒤에도 남습니다. **답이 이상할 때 원인이 검색인지 생성인지 가르는 관측 지점**이 이것입니다 — 근거가 애초에 안 잡혔는지, 잡혔는데 답이 틀렸는지가 여기서 갈립니다.

결과 하나가 스스로 출처를 말합니다. 인용 한 줄을 만들려고 문서를 다시 조회할 필요가 없습니다.

| 필드 | 의미 |
|---|---|
| `score` | **융합 점수**. `0`보다 크고 `1` 이하이며 클수록 상위입니다. **유사도가 아닙니다** — 활성 retriever들이 이 청크를 얼마나 나란히 상위로 꼽았는가를 뜻하지, 질의와 얼마나 가까운지를 뜻하지 않습니다. 여기에 관련성 하한을 걸면 안 됩니다 |
| `rerank_score` | **크로스인코더 점수**. `0`보다 크고 `1`보다 작으며 클수록 상위입니다. 리랭킹되지 않은 결과에서는 `null`입니다. **같은 질의 안에서만 비교할 수 있습니다** — 질의마다 척도가 달라 서로 다른 질문의 값을 비교하거나 고정 임계값을 걸 수 없습니다 |
| `contributions` | 이 청크를 올린 retriever별 `retriever`·`rank`(1이 최상위)·`native_score`. **항상 한 건 이상**이고, 못 찾은 retriever는 여기 없습니다. `native_score`는 retriever마다 척도가 달라 서로 비교하거나 합산해서는 안 됩니다 |
| `char_start` · `char_end` | 원문 문자 오프셋 구간. `text`는 언제나 추출된 원문의 그 구간과 같습니다 |
| `page` | PDF에서 온 청크만 채워집니다. 이때 오프셋은 **그 쪽 안의** 위치입니다 |
| `revision` · `chunk_index` | 근거가 어느 세대 문서의 몇 번째 청크인지 |

응답 최상위의 `retrievers`는 **이번 검색에 실제로 기여한 retriever의 이름**입니다. 설정에서 빠졌거나 이번 요청에서 실패한 것은 여기 나타나지 않습니다 — 설정 오타 하나로 어휘 색인이 빠진 배포와 정상 배포를 구별하는 유일한 신호가 이 목록입니다. 둘 다 `200`을 내고 둘 다 그럴듯한 근거를 돌려주기 때문입니다.

`ordered_by`와 `reranker`가 같은 목적으로 한 쌍 더 있습니다.

| 필드 | 의미 |
|---|---|
| `ordered_by` | `results`의 순서를 정한 신호. `"rerank"`(크로스인코더 점수) 또는 `"fusion"`(융합 점수). **항상 있습니다** |
| `reranker` | 이번 검색에서 실제로 순서를 정한 리랭커의 모델 이름. 꺼져 있거나, 실패했거나, 제한 시간을 넘겨 축소됐으면 `null` |

**리랭커가 죽어도 검색은 `200`입니다.** 융합 순서 그대로 돌아오고 `ordered_by`가 `"fusion"`이 됩니다 — retriever가 죽으면 근거가 사라지지만 리랭커가 죽으면 사라지는 것은 순서의 질뿐이라, 답할 수 있는 질문을 답하지 못하게 만들지 않습니다. 그래서 이 두 필드가 없으면 **축소된 배포와 정상 배포를 구별할 방법이 없습니다.**

검색 대상은 **지금 유효한 청크뿐**입니다. 이전 리비전·이전 색인 구성·삭제된 문서·[`stale`](#index_status--그-문서가-지금-검색-가능한가) 문서의 청크는 저장소에 남아 있더라도 결과에 나타나지 않습니다. 하한에 걸려 결과가 비는 것은 오류가 아니라 `200`과 빈 목록입니다.

거절되는 경우:

| 상황 | 상태 | 코드 |
|---|---:|---|
| 질의가 비었거나 공백뿐 | 422 | `empty_query` |
| 질의 길이 상한 초과 (문자 수 또는 토큰 수) | 422 | `query_too_long` (두 상한 동봉) |
| `top_k`가 `1` 미만 | 422 | `validation_error` (요청 스키마가 판정) |
| `top_k`가 설정 상한 초과 | 422 | `invalid_top_k` (적용된 상한 동봉) |
| **필수** retriever의 저장소 접근 실패 | 503 | `storage_unavailable` |
| 활성 retriever **전부**의 실패 (전부 선택이더라도) | 503 | `storage_unavailable` |

거절된 질의는 **임베딩 계산도 저장소 접근도 유발하지 않습니다.** 저장소 장애를 빈 결과로 위장하지도 않습니다 — 뭉개면 벡터 스토어가 죽은 동안 서비스가 "근거를 찾지 못했습니다"라고 답하고 아무도 장애를 눈치채지 못합니다.

**선택** retriever 하나가 실패하면 나머지로 융합을 마치고 `200`을 냅니다. 어휘 색인이 죽었다고 검색 전체를 세우면 retriever를 늘릴수록 가용성이 떨어지기 때문입니다. 대신 실패는 두 곳에 드러납니다 — 응답의 `retrievers`에서 그 이름이 빠지고, 경고 로그가 남습니다. 실패한 목록은 **빈 목록으로도 융합에 넘기지 않습니다**: "하한이 걸러 비운 목록"은 판정이라 점수 척도에 남아야 하고, 실패한 retriever는 판정을 내린 적이 없기 때문입니다.

**질의 길이 제한은 두 겹이고 목적이 다릅니다.**

| 상한 | 무엇을 막는가 | 값 |
|---|---|---|
| 문자 수 | 임의 길이 입력이 토크나이저에 들어가는 것 | 설정 `APP_RETRIEVAL_MAX_QUERY_CHARS` (기본 1000) |
| 토큰 수 | **조용한 절단** — 잘린 뒷부분이 검색에 반영되지 않는데 사용자는 전부 반영됐다고 믿는 실패 | **임베딩 모델이 선언한 입력 창**(현재 512). 설정 항목이 아닙니다 |

토큰 상한을 설정으로 두지 않는 이유는 손으로 적을 수 있으면 실제 모델과 어긋난 값을 넣을 수 있고, 그 순간 가드가 보증하려던 것이 사라지기 때문입니다. 문자 수 하나로 둘 다 막을 수는 없습니다 — 같은 글자 수라도 문자 종류에 따라 토큰 수가 아홉 배 가까이 달라져, 절단을 막을 만큼 낮춘 문자 상한(약 101자)은 평범한 질문까지 거부합니다. 토큰은 실제로 인코딩되는 문자열(역할 접두사·특수 토큰 포함) 기준으로 셉니다.

검색 로그도 한 줄입니다. **질의 문자열과 청크 본문은 싣지 않습니다.**

```json
{"level":"INFO","logger":"app.api.routes.search","message":"검색 요청을 처리했습니다","request_id":"9917baf0630a...","top_k":3,"result_count":1,"top_fusion_score":1.0,"contributing_retrievers":["dense","lexical"],"ordered_by":"rerank","reranker":"BAAI/bge-reranker-v2-m3","reranked_candidates":1,"target_documents":2}
```

`target_documents`가 있어야 빈 결과의 이유가 갈립니다 — `0`이면 올린 문서가 없거나 전부 `stale`인 것이고, 대상이 있는데 결과가 `0`이면 각 retriever의 하한에 걸린 것입니다. `contributing_retrievers`는 하이브리드가 실제로 돌았는지를 로그만으로 확인하는 자리입니다.

### 답변 API (SSE)

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| `POST` | `/qa` | 근거를 찾아 먼저 보내고, 그 근거만으로 답변을 생성해 조각 단위로 흘려보냄 |

**스트리밍이 유일한 표면입니다.** 전체 답변만 필요하면 마지막 `done` 이벤트 하나만 읽으면 됩니다 — 답변 전문·인용·종료 사유가 거기 다 들어 있습니다.

```bash
curl -N -X POST http://127.0.0.1:8000/qa \
  -H 'content-type: application/json' \
  -d '{"question":"교육비는 얼마까지 지원되나요?","top_k":3}'
```

`-N`이 없으면 curl이 응답을 버퍼링해 스트리밍이 눈에 보이지 않습니다. 아래는 **실제로 받은 출력**입니다(근거 본문은 길어서 줄였습니다).

```
event: sources
data: {"results":[{"document_id":"0de0c0a1-311b-5fc7-a4f7-763b45bc2444","filename":"company-policy.txt",
       "format":"txt","revision":"5baf958963e1...","chunk_index":0,"text":"[사내 복리후생 안내]\n\n1. 교육비 지원\n...",
       "score":1.0,"rerank_score":0.7388216138591319,"char_start":0,"char_end":527,"page":null,
       "contributions":[{"retriever":"dense","rank":1,"native_score":0.86091983},
                        {"retriever":"lexical","rank":1,"native_score":0.6117184199523331}]}],
       "count":1,"top_k":3,"target_documents":2,
       "ordered_by":"rerank","reranker":"BAAI/bge-reranker-v2-m3"}

event: answer
data: {"text":"임"}

event: answer
data: {"text":"직"}

... (answer 이벤트 26회)

event: done
data: {"finish_reason":"stop","answer":"임직원은 연간 최대 200만 원까지 직무 관련 교육비를 지원받을 수 있습니다.[1]",
       "citations":[{"marker":1,"document_id":"0de0c0a1-311b-5fc7-a4f7-763b45bc2444","filename":"company-policy.txt",
       "format":"txt","revision":"2ccbdc608106...","chunk_index":0,"score":0.86091971,
       "char_start":0,"char_end":527,"page":null}],"dropped_markers":0,"elapsed_ms":7385}
```

**조각이 26개인 것은 서버가 나눈 것이 아닙니다.** CLI가 토큰 단위로 델타를 흘리고, 서버는 그것을 쪼개지도 합치지도 않고 그대로 전달합니다.

| 이벤트 | 횟수 | 내용 |
|---|---|---|
| `sources` | 정확히 1회, **항상 먼저** | `results`(`/search`와 같은 모양)·`count`·`top_k`·`target_documents`·`ordered_by`·`reranker` |
| `answer` | 0회 이상 | `text` — 답변 조각. 이어 붙이면 `done`의 `answer`와 같습니다 |
| `done` | 0 또는 1회, **마지막** | `finish_reason`·`answer`·`citations`·`dropped_markers`·`elapsed_ms` |
| `error` | 0 또는 1회, **마지막** | `code`·`message`·`attempts`·`reason` |

**스트림은 `done` 또는 `error` 정확히 하나로 닫힙니다.** 둘 다 나오거나 종료 이벤트 없이 끊기는 경우는 없습니다. 응답이 길어져 조각 사이가 벌어지면 `: keep-alive` 주석이 15초마다 나가는데, **주석은 이벤트가 아니라서** `EventSource`나 표준 SSE 파서에는 나타나지 않습니다.

#### `finish_reason` — 왜 끝났는가

| 값 | 언제 | `answer` | `citations` |
|---|---|---|---|
| `stop` | 근거로 답했음 | 생성기가 쓴 답변 | 검증을 통과한 마커 |
| `no_evidence` | 검색 결과가 0건이라 **생성기를 아예 부르지 않음** | **빈 문자열** | 비어 있음 |
| `insufficient_evidence` | 근거는 있으나 생성기가 "답할 수 없다"고 판정 | 생성기가 쓴 사유 | 비어 있음 |

거절이 두 갈래인 이유는 사용자가 할 일이 다르기 때문입니다 — 앞은 문서를 올려야 하고, 뒤는 질문을 바꿔야 합니다.

**`no_evidence`일 때 서버는 문구를 만들지 않습니다.** `answer`는 빈 문자열이고 `answer` 이벤트도 나가지 않습니다. 화면에 무엇을 띄울지는 소비자가 정합니다 — **답변 문자열을 만드는 곳은 생성기뿐**이라는 규칙에 예외를 두지 않기 위해서입니다.

```
event: sources
data: {"results":[],"count":0,"top_k":3,"target_documents":2,"ordered_by":"fusion","reranker":null}

event: done
data: {"finish_reason":"no_evidence","answer":"","citations":[],"dropped_markers":0,"elapsed_ms":67}
```

문서가 둘 있는데 근거가 0건인 것은 오류가 아닙니다 — 질문("태양계에서 가장 큰 행성은?")이 어느 문서와도 가깝지 않아 [유사도 하한](#설정)에 전부 걸린 것입니다. 그 판정에 LLM이 필요하지 않으므로 **호출도 하지 않고 67밀리초에 끝납니다.**

`reranker`가 `null`인 것도 같은 이유입니다. 리랭커는 켜져 있지만 **재정렬할 후보가 하나도 없어 모델이 호출되지 않았고**, 돌지 않은 리랭커를 응답이 자기 공으로 적지 않습니다.

근거는 잡혔는데 질문을 뒷받침하지 않으면 **모델이 그렇게 판정하고 사유를 직접 씁니다.** 아래도 실제 출력입니다.

```
event: done
data: {"finish_reason":"insufficient_evidence","answer":"제공된 근거에는 서울의 오늘 날씨 정보가 없습니다.",
       "citations":[],"dropped_markers":0,"elapsed_ms":4983}
```

#### 캐시 — 이 답변이 어디에서 왔는가

`done` 이벤트가 세 값을 함께 싣습니다. 같은 질문을 두 번 보내 `cache_hit`이 `false` → `true`로 바뀌는 것이 캐시가 실제로 도는지 확인하는 가장 짧은 방법입니다.

```bash
for i in 1 2; do
  curl -sN localhost:8000/qa -H 'content-type: application/json' \
    -d '{"question":"교육비는 얼마까지 지원되나요?"}' \
    | grep -A1 '^event: done' | tail -1 | jq -c '{cache_hit,cache_layer,cache_similarity,elapsed_ms}'
done
```

```json
{"cache_hit":false,"cache_layer":null,"cache_similarity":null,"elapsed_ms":7385}
{"cache_hit":true,"cache_layer":"exact","cache_similarity":null,"elapsed_ms":12}
```

| 값 | 뜻 |
|---|---|
| `cache_hit` | 이 답변이 캐시에서 왔는가. **캐시 저장소가 죽어 미스로 강등된 요청도 `false`입니다** |
| `cache_layer` | `exact`(정규화한 질문이 같음) / `semantic`(뜻이 충분히 가까움) / `null`(미스) |
| `cache_similarity` | 유사 매치 판정에 쓰인 코사인 유사도. 정확 매치와 미스에서는 `null` |

`elapsed_ms`는 **이번 요청**의 소요 시간이지 캐시된 복사가 아닙니다. 히트는 검색과 생성을 모두 건너뛰므로 그 차이가 그대로 이 값에 드러납니다.

**히트도 미스와 같은 이벤트 시퀀스로 나갑니다** — `sources`에는 원래 생성 시점의 근거가 그대로 실리고, 본문은 `answer` 조각 **하나**로 재생됩니다. 조각 경계를 저장하지 않는 것은 히트에 "조각이 도착하는 사건"이 없기 때문입니다. 그것을 흉내 내면 캐시가 아낀 시간을 도로 쓰면서 진행하는 척만 하게 됩니다.

#### 인용 — 답변이 무엇을 근거로 했는가

답변 본문의 `[1]` 마커가 `sources`의 몇 번째 결과인지를 가리킵니다. **마커는 본문에서 지우지 않습니다** — 지우면 흘러간 문장과 `done.answer`가 달라집니다.

- 등장 순서대로, 중복은 한 번만 실립니다.
- **근거 목록에 없는 번호는 버립니다.** 없는 근거를 가리키는 인용은 환각이고, 번호가 붙어 있어 가장 그럴듯해 보이는 형태입니다. 버린 개수가 `dropped_markers`로 나갑니다.
- 인용 항목의 값은 같은 스트림의 `sources`에 실린 값과 같습니다. 인용이 새 사실을 만들지 않습니다.

마커가 하나도 없는 답변도 유효합니다(`finish_reason: "stop"` + 빈 `citations`).

#### 거절되는 경우

**질의 검증과 검색은 스트림을 열기 전에 끝납니다.** 상태 코드는 첫 바이트와 함께 확정되므로, 여기서 실패하면 SSE가 아니라 평범한 JSON 오류 응답이 돌아옵니다.

| 상황 | 상태 | 코드 | 콘텐츠 타입 |
|---|---:|---|---|
| 질문이 비었거나 공백뿐 | 422 | `empty_query` | `application/json` |
| 질문 길이 상한 초과 | 422 | `query_too_long` | `application/json` |
| `top_k`가 `1` 미만 / 상한 초과 | 422 | `validation_error` / `invalid_top_k` | `application/json` |
| 벡터 스토어 접근 실패 | 503 | `storage_unavailable` | `application/json` |
| 생성 시도 소진 | 200 | `llm_unavailable` (**`error` 이벤트**) | `text/event-stream` |
| LLM 인증 없음·만료 | 200 | `llm_unauthenticated` (**`error` 이벤트**) | `text/event-stream` |

`/search`와 **같은 코드·같은 형식**입니다. 같은 질의가 경로에 따라 다른 답을 받지 않습니다.

#### 인증이 없으면 `/qa`만 실패합니다

`.secrets/codex/auth.json`이 없거나 만료된 상태에서도 **서비스는 정상 기동하고 `/health`·`/documents`·`/search`가 전부 동작합니다.** 인증 부재는 질문한 시점에만, 그 사실 그대로 드러납니다.

```
event: sources
data: {...근거는 정상적으로 나갑니다...}

event: error
data: {"code":"llm_unauthenticated","message":"답변 생성기가 인증되지 않았습니다","attempts":1,"reason":"unauthenticated"}
```

`attempts`가 `1`인 것은 **재시도하지 않기 때문**입니다 — 백오프를 몇 번 돌아도 자격증명이 생기지 않습니다. 시도 소진(`llm_unavailable`)과 코드를 나눈 이유가 이것입니다: 앞은 [자격증명 주입 경로](#llm-자격증명-동기화)를 확인할 일이고, 뒤는 재시도나 상한 조정입니다.

#### 로그

**질문·근거 본문·답변 본문은 남기지 않습니다.** 질문 자체가 개인정보일 수 있고 근거는 문서 내용 그 자체이며, 답변은 그 둘을 합친 것이라 가장 민감합니다.

```json
{"level":"INFO","logger":"app.services.qa","message":"답변 생성 요청을 처리했습니다","request_id":"3f83da8a...","source_count":1,"target_documents":2,"finish_reason":"stop","citation_count":1,"dropped_markers":0,"attempts":1,"elapsed_ms":7385}
```

`dropped_markers`가 늘어나는 것이 프롬프트 열화의 가장 이른 신호이고, `attempts`가 생성 불안정을 같은 방식으로 드러냅니다.

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

## 데모 UI (선택)

`/qa`가 실제로 **조각 단위로** 답하는지, 답변의 `[1]`이 어느 문서의 어느 대목을 가리키는지를
브라우저에서 확인하는 화면입니다. API 서버와 별개로 도는 **선택 절차**이고, 띄우지 않아도
`docker compose up`과 테스트는 그대로 동작합니다.

```bash
cd demo-ui && npm install && npm run dev
```

Node.js 20 이상이 필요합니다. 뜬 뒤 **`http://localhost:5173`** 으로 접속하세요
(`127.0.0.1`이 아닙니다 — Vite가 `localhost` 이름으로 바인딩합니다).

API 서버가 `http://127.0.0.1:8000`에 떠 있어야 합니다. 다른 곳이면 `VITE_API_TARGET`으로 바꿉니다.

| 하려는 것 | 명령 |
|---|---|
| 기동 | `cd demo-ui && npm run dev` |
| 다른 API 주소로 | `VITE_API_TARGET=http://192.168.0.10:8000 npm run dev` |
| 타입 검사 + 빌드 | `cd demo-ui && npm run build` |

**서버는 CORS를 열지 않습니다.** 대신 Vite dev 서버가 `/api`를 API로 프록시합니다 — 브라우저가
5173 한 출처만 보게 해서 프리플라이트 자체를 없앴습니다. 데모 하나 때문에 API의 미들웨어 체인을
영구히 넓히지 않으려는 선택입니다. 그래서 `npm run build`의 산출물을 정적으로 열면 API 호출이
실패합니다. **데모는 dev 서버로 도는 것이 전제입니다.**

화면은 왼쪽 문서 패널(업로드·목록·삭제)과 오른쪽 Q&A 콘솔(질문·근거·스트리밍 답변·인용)로
나뉩니다. 시각·상호작용 규칙과 QA 체크리스트는 [`demo-ui/DESIGN.md`](./demo-ui/DESIGN.md)에 있습니다.

> 자격증명이 없는 환경에서는 **문서 수집·근거 검색·헬스가 전부 정상이고 답변만 실패합니다.**
> 화면이 그 사실을 다른 실패와 구분해 표시합니다 — 고장이 아니라 [정상 동작](#인증이-없으면-qa만-실패합니다)입니다.

> 검색이 계속 0건이면 `./data/chroma`와 `./data/registry`가 어긋난 상태일 수 있습니다.
> 문서를 지웠다가 다시 올리면 복구됩니다.

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

> **알려진 환경 의존 실패 1건.** `test_concurrency_api.py::test_different_documents_are_not_serialized`가 **Docker Desktop(Windows/macOS)에서 실패합니다.** 이 테스트는 캐시를 일부러 닿지 않는 호스트로 겨눠 두고 "두 문서 업로드가 직렬화되지 않는가"를 벽시계로 재는데, 리눅스 도커에서는 없는 호스트 이름이 즉시 실패하는 반면 Docker Desktop의 내장 DNS는 상위로 넘겨 **4초**를 기다립니다. 캐시 호출마다 상한 0.2초가 꽉 차고, 업로드 하나에 무효화가 3회라 예산 0.4초를 넘습니다. 리랭킹과도 캐시 로직과도 무관하며 측정 방식의 문제입니다 — 진단과 고치는 방법은 [`CLAUDE.md`](./CLAUDE.md)에 적어 두었습니다.

의존성 상태는 대역을 주입해 결정론적으로 구성합니다. 실제 컨테이너를 죽여가며 상태를 만들면 느리고 불안정하기 때문입니다. **기본값은 임베딩 모델도 대역입니다** — 실제 가중치를 받으면 이 한 줄이 수백 MB 다운로드에 묶입니다. 실제 모델이 필요한 것(차원·역할 접두사·서명 같은 계약, 그리고 아래의 검색 품질)은 가중치가 이미 캐시된 환경에서만 돌고, 없으면 건너뜁니다.

문서 레지스트리(SQLite)는 임시 파일에 실물로 띄워 검증합니다. **벡터 스토어는 별도 서버**라 실물 어댑터 테스트에는 서버가 필요한데, 위 명령이 `depends_on`으로 함께 띄우므로 그 층도 실행됩니다. 대역으로 대체하지 않은 이유는 그 테스트가 확인하려는 것이 "우리 메타데이터·필터·id 규약이 *실제 Chroma*에서 성립하는가"이기 때문입니다 — 대역으로 바꾸면 확인 대상 자체가 사라집니다.

**검색 품질 테스트는 로컬 임베딩 실물을 씁니다.** "기대한 문서의 청크가 1위로 오는가"는 대역으로는 확인할 수 없습니다 — 해시 기반 페이크 벡터에는 의미가 없어 1위가 무엇이든 정상으로 보입니다. 그래서 이 층만 실제 모델을 쓰고, 가중치가 없으면 건너뜁니다. **이미지에는 가중치가 구워져 있으므로** 위 명령에서는 실행됩니다.

즉 한 줄이 전부를 덮습니다.

| 층 | `docker compose run --build --rm test` |
|------|:---:|
| 구조 층 (필터·순서·경계·오류) | ✅ |
| 실물 Chroma 어댑터 | ✅ (`depends_on`이 띄웁니다) |
| 검색 품질 (임베딩 실물) | ✅ (가중치가 이미지에 있습니다) |
| 캐시 의미 (TTL·상한·유사 매치·무효화) | ✅ (인메모리 구현으로 돕니다) |
| **실물 Redis 어댑터** | ❌ — 저장소가 필요해 기본 실행에서 뺍니다 (바로 아래) |
| **실물 CLI (답변 생성)** | ❌ — 구독이 필요해 기본 실행에서 뺍니다 (바로 아래) |

**LLM 구독도 API 키도 필요 없습니다.** 건너뛴 항목이 있으면 실행 결과에 사유와 함께 표시됩니다.

각 테스트 파일이 **어떤 실패를 막으려고** 있는지는 [`tests/README.md`](./tests/README.md)에 파일별로 적어 두었습니다. 커버리지 수치가 아니라 그 답이 평가 기준이라, 주석 규칙이 파일 안에서 밀어낸 설명을 지우지 않고 그 문서로 옮겼습니다.

### 실물 Redis 층

캐시의 **의미**(수명·총량 상한·유사 매치·태그 무효화)는 인메모리 구현으로 기본 실행에서 검증합니다. 평가자의 한 줄이 저장소에 묶이면 안 되기 때문입니다. Redis에서만 존재하는 것들 — TTL이 실제로 걸리는가, 만료된 지문이 순서 인덱스에서 걷히는가, 용량 상한이 페이로드 키까지 지우는가 — 은 `redis` 마커 뒤에 두었습니다.

```bash
docker compose run --build --rm test pytest -m redis
```

위 명령이 `depends_on`으로 Redis를 함께 띄웁니다. 계약 테스트는 **15번 데이터베이스**를 쓰고 시작과 끝에 비웁니다 — 개발 중에 띄워 둔 캐시(0번)를 건드리지 않습니다.

### 실물 CLI 층

답변 생성 테스트는 전부 페이크 생성기와 **저장해 둔 실물 알림 샘플**(`tests/fixtures/codex/`) 위에서 돕니다. 프로세스 인자 형태나 JSON-RPC 스키마가 바뀌는 것은 그 층에서 잡히지 않으므로, 실제로 `codex app-server`를 부르는 테스트를 `llm` 마커 뒤에 따로 두었습니다.

```bash
docker compose run --build --rm test python -m pytest -m llm
```

**자격증명이 필요합니다.** `docker compose up`을 한 번 돌려 `.secrets/codex/auth.json`이 만들어진 뒤에 실행하세요 — 없으면 사유와 함께 건너뜁니다(`2 skipped`). 기본 실행에서는 이 층이 항상 제외됩니다(`1043 passed, 30 deselected` — 제외된 30건은 실물 CLI 2건, 실물 Redis 21건, 골든셋 순위 비교 4건, 골든셋 판정 채점 3건입니다).

### 골든셋 리랭킹 비교

크로스인코더를 켠 구성과 끈 구성을 **같은 저장소 위에서** 비교하는 층입니다. 보험 요약집 PDF 한 건에 대한 진단용 평가셋(`tests/fixtures/golden/`) 44문항을 두 구성으로 돌려 근거 인용문의 순위를 잽니다. CPU에서 30분대라 `slow` 마커 뒤에 두었습니다 — 평가자가 도는 한 줄이 여기 묶이면 안 됩니다.

```bash
docker compose run --build --rm test pytest -m slow -s
```

`-s`를 붙이면 비교표가 그대로 출력됩니다. 이 층이 재는 것은 **검색 순서**입니다 — 답변 품질은 아래 판정 층이 잽니다.

### 골든셋 판정 채점 (LLM-Judge)

순위가 좋아졌다고 답이 좋아진 것은 아닙니다. 그래서 같은 골든셋의 `reference_answer`를 기준으로, 두 구성이 각자의 상위 K로 만든 **답변**을 판정자에게 대조시키는 층을 따로 두었습니다.

채점은 세 층이고 **합산하지 않습니다.**

| 층 | 무엇을 보는가 | 성질 |
|---|---|---|
| ① 검색 게이트 | `evidence.quote`가 상위 K에 왔는가 | 결정적. 통과하지 못하면 **생성 채점을 하지 않습니다** |
| ② 문자열 검사 | `expected_spans` 전부 포함·`must_not_contain` 0건 | 결정적. 예상 밖 표현으로 틀린 답은 놓칩니다 |
| ③ LLM-Judge | 답변이 기준 답변과 같은 말을 하는가 | 비결정적. ②가 놓치는 구멍을 메웁니다 |

①이 있는 이유는 근거가 오지 않은 답변을 생성 실패로 세면 두 지표가 함께 움직여 어느 층이 무너졌는지 알 수 없기 때문입니다(골든셋 README의 규칙입니다).

```bash
docker compose run --build --rm test pytest -m "llm and slow" -s
```

**구독과 실물 가중치가 모두 필요합니다.** `.secrets/codex/auth.json`이 없거나 가중치가 캐시에 없으면 사유와 함께 건너뜁니다. 문항 하나가 구성마다 실물 턴 둘(생성·판정)을 쓰므로 한 바퀴가 47문항 × 2 × 2 = **턴 188번, 실측 41분**입니다.

**이 층은 품질을 단언하지 않습니다.** 판정이 비결정적이라 같은 답변을 다시 물으면 달라질 수 있고, 재지 않은 임계값을 회귀 게이트로 쓰지 않는다는 규칙을 여기에도 적용했습니다. 회귀를 세우는 것은 결정적인 층(순위 지표·`tests/test_retrieval_quality.py`의 회귀 질의)이고, 이 층이 단언하는 것은 하네스 건전성 하나입니다 — **판정불가가 과반이면 그 회차의 통과 수를 읽지 않습니다.**

판정 프롬프트의 설계 의도는 [`PROMPT_DESIGN.md`](./PROMPT_DESIGN.md)에, 결과는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)의 실측표에 있습니다.

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
| `APP_LEXICAL_INDEX_PATH` | 어휘 색인(SQLite FTS5) 경로 | `./data/lexical.sqlite3` |
| `APP_LEXICAL_MIN_TOKEN_RARITY` | 질의 토큰이 "드물다"고 인정받는 하한. 이 값을 넘는 토큰이 하나도 겹치지 않는 청크는 어휘 검색 결과에서 빠집니다 | `0.3` |
| `APP_EMBEDDING_MODEL` | 임베딩 모델 이름 | `intfloat/multilingual-e5-small` |
| `APP_CHUNK_STRATEGY` | 분할 전략 | `recursive` |
| `APP_CHUNK_SIZE` | 청크 크기 상한(문자) | `600` |
| `APP_CHUNK_OVERLAP` | 인접 청크 겹침(문자). `0` 불가 | `100` |
| `APP_EMBEDDING_BATCH_SIZE` | 임베딩·저장 배치 크기 | `64` |
| `APP_MAX_UPLOAD_BYTES` | 업로드 크기 상한 | `20971520` (20 MiB) |
| `APP_INGESTION_CONCURRENCY` | 동시 수집 상한 | `2` |
| `APP_RETRIEVAL_RRF_K` | RRF 상수. 클수록 상위 순위의 우대가 약해집니다 | `60` |
| `APP_RETRIEVAL_TOP_K` | 검색 기본 상위 K. 요청의 `top_k`가 덮어씁니다 | `5` |
| `APP_RETRIEVAL_MAX_TOP_K` | 요청이 지정할 수 있는 `top_k`의 상한. 후보 깊이와 함께 봅니다(아래) | `20` |
| `APP_RETRIEVAL_MIN_SCORE` | **밀집 retriever의** 코사인 유사도 하한. 이 값 미만인 청크는 그 목록에 실리지 않습니다 | `0.82` |
| `APP_RETRIEVAL_MAX_QUERY_CHARS` | 질의 문자 수 상한 | `1000` |
| `APP_RERANKER_ENABLED` | 크로스인코더 리랭킹 사용 여부. 끄면 융합 순서를 그대로 씁니다 | `true` |
| `APP_RERANKER_MODEL` | 리랭커 모델 이름. 아는 모델이 아니면 **기동에 실패합니다** | `BAAI/bge-reranker-v2-m3` |
| `APP_RERANK_CANDIDATES` | 리랭커에게 넘길 융합 상위 후보 수. `APP_RETRIEVAL_MAX_TOP_K` 이상이어야 합니다 | `30` |
| `APP_RERANKER_TIMEOUT_SECONDS` | 리랭킹 한 번의 시간 상한(초). 넘기면 융합 순서로 축소 | `15.0` |
| `APP_QA_LLM_TIMEOUT_SECONDS` | 생성 **한 시도**의 시간 상한(초) | `60.0` |
| `APP_QA_LLM_MAX_ATTEMPTS` | 최대 시도 횟수. `1`이면 재시도하지 않음 | `3` |
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
| `APP_CACHE_SEMANTIC_CANDIDATES` | 유사 매치가 한 요청에서 훑는 후보 수 상한 | `200` |
| `APP_CACHE_OPERATION_TIMEOUT_SECONDS` | 캐시 작업 하나의 시간 상한(초) | `0.2` |
| `APP_CACHE_CIRCUIT_BREAKER_FAILURES` | 연속 실패가 이만큼이면 캐시 호출을 건너뜁니다 | `3` |
| `APP_CACHE_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | 건너뛰는 시간(초). 지나면 자동 재개 | `30.0` |

구현되지 않은 `APP_CHUNK_STRATEGY` 값(현재 구현된 것은 `recursive` 하나입니다)이나 청크 크기 이상의 겹침을 넣으면 **기동에 실패합니다.** 잘못된 색인 구성으로 조용히 뜨는 것보다 낫기 때문입니다.

같은 이유로 **`APP_RETRIEVAL_TOP_K`와 `APP_RETRIEVAL_MAX_TOP_K`는 함께 검증됩니다** — 기본 K가 상한보다 크면 어떤 요청도 통과할 수 없으므로 기동을 막습니다. 두 값이 각각은 멀쩡한데 조합이 성립하지 않는 자리라, 첫 검색 요청이 아니라 기동에서 드러나야 합니다.

**`APP_QA_LLM_MODEL`을 비워 두면 캐시 키에 빈 문자열이 들어갑니다.** 빈 값은 "CLI 기본 모델을 쓴다"는 뜻인데, 그 기본값이 CLI 업그레이드로 바뀌어도 캐시 키는 `""` 그대로입니다. 그러면 **옛 모델이 만든 답변이 새 모델의 답인 척 남습니다.** 모델을 명시하면 이 구멍이 닫히고, 모델을 바꾸는 순간 캐시가 저절로 갈립니다.

**리랭커를 끄는 것이 이 change의 되돌리기입니다.** `APP_RERANKER_ENABLED=false`로 기동하면 파이프라인이 리랭킹 도입 이전과 같아집니다 — 순서가 융합 점수 그대로이고, 응답의 `ordered_by`가 `"fusion"`이 되며, `reranker`와 `rerank_score`가 비어 옵니다. 리랭커 서명이 캐시 키 재료라, 껐다 켜는 순간 캐시는 무효화 호출 없이 저절로 갈립니다.

**첫 빌드가 깁니다.** 리랭커 가중치 2.2GB를 이미지에 굽기 때문이고(임베딩 가중치와 같은 레이어), 실측으로 내려받기에만 2분 반이 걸렸습니다. 그 레이어가 `COPY src`보다 앞이라 **코드를 고쳐 다시 빌드할 때는 다시 받지 않습니다.** 가중치를 굽는 이유는 첫 요청이 다운로드를 기다리지 않게 하기 위해서이고, 오프라인 환경에서도 그대로 돌기 위해서입니다.

**활성 retriever 목록만은 이 표에 없습니다.** 값이 항목 넷을 가진 목록이라 환경변수 한 줄로 적기에 맞지 않아, `config.py`에 두고 아래처럼 바꿉니다.

### retriever 구성

검색 한 건은 **활성 retriever 전부**에게 같은 질의를 보내고, 각자가 돌려준 순위 목록을 [RRF](./ARCHITECTURE.md)로 하나로 합칩니다. 조합·가중치·후보 깊이를 바꾸는 데 **검색 코드를 고칠 필요는 없습니다** — 구성은 [`src/app/config.py`](./src/app/config.py)의 `_default_retrievers()` 한 곳에 있습니다.

```python
def _default_retrievers() -> list[RetrieverSettings]:
    return [
        RetrieverSettings(name="dense", required=True),
        RetrieverSettings(name="lexical", required=False),
    ]
```

바꾸려면 이 목록을 고치고 이미지를 다시 굽습니다. **`--build`가 없으면 직전 이미지의 구성으로 뜹니다.**

```python
# 어휘 retriever만 켜기 — 이 구성에서는 임베딩을 한 번도 계산하지 않습니다
return [RetrieverSettings(name="lexical", required=True)]

# 어휘에 비중을 더 주고 후보를 더 깊이 받기
return [
    RetrieverSettings(name="dense", required=True),
    RetrieverSettings(name="lexical", weight=1.5, candidate_depth=80, required=False),
]
```

```bash
docker compose up -d --build api
```

구성이 실제로 바뀌었는지는 검색 한 번으로 확인됩니다 — 응답의 `retrievers`가 그것입니다. 위의 첫 구성에서는 `"retrievers":["lexical"]`이 오고, 모든 결과의 `contributions`에 `dense`가 없습니다.

| 항목 | 의미 |
|---|---|
| `name` | 등록된 retriever 이름. 현재 `dense`(임베딩 + Chroma)와 `lexical`(SQLite FTS5 BM25) |
| `weight` | 융합에서의 비중. **양수여야 합니다** — `0`이나 음수는 그 목록이 순위에 기여하지 않거나 순서를 뒤집는다는 뜻입니다 |
| `candidate_depth` | 융합 **전에** 그 retriever에게 받아 오는 후보 수(기본 `100`). `APP_RETRIEVAL_MAX_TOP_K` 이상이어야 합니다 |
| `required` | `true`면 이 retriever의 실패가 `503`, `false`면 나머지로 진행하고 `200` |

**네 경우가 기동을 막습니다**: 목록이 비었을 때, 등록되지 않은 이름을 적었을 때(실패 사유에 그 이름이 나옵니다), 가중치가 양수가 아닐 때, `candidate_depth`가 `APP_RETRIEVAL_MAX_TOP_K`보다 작을 때. 마지막 것의 비교 대상이 K의 **기본값**이 아니라 **상한**인 이유는 요청이 `top_k`를 상한까지 올릴 수 있기 때문입니다 — 기본값으로만 검증하면 기동을 통과한 구성에서 큰 `top_k` 요청 하나가 곧바로 깊이를 넘어섭니다.

**깊이와 K 상한 사이의 여유가 곧 융합의 여지입니다.** 한 retriever가 K칸을 혼자 채워 오면 다른 쪽의 발견이 들어올 자리가 없기 때문입니다. 기동 검증이 요구하는 것은 `깊이 >= 상한`이라는 **하한**뿐이라, 둘을 같게 두어도(예: 50/50) 기동은 통과하지만 `top_k=50` 요청에서는 융합할 재료가 남지 않습니다. 그래서 기본값을 **깊이 100 · 상한 20 — 다섯 배**로 잡았습니다. 상한을 응답 크기가 아니라 이 비율로 정한 이유는, 청크 50개는 어차피 프롬프트 예산상 문맥에 들어가지 못해 상한 50이 실효가 없기 때문입니다.

이 비율을 규칙으로 못 박지 않은 것은 의도한 것입니다. 배수를 기동 검증에 넣으면 아직 큰 코퍼스로 재 보지 않은 값이 제약이 됩니다 — 현재 `sample-docs`는 문서 2건·청크 3개뿐이라 깊이의 비용도 효과도 이 표본에서는 관측되지 않습니다.

`APP_RETRIEVAL_MIN_SCORE`의 기본값 `0.82`는 감으로 적은 값이 아니라 **계측값**입니다 — `sample-docs`의 두 문서로 관련 질의 4개와 무관 질의 3개의 점수 분포를 실제로 재서 그 사이에 놓았습니다(관련 1위 최솟값 0.8511 / 무관 1위 최댓값 0.8134). 표본이 문서 2개·질의 7개뿐이라는 한계와 계측 절차는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)에 적어 두었습니다. 임베딩 모델을 바꾸면 점수 분포가 통째로 이동하므로 이 값도 다시 재야 합니다.

`APP_LEXICAL_MIN_TOKEN_RARITY`의 기본값 `0.3`도 **계측값**입니다 — `sample-docs`의 두 문서로 하한 후보 넷을 재서, 네 회귀 질의가 모두 살아남는 가장 높은 값을 골랐습니다(`0.4`부터 코드리뷰 질의가 빈 목록이 됩니다). 실측표와 표본 크기의 한계는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)에 있습니다.

`APP_EMBEDDING_MODEL`·`APP_CHUNK_STRATEGY`·`APP_CHUNK_SIZE`·`APP_CHUNK_OVERLAP`과 **어휘 색인의 토큰화 구성**은 `index_signature`의 재료입니다. 바꾸면 기존 문서가 다음 기동에서 [`stale`](#index_status--그-문서가-지금-검색-가능한가)이 되어 재업로드가 필요합니다. 나머지 값(배치 크기·업로드 상한·동시성, 그리고 검색 시점에만 쓰이는 하한들)은 저장된 벡터와 어휘 색인의 내용을 바꾸지 않으므로 서명에 영향을 주지 않습니다 — 성능 튜닝과 하한 조정이 전면 재색인을 유발하지 않습니다.

> **이 버전으로 올리면 기존 문서를 다시 업로드해야 합니다.** 어휘 색인이 추가되면서 토큰화 구성이 `index_signature`에 들어갔고, 그래서 이전 버전에서 수집한 모든 문서의 서명이 달라집니다. 첫 기동에서 기동 정리가 이를 발견해 두 색인의 청크를 지우고 문서를 `stale`로 표시합니다 — **기동은 실패하지 않습니다.** `GET /documents`에서 `index_status`가 `stale`인 문서를 같은 파일로 다시 올리면 `reindexed`로 복구됩니다.

`.env` 파일도 읽습니다. 환경을 직접 조회하는 곳은 `src/app/config.py` 하나뿐이며, 다른 모듈의 직접 조회는 린트 규칙으로 막혀 있습니다.

## 기술 선택

| 계층 | 선택 | 이유 |
|------|------|------|
| 언어 / 프레임워크 | Python 3.11+ / FastAPI | 과제 고정 조건. SSE 스트리밍과 async 파이프라인이 네이티브 |
| LLM SDK | **Codex SDK** (`@openai/codex`) | API 키 없이 구독으로 동작. `claude-code-sdk`에서 갈아탔습니다 — 자격증명이 세 OS 모두 **평문 파일**이라 컨테이너가 볼륨으로 읽을 수 있고, 그래야 `docker compose up` 한 줄에 인증이 들어옵니다 ([경위](./ARCHITECTURE.md#llm-sdk-통합-방식)) |
| 임베딩 | sentence-transformers (`intfloat/multilingual-e5-small`) | 로컬 오픈소스 모델이라 테스트가 LLM 구독 없이 실행됨. 다국어·512 토큰 창 |
| 문서 레지스트리 | SQLite (표준 라이브러리) | "지금 유효한 리비전이 무엇인가"의 단일 답. 컨테이너를 늘리지 않고 벡터 스토어와 같은 볼륨에 놓임 |
| 벡터 DB | Chroma (**서버 모드**, 별도 컨테이너) | 메타데이터 필터와 문서 단위 삭제를 지원해 리비전 교체·캐시 무효화 연동이 가능. 저장소를 앱 프로세스 밖으로 빼 API 재배포와 수명이 분리됨 |
| 어휘 색인 | SQLite **FTS5** (표준 라이브러리) | `bm25()` 순위 함수가 내장이라 컨테이너가 늘지 않음. Elasticsearch는 이 규모에 JVM 컨테이너가 과잉이고, 인메모리 BM25는 영속성이 없어 기동마다 전 청크를 재구축해야 함 ([근거](./ARCHITECTURE.md#어휘-색인)) |
| PDF 파싱 | PyMuPDF | 쪽 단위 텍스트 추출이 정확하고 빠름. **AGPL-3.0**이므로 배포 형태를 바꿀 때 재검토가 필요 |
| 캐시 DB | Redis | 정확 매치는 키 조회, 유사 질문은 질문 임베딩 유사도로 판정. TTL·태그 기반 무효화가 자연스러움 |
| 린터 | ruff | 포매팅과 린팅을 한 도구로 통일. 레이어 경계도 린트 규칙으로 강제 |

> Codex CLI는 `POST /qa`가 실제로 호출합니다 — `codex app-server`(stdio 위의 JSON-RPC) 세션을 풀에 두고 `item/agentMessage/delta` 알림을 그대로 SSE `answer` 이벤트로 흘립니다. `codex exec`가 아닌 이유는 실측입니다(`exec`에는 토큰 델타가 없어 4,808자 답변도 한 이벤트로 옵니다). 근거는 [`ARCHITECTURE.md`](./ARCHITECTURE.md#llm-sdk-통합-방식)에 있습니다.
>
> **Redis는 현재도 헬스 점검에만 쓰입니다** — 캐싱은 다음 change입니다. sentence-transformers·Chroma·SQLite·PyMuPDF는 수집·검색 경로에서 실제로 쓰입니다.

설계 근거는 [`ARCHITECTURE.md`](./ARCHITECTURE.md)에 있습니다.

## 회고

`git commit`마다 Retrobot이 작업 로그를 분석해 KPT 회고를 [`retros/`](./retros/)에 생성합니다. 활성화:

```bash
git config core.hooksPath .githooks
```
