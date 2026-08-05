## 1. 재정렬 도메인 (순수 함수)

- [x] 1.1 `core/retrieval.py` 의 `ScoredChunk` 에 `rerank_score: float | None = None` 을 더한다. `score` 의 `(0, 1]` 검사와 의미는 **그대로 둔다** — 리랭킹 점수는 다른 자리에 산다 (design 결정 9)
- [x] 1.2 `core/reranking.py` 에 재정렬 순수 함수를 둔다 — 융합 결과와 점수 목록·깊이를 받아, 리랭킹된 후보는 `(-rerank_score, document_id, chunk_index)` 전순서로 앞에, 깊이 밖 후보는 융합 순서 그대로 뒤에 놓는다. I/O 없음, 표준 라이브러리만 (design 결정 4)
- [x] 1.3 점수 목록의 길이가 대상 후보 수와 다르면 도메인 오류로 끊는다. 어긋난 채로 `zip` 하면 **다른 청크의 점수가 실린다** — 오류가 아니라 조용한 오답이다
- [x] 1.4 테스트 — 순서가 리랭킹 점수 내림차순이고, 동점이 정체성 값 오름차순으로 깨지며, 같은 입력을 열 번 넣어도 순서가 같은지 (`retrieval`: 리랭킹 동점이 순서를 흔들지 않는다)
- [x] 1.5 테스트 — 재정렬이 후보를 **걸러내지 않는지**. 입력 집합과 출력 집합이 같고 깊이 밖 후보가 융합 순서를 유지한 채 뒤에 오는지 (`retrieval`: 리랭킹은 결과를 걸러내지 않는다)
- [x] 1.6 테스트 — 융합 점수와 기여 내역이 재정렬을 통과해도 그대로인지 (`retrieval`: 리랭킹이 융합 점수를 바꾸지 않는다)

## 2. `Reranker` 계약과 크로스인코더 어댑터

- [x] 2.1 `adapters/protocols.py` 에 `Reranker` 프로토콜을 더한다 — `rerank(query, documents) -> list[float]`, `signature: str`, `warm_up()`. `ScoredChunk` 를 넘기지 않고 정렬도 시키지 않는다 (design 결정 2)
- [x] 2.2 `adapters/reranking/local.py` — `CrossEncoderReranker`. 임베더와 같은 모양으로 알려진 모델 프로파일 표(입력 창)를 두고, 표에 없는 이름은 `ConfigurationError` 로 기동을 막는다
- [x] 2.3 로딩 시 실제 모델의 입력 창이 선언보다 좁으면 `ConfigurationError` 를 던진다. 임베딩 어댑터의 `_assert_matches_declaration` 과 같은 자리·같은 이유다 (`reranking`: 실제 모델이 선언보다 좁으면 로딩이 실패한다)
- [x] 2.4 **커밋 해시를 고정**한다(`revision=`). 값은 실제로 받은 커밋으로 채운다. `trust_remote_code` 는 켜지 않는다 — 표의 모델은 원격 코드가 없고, 그래야 이 고정이 가중치·설정·토크나이저 전부에 미친다 (design 결정 8)
- [x] 2.5 원 로짓에 시그모이드를 씌워 `(0, 1)` 로 내보낸다. 단조 변환이라 순서가 바뀌지 않는다는 것과 **교정된 값이 아니라는 것**을 주석에 남긴다 (design 결정 3)
- [x] 2.6 후보 목록이 비면 모델을 부르지 않고 빈 목록을 돌려준다 (`reranking`: 후보가 없으면 모델을 부르지 않는다)
- [x] 2.7 인코딩을 `asyncio.to_thread` 로 내보내고, 지연 로딩(`_ensure_model`)을 백스톱으로 둔다. 모델 인스턴스는 하나이고 로딩은 잠금 아래에서 한 번만 일어난다 (design 결정 10)
- [x] 2.8 `signature` 를 **가중치를 올리지 않고** 만든다 — 모델 이름과 점수 규약 판 번호로 구성한다 (`reranking`: 가중치를 올리지 않고도 서명을 읽는다)
- [x] 2.9 테스트 — 점수 개수·순서가 후보와 대응하고, 같은 입력이 같은 점수를 주며, 후보 0개에서 모델이 불리지 않는지 (`reranking`: 질의와 후보를 함께 읽어 후보별 점수를 돌려준다)
- [x] 2.10 테스트 — 모델이 다르면 서명이 다르고, 서명을 읽는 동안 가중치 로딩이 일어나지 않는지

## 3. 설정과 기동 검증

- [x] 3.1 `config.py` 에 항목을 더한다 — `reranker_enabled`(기본 `True`), `reranker_model`(기본 `BAAI/bge-reranker-v2-m3`), `rerank_candidates`(기본 30), `reranker_timeout_seconds`. 전부 기본값을 가져 환경변수 없이 기동된다
- [x] 3.2 `rerank_candidates < retrieval_max_top_k` 면 기동을 막는 검증자를 더한다. `_candidate_depth_must_cover_the_k_ceiling` 옆에 같은 모양으로 둔다 (`retrieval`: 리랭크 깊이가 K 상한보다 작으면 기동을 막는다)
- [x] 3.3 선언된 입력 창이 `retrieval_max_query_chars` 와 `chunk_size` 를 함께 담지 못하면 기동을 막는다 (`reranking`: 입력 창이 질의와 청크를 담지 못하면 기동을 막는다)
- [x] 3.4 테스트 — 위 세 실패가 각각 기동을 세우고, 실패 사유에서 문제가 된 값을 확인할 수 있는지. 기본 설정으로는 기동이 통과하는지

## 4. 검색 파이프라인 배선

- [x] 4.1 `services/retrieval.py` 에 리랭커를 선택 의존성으로 받는다(`None` 이면 리랭킹 없음). 융합 **뒤**, `_drop_superseded` **앞**에 한 단계를 끼운다 — 절단은 지금처럼 재검증 뒤에 남는다 (design 결정 4)
- [x] 4.2 `RetrievalResult` 에 `ordered_by` 와 이번 검색에 실제로 돈 리랭커 이름을 싣는다. 리랭킹이 돌지 않았으면 이름이 없다 (`retrieval`: 응답은 순서를 정한 신호를 밝힌다)
- [x] 4.3 축소 경로를 구현한다 — `asyncio.wait_for` 로 감싸고, 예외·타임아웃이면 경고 로그 뒤 융합 순서를 그대로 쓴다. 취소(`CancelledError`)는 축소 대상이 아니다 (`_dispose_of` 가 취소를 다루는 방식과 같게) (design 결정 6)
- [x] 4.4 `RetrievalService` 에 `rerank_signature` 읽기 전용 프로퍼티를 더한다 — 리랭커가 없으면 빈 문자열. 캐시가 이것을 물어본다 (design 결정 7)
- [x] 4.5 `main.py` 에서 리랭커를 배선하고 `create_app` 의 주입 인자로 연다. `reranker_enabled=false` 면 `None` 을 넘긴다 — 인메모리 대역 같은 중간 구현을 만들지 않는다
- [x] 4.6 lifespan 에서 리랭커를 선로딩한다. 임베더와 같은 자리이고 **실패는 경고로 끝낸다** (design 결정 10, `reranking`: 가중치가 없어도 기동된다)
- [x] 4.7 검색 로그에 `ordered_by` 와 리랭커 이름·리랭킹 대상 후보 수를 더한다. 질의 문자열과 청크 본문은 여전히 싣지 않는다
- [x] 4.8 테스트 — 순서를 뒤집는 페이크 리랭커에서 `results` 순서가 바뀌고, 결과 집합은 그대로인지 (`retrieval`: 리랭킹이 순서를 바꾼다)
- [x] 4.9 테스트 — 실패하는 페이크·느린 페이크에서 각각 `200` 과 융합 순서로 끝나고, 그 결과가 **리랭커를 끈 구성의 결과와 같은지** (`retrieval`: 축소 결과는 리랭커를 끈 구성과 같다)
- [x] 4.10 테스트 — 리랭킹이 융합 **뒤**·재검증 **앞**에 정확히 한 번 도는지. 재검증에서 떨어진 문서의 청크가 결과에 없고, 리랭킹 호출 횟수가 요청당 1 인지
- [x] 4.11 테스트 — 리랭킹이 오래 걸리는 동안 헬스 응답이 막히지 않는지 (`retrieval`: 리랭킹이 오래 걸리는 검색 중 다른 요청)

## 5. API 표면

- [x] 5.1 `api/queries.py` 의 `SearchResultView` 에 `rerank_score: float | None` 을 더한다. `score` 필드의 설명과 제약(`gt=0, le=1`)은 그대로 둔다
- [x] 5.2 `api/routes/search.py` 의 `SearchResponse` 에 `ordered_by` 와 `reranker` 를 더한다. 두 필드의 뜻을 스키마 설명에 적는다 — 기여 retriever 목록과 같은 목적이다
- [x] 5.3 `api/sse.py` 의 `sources` 이벤트에 같은 두 필드를 더한다. `results` 가 `/search` 와 같은 모양이라는 계약이 여기서 지켜진다 (design 결정 9)
- [x] 5.4 테스트 — 리랭커를 켠 응답에 세 필드가 모두 있고, 끈 응답에서 `ordered_by` 가 융합이며 `reranker` 와 `rerank_score` 가 비어 있는지 (`retrieval`: 리랭커를 끄면 응답이 그 사실을 밝힌다)
- [x] 5.5 테스트 — `/qa` 의 `sources` 이벤트와 `/search` 응답의 결과 모양이 여전히 같은지

## 6. 캐시 키 재료

- [x] 6.1 `core/cache.py` 의 `derive_cache_key`·`derive_cache_scope` 에 리랭커 서명 재료를 더한다. 두 함수가 같은 재료를 쓰는 규약을 유지한다 (`response-cache`: 리랭커 구성이 정체성 재료다)
- [x] 6.2 `services/cache.py` 가 그 값을 배선에서 따로 받지 않고 `RetrievalService.rerank_signature` 에서 읽게 한다 — 유도 지점을 늘리지 않는다 (design 결정 7)
- [x] 6.3 `CachedAnswer` 코덱이 `rerank_score` 를 왕복시키고, 그 필드가 없는 옛 페이로드도 읽을 수 있게 한다
- [x] 6.4 테스트 — 리랭커를 켜고 끈 두 구성에서 같은 질문이 서로 다른 항목이 되는지, 모델을 바꿔도 그런지 (`response-cache`: 리랭커를 켜면 이전 항목을 쓰지 않는다)
- [x] 6.5 테스트 — 리랭커가 꺼진 구성에서 기존 캐시 동작(정확 매치·유사 매치·무효화)이 그대로인지

## 7. 컨테이너와 실행

- [x] 7.1 `Dockerfile` 에서 리랭커 가중치를 임베딩 가중치와 **같은 레이어**에 굽는다. `APP_RERANKER_MODEL` 환경변수가 굽는 모델과 런타임 모델의 유일한 진실 원천이다 (design 결정 8)
- [x] 7.2 굽는 단계가 `KNOWN_RERANKER_PROFILES` 와 **같은 리비전**을 쓰게 하고, 그 일치를 테스트가 고정한다 — `Dockerfile` 은 앱 패키지를 import 할 수 없어 값이 두 곳에 적히고, 어긋나면 런타임에 조용히 다시 받는다 (`docker-compose.yml` 을 읽어 계약을 고정하는 테스트와 같은 방식)
- [x] 7.3 `docker compose run --build --rm test` 로 전체 스위트를 돌린다. 리랭커 실물 층이 컨테이너 안에서 실제로 도는지 확인한다(가중치가 구워져 있다)
- [x] 7.4 `docker compose up -d --build --wait` 로 `/search` 를 실물로 한 번 친다. `./data` 에 남은 이전 실행 벡터를 지우고 `sample-docs/` 두 건만 올린 뒤 잰다 (`CLAUDE.md` 검증 절차)

## 8. 실측

- [x] 8.1 후보 30개 리랭킹의 CPU 지연을 컨테이너 안에서 잰다. 회귀 질의 4개 기준이며, 첫 요청(지연 로딩)과 이후 요청을 나눠 적는다 (design 결정 11)
- [x] 8.2 `reranker_timeout_seconds` 기본값을 그 실측의 3배 이상으로 정하고, 근거를 `config.py` 주석에 남긴다
- [x] 8.3 리랭킹 전후의 순위 변화를 잰다 — 같은 저장소 위에 설정만 다른 검색 서비스를 세워(`searching_with`) 비교한다. 저장소를 새로 만들면 비교가 성립하지 않는다
- [x] 8.4 이미지 크기 증가를 잰다(`docker image ls` 전후)
- [x] 8.5 실측 결과를 보고 `rerank_candidates` 기본값 30 을 유지할지 정한다 (design Open Questions)

## 9. 품질 테스트와 회귀

- [ ] 9.1 리랭킹 품질 테스트를 더한다 — 실물 모델로 한국어 질의에서 관련 있는 후보가 위로 오는지. 가중치가 없으면 사유와 함께 건너뛴다(임베딩 품질 테스트와 같은 방식)
- [ ] 9.2 회귀 — `sample-docs/` 네 질의의 1위가 리랭커를 켠 구성과 끈 구성에서 같은지 (`retrieval`: 리랭킹을 켜도 회귀 질의의 1위가 같다)
- [ ] 9.3 회귀 — 하이브리드 구제 시나리오(식별자 질의)가 리랭킹을 켜도 그대로인지
- [ ] 9.4 회귀 — 리랭커를 끈 구성에서 **기존 테스트 전부**가 통과하는지. 이것이 "얹었을 뿐 바꾸지 않았다"의 증명이다
- [ ] 9.5 `python3 scripts/check_comments.py` 로 주석 규칙 위반 0건을 확인한다. 새 파일의 긴 설명은 `tests/README.md` 나 `ARCHITECTURE.md` 로 옮긴다

## 10. 문서

- [ ] 10.1 `ARCHITECTURE.md` 「재랭킹·질의 확장은 여전히 없다」를 **리랭킹 절로 갈아 쓴다.** 파이프라인 그림에 단계를 더하고, 질의 확장·HyDE 가 여전히 없다는 사실은 남긴다
- [ ] 10.2 모델 후보표와 기각 사유를 적는다 — 기각의 축이 둘("한국어가 학습 분포 안에 있는가", "아키텍처가 `transformers` 안에 있는가")이라는 것과, 근거가 벤치마크가 아니라 학습 언어 구성·실물 로딩 결과라는 사실을 함께 적는다 (design 결정 1)
- [ ] 10.3 순위 신호가 둘인 이유와 `score` 를 갈아치우지 않은 근거를 적는다 (design 결정 9)
- [ ] 10.4 리랭킹 점수에 하한을 걸지 않은 이유를 적는다 — 「하한이 판정하는 것과 하지 않는 것」 옆에 둔다 (design 결정 5)
- [x] 10.5 8장의 실측표(지연·순위 변화·이미지 크기)를 적는다. **품질이 좋아졌다고 말할 근거가 없다는 사실을 함께 적는다** (design 결정 11)
- [ ] 10.6 원격 코드를 쓰는 모델을 피한 근거와 리비전 고정을 적는다 — 첫 후보가 실물에서 깨진 기록(`transformers` 판 비호환, 코드가 다른 저장소라 고정 불가)을 함께 남긴다 (design 결정 1·8)
- [ ] 10.7 캐시 키에 리랭커 몫만 들어갔고 **retriever 가중치는 여전히 키에 없다**는 사실을 적는다. 적지 않으면 다음 사람이 "검색 구성이 전부 키에 있다"고 읽는다 (design 결정 7)
- [ ] 10.8 `README.md` 에 리랭커 설정(`APP_RERANKER_ENABLED`·`APP_RERANKER_MODEL`·`APP_RERANK_CANDIDATES`)과 끄는 방법, 첫 빌드가 길어진다는 사실을 적는다
- [ ] 10.9 새 테스트의 밀려난 설명을 `tests/README.md` 에 파일별로 옮긴다
- [ ] 10.10 `openspec validate add-cross-encoder --strict` 와 문서-코드 일치를 마지막으로 확인한다 — 문서에 적은 기능의 코드가 전부 있는지
