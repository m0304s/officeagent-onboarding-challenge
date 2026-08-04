## 1. 융합 코어

- [x] 1.1 `core/fusion.py` 에 `FusionInput`(이름·가중치·항목 목록)과 `FusedItem`(항목·융합 점수·기여 내역) 값 객체를 정의한다. 표준 라이브러리만 쓴다
- [x] 1.2 RRF 계산을 구현한다 — 원점수 `Σ w/(RRF_K+rank)` 를 **전달된 입력 목록 전체**의 이론 최댓값 `Σ w/(RRF_K+1)` 으로 나눠 `(0, 1]` 로 정규화한다. 두 합은 **목록별 항을 이름표 정렬 순서로** 누적한다 — `(Σw)/(RRF_K+1)` 로 접으면 만장일치 1위가 `1.0` 에서 1 ulp 어긋난다. 빈 목록의 가중치는 분모에 남고, 전달되지 않은 목록은 분모에 없다. 결과에 싣는 것은 정규화된 값이다
- [x] 1.3 타이브레이크를 `(-score, document_id, chunk_index)` 전순서로 구현한다. 목록 전달 순서와 딕셔너리 순회 순서가 결과에 닿지 않게 한다
- [x] 1.4 한 목록 안의 중복은 가장 앞선 순위 하나만 세고, 목록이 0개·1개인 경우와 상위 K 절단을 처리한다. 목록 길이는 제약하지 않는다 — K보다 짧은 목록도 정상 입력이다
- [x] 1.5 `tests/test_fusion.py` — `rank-fusion` 스펙의 시나리오를 덮는다: 양쪽 등장 항목의 상승, 점수 크기 무관, 가중치 효과, 반복 융합 동일성, 전달 순서 무관, 동점 정렬, 1위 만장일치 = `1.0`(가중치가 서로 다를 때도 **정확히** `1.0`), 전달 순서가 달라도 점수가 마지막 자리까지 같은 것, 빈 목록이 분모에 남아 `0.5`, 목록을 빼면 `1.0`, 목록 1개일 때 순서 보존, 중복 1회 계산, K 절단, K보다 짧은 목록

## 2. 어휘 토큰화

- [x] 2.1 `core/lexical.py` 에 토크나이저를 구현한다 — NFKC + 소문자화, 한글/라틴·숫자 경계 분할, 혼합 토큰의 원형·조각 동시 산출
- [x] 2.2 조사·어미 접미 목록을 두고 최장 일치로 한 번 벗겨 **원 토큰과 어근을 함께** 낸다. 어근이 2글자 미만이면 벗기지 않는다
- [x] 2.3 `TOKENIZER_VERSION` 상수와 토큰화 구성 서명 재료를 노출한다
- [x] 2.4 `tests/test_lexical_tokenizer.py` — 조사 붙은 질의가 어근에 닿는 것, `P1`·`v2`·`200만원` 이 쪼개지지 않는 것, 대소문자·전각 흡수, 같은 입력의 결정성, 구성이 다르면 서명이 다른 것

## 3. 어휘 색인 어댑터

- [x] 3.1 `adapters/protocols.py` 에 `LexicalIndex` 프로토콜을 정의한다 — `add_chunks` / `delete_document` / `count_chunks` / `list_stored_versions` / `search`. 벡터 스토어와 **같은 축**(삼중항)을 쓴다
- [x] 3.2 `adapters/lexical/sqlite.py` 에 FTS5 구현을 만든다. 삼중항·`chunk_index` 는 `UNINDEXED` 컬럼, 본문은 토큰 문자열 컬럼. 모든 연산을 `asyncio.to_thread` 로 내보내고 실패를 도메인 예외로 바꾼다
- [x] 3.3 `bm25()` 순위와 대상 삼중항 `WHERE` 필터로 `search` 를 구현한다. **`bm25()` 는 음수를 돌려주므로** 정렬은 `ORDER BY bm25(...) ASC`, 노출 점수는 `-bm25(...)` 다 — 원값을 그대로 싣거나 `DESC` 를 걸면 순위가 뒤집힌다. 대상 목록이 비면 저장소를 건드리지 않고 빈 목록을 낸다
- [x] 3.4 `fts5vocab` 로 `df` 를 읽어 변별력 하한을 적용한다. 기준은 설정 항목(기본 `0.3`). 판정은 커버리지 비율이 아니라 **토큰별 희소도**(`idf(t) / idf(df=0)`)의 최댓값이다 — 비율은 토큰 하나짜리 질의를 걸러내지 못하고, 색인에 없는 표층형이 분모를 부풀려 정상 질의를 죽인다 (design 결정 3 의 실측)
- [x] 3.5 기동 시 FTS5 가용성을 확인하고, 없으면 기동을 세우지 않고 색인을 실패 상태로 둔다
- [x] 3.6 `tests/test_lexical_index.py` — `lexical-index` 스펙 시나리오: 등록·검색, 대상 목록 필터, 빈 대상, **모든 점수가 `0` 이상이고 관련도 높은 청크가 앞에 오는 것**(부호 변환 회귀), 희소성·빈도 포화·길이 정규화, 조사/식별자/대소문자, 변별력 가드(흔한 토큰만·`"오늘 서울 날씨 어때?"`·드문 토큰 1개), 문서·세대 단위 제거, 삼중항 열거, 재등록 중복 없음, 결정성, 접근 실패가 빈 결과로 위장되지 않음
- [x] 3.7 `ARCHITECTURE.md` 에 어휘 색인 저장소 선택 근거(FTS5 vs Elasticsearch vs 인메모리)와 토큰화 규약을 적는다

## 4. 색인 서명과 수집 통합

- [x] 4.1 `derive_index_signature` 재료에 토큰화 구성을 더한다. 검색 시점 설정(가중치·깊이·하한·융합 상수)은 **넣지 않는다**
- [x] 4.2 `services/ingestion.py` 의 쓰기 순서에 어휘 색인을 끼운다 — 벡터 쓰기 → 어휘 쓰기 → 레지스트리 커밋 → 이전 세대 정리(양쪽)
- [x] 4.3 되돌리기가 양쪽을 모두 지우게 한다. 어느 한쪽 쓰기가 실패해도 응답 시점에 양쪽에 그 리비전의 청크가 0개여야 한다
- [x] 4.4 삭제와 기동 정리(잔여 청크 제거·`stale` 표시)에 어휘 색인을 포함한다
- [x] 4.5 `main.py` 배선에 어휘 색인을 추가한다. 경로는 설정 항목(`./data/lexical.sqlite3`)
- [x] 4.6 `tests/test_ingestion_pipeline.py` 확장 — `document-ingestion` 델타 시나리오: 양쪽 저장, 교체·재색인·삭제가 양쪽에 미침, 어휘 쓰기 실패 시 벡터 되돌림(그 반대도), 실패한 교체가 이전 리비전을 양쪽에 보존, 기동 정리와 `stale` 이 양쪽에 미침
- [x] 4.7 `tests/test_documents.py` 확장 — 토큰화 구성이 바뀌면 서명이 달라지고, 검색 시점 설정이 바뀌어도 서명이 그대로인 것
- [x] 4.8 `README.md` 에 "이 버전으로 올리면 기존 문서를 다시 업로드해야 한다"(서명 변경에 따른 `stale`)를 적는다

## 5. Retriever 계약과 구현

- [x] 5.1 `core/retrieval.py` 에 `RetrievedChunk`(청크 + `native_score`)와 `Contribution`(retriever 이름·순위·원래 점수)을 정의하고, `ScoredChunk` 에 기여 내역을 더한다. `score` 불변식을 `0 < score <= 1` 로 바꾼다
- [x] 5.2 `adapters/protocols.py` 에 `Retriever` 프로토콜(`retrieve(query, *, depth, versions)`)을 정의한다
- [x] 5.3 `adapters/retrievers/dense.py` — 기존 임베딩 + 벡터 스토어 질의를 프로토콜 뒤로 옮긴다. 코사인 하한을 이 안에서 적용한다
- [x] 5.4 `adapters/retrievers/lexical.py` — 어휘 색인을 프로토콜 뒤로 감싼다
- [x] 5.5 `adapters/retrievers/registry.py` — 이름 → 팩토리 표. 알 수 없는 이름은 `ConfigurationError`
- [x] 5.6 `config.py` 에 `retrievers` JSON 설정(이름·가중치·후보 깊이·필수 여부)과 융합 상수 `RRF_K`·IDF 커버리지 하한을 더한다(캐시 크기는 5.8 에서 뺀다). 빈 목록·미등록 이름·비양수 가중치·`top_k` 상한보다 작은 후보 깊이는 기동을 막는다
- [x] 5.7 `tests/test_config.py` 확장 — 기동을 막는 네 경우를 각각 검증한다. 후보 깊이는 기본값이 아니라 **`top_k` 상한**과 비교되는지까지 본다
- [ ] 5.8 `config.py` 에서 `retrieval_cache_size` 를 뺀다 — 검색 결과 캐시를 만들지 않기로 했으므로(design 결정 7) 아무도 읽지 않는 환경변수다. 5.6 에서 이미 커밋된 값이라 지우는 것이 남은 일이고, `tests/test_config.py` 에 그 항목을 보는 단언이 있으면 함께 지운다

## 6. 검색 서비스 팬아웃

- [x] 6.1 `services/retrieval.py` 의 저장소 질의 한 줄을 팬아웃으로 바꾼다. 대상 집합 계산·현재성 재검증·상위 K 절단은 **요청당 한 번** 그대로 유지한다
- [x] 6.2 `asyncio.gather(return_exceptions=True)` 결과를 분류한다 — 필수 실패는 `503`, 선택 실패는 경고 로그 후 제외, 전부 실패는 `503`. 실패한 retriever의 목록은 **빈 목록으로도 전달하지 않는다**(분모에서 빠져야 한다)
- [x] 6.3 융합 결과를 `ScoredChunk` 로 조립해 기여 내역을 싣는다. 기여 retriever 목록을 `RetrievalResult` 에 더한다
- [x] 6.4 `tests/test_retrieval_service.py` 확장 — 두 retriever 기여, 하나만 켠 구성의 순서 보존, 같은 대상 목록 공유, 대상 없으면 retriever 미호출, 어휘만 켜면 임베딩 미호출, 필수·선택·전부 실패의 세 처분, 선택 실패 시 1위 점수가 단독 구성과 같은 것
- [x] 6.5 `tests/test_retrieval_core.py` 확장 — 밀집 하한이 융합 **전에** 걸리는 것, 밀집 하한을 `1.0` 으로 올려도 어휘 결과가 남는 것, 양쪽 하한에 모두 걸리면 빈 결과

## 7. API 표면

- [x] 7.1 `api/queries.py` 의 `SearchResultView` 에 기여 내역을 더한다. `score` 의 의미를 설명하는 필드 문서를 고친다
- [x] 7.2 `api/routes/search.py` 응답에 기여 retriever 목록을 더하고, 로그의 `top_score` 가 무엇을 뜻하는지 맞춘다
- [x] 7.3 `tests/test_search_api.py` 확장 — 점수 범위 `(0, 1]`, 내림차순, 모든 결과에 기여 내역 1건 이상, 기여 목록에 실패한 선택 retriever가 없는 것, 설정으로 어휘를 끄면 목록·내역에서 사라지는 것
- [x] 7.4 `demo-ui` 의 점수 표기를 "유사도"에서 융합 점수로 고친다
- [x] 7.5 `README.md` 에 retriever 설정 방법(`APP_RETRIEVERS` 예시)과 응답 필드 설명을 적는다

## 8. 계측·회귀·마무리

- [ ] 8.1 `tests/test_retrieval_quality.py` 를 회귀 기준으로 유지한다 — `sample-docs/` 네 질의가 하이브리드 구성에서도 1위를 맞히는지, 무관 질의가 빈 결과인지
- [ ] 8.2 하이브리드가 구제하는 질의(문서에 그대로 적힌 식별자를 문맥 없이 묻기)를 밀집 단독 구성과 비교하는 테스트를 더한다
- [ ] 8.3 네 회귀 질의와 식별자 질의로 가중치·IDF 커버리지 하한을 실측하고, 조정이 필요하면 설정 기본값만 바꾼다
- [ ] 8.4 실측 절차와 값의 근거를 `ARCHITECTURE.md` 검색 파이프라인 절에 적는다 — 점수 의미 변경(유사도 → 융합 점수)과 하한 적용 지점 이동의 이력을 함께 남긴다. 검색 결과 캐시를 넣지 않은 이유(결과가 아니라 지연만 다룬다)도 한 줄 남긴다 — 적지 않으면 다음 사람이 빠진 것으로 읽고 다시 설계한다
- [ ] 8.5 `docker compose run --build --rm test` 로 전체 스위트를 돌린다. `--build` 없이 돌린 결과는 직전 이미지의 코드다
- [ ] 8.6 `docker compose run --build --rm test ruff check .` 와 `python3 scripts/check_comments.py` 를 통과시킨다
- [ ] 8.7 `docker compose up -d --build --wait` 후 `./data` 를 비우고 `sample-docs/` 두 건을 올려 `/search`·`/qa` 를 눈으로 확인한다 — 기여 내역과 기여 retriever 목록이 실제로 실려 나오는지
- [ ] 8.8 `openspec validate add-rrf-algorithm-spec --strict` 와 문서-코드 일치(`README.md`·`ARCHITECTURE.md` 에 적은 것이 전부 존재하는지)를 확인한다
