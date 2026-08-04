## 1. 캐시 도메인 (순수 함수)

- [x] 1.1 `core/cache.py` 에 `normalize_query` 를 둔다 — NFC → `strip` → 연속 공백 축약 → `casefold`. 그 이상의 의미 변형은 하지 않는다 (design.md 결정 3)
- [x] 1.2 `core/cache.py` 에 `derive_cache_key` 를 둔다 — 다섯 재료(정규화 질의·`top_k`·`PROMPT_VERSION`·`index_signature`·모델 식별자)의 정규 JSON → SHA-256. `derive_index_signature` 와 같은 직렬화 규약을 쓴다
- [x] 1.3 `core/cache.py` 에 값 객체를 둔다 — `CachedAnswer`(답변·종료 사유·인용·`sources`·`source_versions`), `CacheLayer`(`EXACT`/`SEMANTIC`), `CacheLookup`(히트 여부·층·유사도·항목). I/O 는 없다
- [x] 1.4 `core/cache.py` 에 코사인 유사도 계산과 "후보 중 최댓값 하나" 선택을 둔다. 임계값 판정도 여기다 — 어댑터가 정책을 갖지 않게 한다
- [x] 1.5 테스트 — 정규화(공백·대소문자·유니코드 NFD/NFC 쌍이 같은 키), 재료 하나만 달라도 키가 달라짐, 키가 질의 문자열을 복원 가능한 형태로 담지 않음 (`response-cache`: 질문과 답변 본문은 로그에 남기지 않는다)

## 2. 캐시 저장소 계약과 구현 셋

- [x] 2.1 `adapters/protocols.py` 에 `ResponseCache` 프로토콜을 더한다 — `lookup_exact`, `lookup_semantic`, `store`, `invalidate_document`, `invalidate_negative`, `discard`. 소비처가 생기는 지금 정의한다
- [x] 2.2 `adapters/cache/null.py` — `NullResponseCache`. 조회는 항상 미스, 저장은 무동작. `cache_enabled=false` 가 배선되는 곳이다. 인메모리로 잇지 않는다 — 그러면 캐시를 껐는데 히트가 계속 난다 (design.md 결정 12)
- [x] 2.3 `adapters/cache/memory.py` — `InMemoryResponseCache`. TTL·상한·유사 매치·태그 무효화까지 전체 의미를 구현한다. **테스트가 명시적으로 주입할 때만** 쓰이며, 프로덕션 배선이 여기로 떨어지는 분기를 만들지 않는다
- [x] 2.4 `config.py` 에 설정 항목을 더한다 — `cache_enabled`, `cache_ttl_seconds`, `cache_max_entries`, `cache_semantic_threshold`, `cache_semantic_candidates`. 전부 기본값을 갖게 해 환경 변수 없이 기동되게 한다
- [x] 2.5 `config.py` 에 장애 대비 항목을 더한다 — `cache_operation_timeout_seconds`(기본 0.2), `cache_circuit_breaker_failures`(기본 3), `cache_circuit_breaker_cooldown_seconds`(기본 30). 헬스 프로브의 2초와 값이 다른 이유를 주석으로 남긴다 (design.md 결정 13)
- [x] 2.6 테스트 — TTL 만료 후 미스, 항목 수 상한 초과 시 오래된 항목부터 밀려나되 저장이 거부되지 않음, 후보 스캔 상한이 지켜짐 (`response-cache`: 캐시 항목에는 수명과 총량 상한이 있다)
- [ ] 2.7 테스트 — `cache_enabled=false` 로 **같은 질문을 두 번** 보내면 둘 다 미스이고 생성기가 두 번 호출되는지 (`response-cache`: 캐시를 끈 상태에서는 어떤 요청도 히트가 되지 않는다)
- [x] 2.8 테스트 — 배선 코드에 인메모리 구현으로 가는 분기가 없는지 고정한다. 프로덕션 경로가 인메모리로 새면 결정 12 가 막으려던 실패가 그대로 돌아온다

## 3. Redis 어댑터

- [x] 3.1 `adapters/cache/redis.py` — 연결 관리. `probe.py` 와 클라이언트를 공유하지 말고 수명을 분리한다(프로브는 매번 새로 연결해 닫는다)
- [x] 3.2 `adapters/cache/store.py` — 키 구조를 구현한다: `qa:v1:entry:{fp}`(TTL), `qa:v1:vec:{fp}`(float32 packed, TTL), `qa:v1:index:{scope}`(ZSET, 점수는 `INCR` 카운터), `qa:v1:doc:{id}`(SET), `qa:v1:negative`(SET) (design.md 결정 2)
- [x] 3.3 L2 후보 스캔을 구현한다 — **그 scope 의** `ZCARD` 가 0 이면 즉시 미스(임베딩을 만들지 않는다), 아니면 `ZREVRANGE` 상한개 → `MGET` 벡터 → 완전 탐색. 페이로드는 이긴 하나만 읽는다. 다른 scope 의 항목은 후보에 들어오지 않는다 (design.md 결정 1·2·10)
- [x] 3.4 지연 정리를 구현한다 — `MGET` 이 `None` 을 돌려준 fp 를 `ZREM` 으로 걷어낸다. 백그라운드 스위퍼를 만들지 않는다
- [x] 3.5 용량 상한을 구현한다 — 저장 후 그 scope 의 `ZCARD` 가 상한을 넘으면 `ZREMRANGEBYRANK` 로 오래된 것부터 자르고 그 fp 의 `entry`·`vec` 키를 지운다. 상한이 scope 단위로 도는 근거는 design.md 결정 2 에 있다
- [x] 3.6 태그 무효화를 구현한다 — `SMEMBERS` 로 fp 를 받아 `entry`·`vec` 를 `DEL`, `index` 에서 `ZREM`, 태그 SET 자체를 `DEL`. 저장 시 태그 SET 의 TTL 을 갱신한다
- [x] 3.7 Redis 왕복을 파이프라인으로 묶는다 — 저장은 `entry`·`vec`·`index`·태그 SET 이 한 번에 나가야 한다
- [x] 3.8 블로킹 호출이 이벤트 루프에 남지 않게 한다. 벡터 완전 탐색(CPU 바운드)은 스레드풀로 오프로드한다
- [x] 3.9 테스트 — Redis 계약 테스트를 `redis` 마커 뒤에 둔다(`llm` 마커와 같은 방식). 기본 `pytest` 한 줄 실행에서 빠지는지 확인한다
- [x] 3.10 `ARCHITECTURE.md` 에 키 구조와 선택 근거를 적는다 — RediSearch·Chroma·프로세스 메모리를 기각한 이유, 벡터를 페이로드에서 뗀 이유, 지연 정리를 고른 이유

## 4. 캐시 오케스트레이션

- [x] 4.1 `services/cache.py` — 조회 순서를 구현한다: L1 정확 매치 → (미스면) 질의 임베딩 → L2 유사 매치 → 임계값 판정. 정확 매치가 성공하면 임베딩을 만들지 않는다 (`response-cache`: 정확 매치가 있으면 유사 매치를 시도하지 않는다)
- [x] 4.2 현재성 재검증을 구현한다 — 히트 후보의 `source_versions` 를 `DocumentRegistry` 로 확인하고, `Document.is_up_to_date` 를 쓴다(판정을 새로 구현하지 않는다). 하나라도 어긋나면 히트를 버리고 항목을 지운 뒤 미스로 진행한다 (design.md 결정 7)
- [x] 4.3 저장 정책을 구현한다 — `done` 으로 끝난 것만 저장하고 `error` 는 저장하지 않는다. `sources` 가 있으면 그 문서 전부를 태그로 달고, **`no_evidence`·`insufficient_evidence` 는 `negative` 집합에도 넣는다**. `insufficient_evidence` 는 태그와 집합을 **둘 다** 받는다 — 두 기제가 다른 사건을 덮는다 (design.md 결정 4)
- [x] 4.4 축소 동작을 구현한다 — 조회 실패는 미스, 저장 실패는 로그. 캐시 예외가 `services/qa.py` 밖으로 나가지 않는다 (`response-cache`: 캐시에 닿지 못해도 질의응답은 성립한다)
- [x] 4.5 작업 타임아웃과 차단기를 `services/cache.py` 에 둔다 — 연속 실패 N회면 쿨다운 동안 저장소를 호출하지 않고 즉시 미스, 쿨다운 뒤 첫 요청이 탐침이 되어 자동 회복한다. 상태는 프로세스 메모리다 (design.md 결정 13)
- [x] 4.6 관측을 구현한다 — 히트 여부·층·유사도·무효화된 항목 수·축소 여부를 로그에 남기되 질문 원문·답변 본문·청크 본문은 남기지 않는다
- [x] 4.7 로그 빈도를 상태에 맞춘다 — 캐시 꺼짐은 **기동 시 한 줄**, 저장소 가용/불가용 전환은 **전이 시점에만** 경고. 요청마다 경고를 찍으면 로그가 넘쳐 진짜 신호가 묻힌다 (design.md 결정 12)
- [ ] 4.8 테스트 — L1 히트에서 임베더가 호출되지 않음, L2 히트에서 생성기가 호출되지 않음, 임계값 미달이 미스가 됨, `error` 종료가 캐시되지 않음
- [x] 4.9 테스트 — 인용 문서가 옛 리비전인 항목이 남아 있을 때 히트가 버려지고 미스로 처리되며, 그 항목이 캐시에서 제거되는지 (`response-cache`: 캐시된 답변은 인용 문서가 지금도 현재일 때만 쓰인다)
- [x] 4.10 테스트 — 캐시 저장소가 죽은 상태·비활성화 상태에서 `/qa` 가 `200` 으로 끝나고 히트로 표시되지 않는지
- [x] 4.11 테스트 — 응답하지 않는 저장소에서 조회가 타임아웃 안에 미스로 끝나는지, 연속 실패 뒤에는 저장소를 아예 호출하지 않는지, 쿨다운 뒤 재기동 없이 히트가 다시 나는지 (`response-cache`: 캐시 장애가 응답을 느리게 만들지 않는다)

## 5. `/qa` 배선

- [ ] 5.1 `RetrievalService` 에 `index_signature` 읽기 전용 프로퍼티와 `resolve_top_k(top_k) -> int` 를 더한다. `search` 가 그 메서드를 쓰게 해 기본값 규칙이 한 곳에만 남게 한다 (design.md 결정 14)
- [ ] 5.2 `QaService` 가 키 재료 셋을 확보하게 한다 — `effective_k` 와 `index_signature` 는 `RetrievalService` 에서 읽고, `qa_llm_model` 은 배선이 주입한다. **어느 것도 `QaService` 안에서 다시 유도하지 않는다** — 유도 지점이 늘면 캐시가 검색과 다른 세대의 키를 쓴다
- [ ] 5.3 테스트 — `top_k` 를 생략한 요청과 설정 기본값과 같은 `top_k` 를 명시한 요청이 **같은 캐시 키**를 만드는지. `None` 이 키에 들어가면 여기서 깨진다
- [ ] 5.4 테스트 — `retrieval_top_k` 설정을 바꾸면 이전 항목이 히트되지 않는지, `index_signature` 가 검색과 캐시에서 같은 값인지
- [ ] 5.5 `QaContext` 가 캐시 조회 결과와 검색에 실제로 쓰인 질의를 담게 한다. `result` 는 히트일 때 없을 수 있다
- [ ] 5.6 `QaService.prepare` 가 검색 **앞에서** 캐시를 조회하고, 히트면 검색을 하지 않게 한다. 검색 예외는 지금처럼 그대로 올린다 (design.md 결정 5)
- [ ] 5.7 `QaService.stream` 에 히트 재생 경로를 더한다 — `sources`(캐시된 근거) → `answer`(**조각 하나**) → `done`. 원래의 조각 경계를 저장하지도 재생하지도 않는다 (design.md 결정 6)
- [ ] 5.8 히트 경로가 `anyio.CapacityLimiter` 를 잡지 않게 한다 — 히트에는 생성기 프로세스가 없다 (design.md 결정 5)
- [ ] 5.9 저장 호출을 `done` 을 내보내기 **직전**에 둔다. 실패는 삼키고 로그로 남긴다
- [ ] 5.10 `api/sse.py` 의 `done` 페이로드에 `cache_hit`·`cache_layer`·`cache_similarity` 를 더한다. 기존 필드의 의미는 바꾸지 않는다 (design.md 결정 11)
- [ ] 5.11 `main.py` 에서 배선한다 — `cache_enabled` 에 따라 `NullResponseCache` 또는 `RedisResponseCache` 만 고른다(인메모리 분기 없음). Redis 에 닿지 못하는 기동이 실패하지 않는지 확인한다
- [ ] 5.12 테스트 — 히트의 이벤트 이름 순서가 미스와 같고, `answer` 들을 이어 붙인 것이 `done.answer` 와 같고, `citations` 가 첫 요청의 것과 같은지 (`answer-generation`: 캐시에서 온 답변도 같은 이벤트 시퀀스로 전달된다)
- [ ] 5.13 테스트 — `elapsed_ms` 가 이번 요청의 값이지 캐시된 복사가 아닌지, 미스/정확 히트/유사 히트에서 `cache_layer` 가 각각 구분되는지
- [ ] 5.14 테스트 — `no_evidence` 와 `insufficient_evidence` 가 각각 캐시되고 두 번째 요청에서 검색이 다시 돌지 않는지 (`response-cache`: 답하지 못한 종료도 캐시되지만, 문서 내용이 바뀌면 먼저 사라진다)
- [ ] 5.15 `README.md` 에 캐시 설정 항목과 `cache_hit` 확인 방법을 적는다. **`qa_llm_model` 을 비워 두면 CLI 기본 모델이 바뀌어도 캐시 키가 그대로라는 사실**을 함께 적는다 (design.md 결정 14). `PROMPT_DESIGN.md` 에 프롬프트 버전이 캐시 키에 들어간다는 사실을 적는다 — 프롬프트를 고치면 캐시가 저절로 갈린다

## 6. 무효화 (핵심 3경로)

- [ ] 6.1 `services/ingestion.py` 의 `ingest` 가 레지스트리 커밋 **뒤에** 해당 문서의 캐시를 무효화하게 한다. `IngestionStatus.UNCHANGED` 에서는 부르지 않는다 (design.md 결정 8)
- [ ] 6.2 `UNCHANGED` 를 뺀 모든 수집(`CREATED`·`REPLACED`·`REINDEXED`)에서 `negative` 집합을 비운다 — 판정을 뒤집는 것은 문서 개수가 아니라 내용이다. `REINDEXED` 를 빠뜨리면 stale 복구가 낫지 않는다 (design.md 결정 8)
- [ ] 6.3 `services/ingestion.py` 의 `delete` 가 삭제 확정 뒤에 해당 문서의 캐시를 무효화하게 한다
- [ ] 6.4 무효화 실패가 수집·삭제 요청을 실패시키지 않게 하고, 실패 사실을 경고 로그로 남긴다
- [ ] 6.5 테스트 — 재업로드 후 그 문서에 관한 질문이 미스가 되고 생성기가 다시 호출되는지 (`document-ingestion`: 재업로드가 그 문서를 인용한 답변을 무효화한다)
- [ ] 6.6 테스트 — 삭제 후 그 문서를 인용한 답변이 나가지 않는지
- [ ] 6.7 테스트 — 문서 A 를 인용한 **긍정 답변**(`stop`)이 문서 B 의 변경으로 지워지지 않는지. 6.11 과 짝이다 — 같은 사건이 긍정은 살리고 부정은 지운다 (`response-cache`: 관계없는 문서의 변경은 캐시를 건드리지 않는다)
- [ ] 6.8 테스트 — 거부된 업로드(지원하지 않는 포맷·빈 문서)와 `unchanged` 재업로드가 캐시를 건드리지 않는지
- [ ] 6.9 테스트 — 새 문서 수집이 `no_evidence` 항목을 무효화하는지 (`document-ingestion`: 새 문서 수집이 근거 없음 판정을 무효화한다)
- [ ] 6.10 테스트 — **기존 문서를 다른 내용으로 교체**해도 `no_evidence` 항목이 무효화되는지. 문서 수가 그대로인 것이 함정이다 (`response-cache`: 문서를 교체해도 근거 없음 판정이 사라진다)
- [ ] 6.11 테스트 — 문서 A 를 근거로 `insufficient_evidence` 가 캐시된 뒤 **문서 B 를 수집**하면(A 는 안 건드림) 그 항목이 무효화되는지 (`response-cache`: 새 문서가 "답할 수 없음" 판정을 뒤집는다)
- [ ] 6.12 테스트 — `STALE` 로 검색에서 빠진 동안 캐시된 `no_evidence` 가, 같은 내용의 재수집(`REINDEXED`, 서명 동일)으로 무효화되는지. 서명이 그대로라 캐시 키가 살아 있는 유일한 재색인 경로다
- [ ] 6.13 테스트 — `unchanged` 재업로드가 `negative` 집합을 비우지 않는지
- [ ] 6.14 테스트 — 캐시 저장소가 죽은 상태에서 수집이 정상 성공하는지

## 7. 임계값 계측과 마감

- [ ] 7.1 `sample-docs/` 두 건으로 유사도 분포를 잰다 — 같은 뜻의 다른 표현 쌍, 서로 다른 질문 쌍, 그리고 **부정문 쌍을 반드시 포함한다**(`"환불 정책이 어떻게 되나요?"` ↔ `"환불이 안 되는 경우가 있나요?"` 형태). 각 쌍의 코사인 유사도를 기록한다 (design.md 결정 9)
- [ ] 7.2 부정문 쌍이 임계값 위에 남는지 확인하고, **남는다는 사실 자체를 계측 결과로 기록한다** — 임계값으로 풀리지 않는 한계라 값을 고르는 근거가 아니라 값의 한계로 남긴다 (design.md Risks 「부정문이 긍정문과 구분되지 않는다」)
- [ ] 7.3 계측 결과로 `cache_semantic_threshold` 기본값을 정하고, `retrieval_min_score = 0.82` 와 같은 방식으로 근거를 값 옆 주석과 `ARCHITECTURE.md` 에 남긴다. **부정문 한계와 "확신이 없으면 1.0 가까이 올려 L2 를 끈다"는 선택지를 함께 적는다** — 이 한계를 모르는 사람이 값을 내리는 것을 막는다
- [ ] 7.4 `docker compose run --build --rm test` 로 전체 스위트를 돌린다 — LLM 구독도 실제 Redis 도 없이 통과해야 한다
- [ ] 7.5 `docker compose up -d --build --wait` 후 실물 `/qa` 를 두 번 호출해 `cache_hit` 이 `false` → `true` 로 바뀌는지 눈으로 확인한다. `./data` 에 이전 실행의 벡터가 남아 있으면 지우고 `sample-docs/` 두 건만 다시 올린 뒤 잰다
- [ ] 7.6 `docker compose stop redis` 상태에서 `/qa` 응답 시간이 `cache_enabled=false` 와 같은 수준인지 잰다 — 차단기가 실제로 닫히는지 확인하는 유일한 방법이다 (`response-cache`: 캐시 장애가 응답을 느리게 만들지 않는다)
- [ ] 7.7 `docker compose run --build --rm test ruff check .` 와 `python3 scripts/check_comments.py` 를 통과시킨다
- [ ] 7.8 문서-코드 일치를 확인한다 — `ARCHITECTURE.md` 9번째 줄의 "**캐싱은 아직 없습니다**"와 1415번째 줄의 "캐시는 아직 없으므로 `cache_hit` 자리를 화면에 두지 않았습니다"를 고친다. 고치지 않으면 문서가 구현을 부정한다
