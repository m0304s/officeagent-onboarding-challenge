## 1. 인프라 전제 확인 (결정 5)

- [x] 1.1 `docker-compose.yml`의 `redis:7-alpine` → `redis:8-alpine`, `pyproject.toml`의 `redis>=5.2` → `redis>=6.0`
- [x] 1.2 컨테이너에서 벡터셋 지원을 실측한다 — `docker compose up -d --build redis` 후 `COMMAND INFO VADD`가 비어 있지 않은지, `VADD`/`VSIM`/`VREM`/`VCARD`가 도는지 확인하고 결과를 design.md 「위험」에 한 줄로 반영
- [x] 1.3 `redis-py`에 `vadd`/`vsim`/`vrem`/`vcard` 헬퍼가 있는지 확인하고, 없으면 어댑터가 `execute_command`로 보내기로 확정 (결정 5의 대비책)
- [x] 1.4 `CacheProbe`에 벡터셋 지원 확인을 더한다 — 미지원이면 `unavailable` 사유로 드러내고, 배선이 캐시를 축소 동작으로 떨어뜨린다
- [x] 1.5 `tests/test_probes.py`에 벡터셋 미지원 프로브가 `unavailable`을 내는 경우를 추가

## 2. 검색 게이트를 표시로 (결정 1·2 / `retrieval`·`lexical-index` 델타)

- [x] 2.1 `core/retrieval.py`의 `RetrievedChunk`에 `gate_passed` 추가 (기본값 `True` — 게이트가 없는 retriever를 위한 값)
- [x] 2.2 `adapters/retrievers/dense.py` — 하한으로 거르지 말고 `gate_passed`에 비교 결과를 채워 최대 `depth`개를 그대로 싣는다
- [x] 2.3 `adapters/lexical/sqlite.py::_search` — `_has_rare_overlap` 결과로 `gate_passed`를 채우고, BM25 순 상위 `top_k`를 압축 없이 싣는다
- [x] 2.4 `services/retrieval.py` — 융합 입력을 만들 때 항목별 게이트 통과 여부를 목록 간 OR로 합치고, 융합 뒤·절단 전에 채택 규칙(하나라도 통과)을 적용
- [x] 2.5 `tests/test_lexical_index.py` — 흔한 토큰만 겹치는 청크가 목록에 남되 `gate_passed=False`로 표시되고, 판정이 BM25 순서를 바꾸지 않는지
- [x] 2.6 `tests/test_retrieval_core.py` — 두 목록이 함께 올린 하한 미만 항목이 채택되는지, 어느 게이트도 통과하지 못한 항목이 결과에 없는지
- [x] 2.7 `tests/test_retrieval_core.py` — 스펙에서 다시 쓴 시나리오 둘(단독 결과의 하한, 밀집 하한 `1.0`에서 어휘 결과 잔존)로 교체. `tests/test_retrieval_quality.py`(실물 임베더)는 손대지 않고 회귀만 확인
- [x] 2.8 `ARCHITECTURE.md`의 「검색 파이프라인」·「어휘 색인」에서 하한이 목록을 자른다고 적은 문장을 채택 규칙 설명으로 고친다

## 3. 현재성 재검증 절단 (결정 7 / `retrieval` 델타)

- [x] 3.1 `services/retrieval.py::_drop_superseded`를 「절단 → 재검증 → 채움」 루프로 교체하고, 문서별 판정을 요청 안에서 메모
- [x] 3.2 `tests/test_retrieval_service.py` — 상위 K의 문서가 밀려나면 뒤 후보가 자리를 채워 결과 수가 K로 유지되는지
- [x] 3.3 `tests/test_retrieval_service.py` — 레지스트리 단건 조회 수가 `top_k` 이하인지, 같은 문서가 두 번 조회되지 않는지 (호출을 세는 페이크 레지스트리)

## 4. 대상 필터를 합성 키로 (결정 8 / `document-ingestion` 델타)

- [x] 4.1 `core/documents.py::StoredIndexVersion`에 `key` 속성 추가
- [x] 4.2 `adapters/vector_store/chroma.py` — `_metadata`가 `version_key`를 쓰고, `_version_filter`가 `{"version_key": {"$in": [...]}}` 하나를 만든다
- [x] 4.3 `adapters/vector_store/chroma.py` — `version_key`가 없는 청크의 메타데이터를 채우는 보정 연산 추가 (전수 조회 1회, 배치 `update`, 실패는 예외로 올린다)
- [x] 4.4 `services/ingestion.py::_reconcile` — 규칙 1·2 앞에 보정을 부르고, 실패는 삼켜 기동을 계속하되 경고와 건수를 남긴다
- [x] 4.5 `tests/test_vector_store.py` — 대상 목록이 여럿일 때 필터가 조건 하나이고, 대상 밖 청크가 새어 나오지 않는지
- [x] 4.6 `tests/test_vector_store.py`(실물 Chroma) — `version_key` 없이 저장된 청크가 보정 뒤 검색되는지. `tests/test_ingestion_pipeline.py` — 보정이 기동 정리보다 먼저 돌고, 실패해도 기동이 성공하는지
- [x] 4.7 `ARCHITECTURE.md`에 대상 필터의 메타데이터 형태와 기동 보정을 적는다

## 5. 질의 임베딩 1회 (결정 6 / `retrieval` 델타)

- [x] 5.1 `adapters/retrievers/dense.py` — 임베딩 입력을 `normalize_query(query)`로 통일하고, 주어진 벡터가 있으면 그것을 쓴다
- [x] 5.2 `services/retrieval.py::search`에 `query_embedding` 인자 추가, `RetrievalResult`가 이번 검색에 쓰인 벡터를 싣는다
- [x] 5.3 `services/qa.py::prepare` — `CacheSlot`의 벡터를 검색에 넘기고, 검색이 만든 벡터를 슬롯에 접어 넣어 저장 경로가 다시 만들지 않게 한다
- [x] 5.4 `tests/test_qa_cache.py` — 캐시가 비어 있는 요청·비어 있지 않은 요청 모두에서 질의용 임베딩 호출이 정확히 1회인지 (호출을 세는 스텁 임베더)
- [x] 5.5 `tests/test_retrieval_service.py` — 캐시가 벡터를 준 요청과 주지 않은 요청의 결과가 같은지, 결과가 쓴 벡터를 싣는지, 어휘 단독은 벡터를 만들지 않는지. `ARCHITECTURE.md`에 요청당 1회·정규화 통일을 적었다
- [x] 5.6 `docker compose run --build --rm test pytest tests/test_retrieval_quality.py -q` 로 하한 회귀 확인 (정규화 통일로 점수 분포가 움직였는지)

## 6. 캐시 스코프와 극성 게이트 (결정 3·4 / `response-cache` 델타)

- [x] 6.1 `core/cache.py::derive_cache_scope`에서 `top_k`를 뺀다 (`derive_cache_key`는 그대로)
- [x] 6.2 `core/cache.py`에 `negation_polarity` 추가 — 정규화된 질의 하나를 받아 불리언을 낸다
- [x] 6.3 `adapters/protocols.py`의 `ResponseCache` — `store`·`lookup_semantic`에 극성 인자 추가
- [x] 6.4 `services/cache.py` — 조회·저장 양쪽에서 극성을 유도해 넘긴다 (정확 매치 경로는 그대로)
- [x] 6.5 `adapters/cache/memory.py` — 극성이 다른 항목을 후보에서 제외
- [x] 6.6 `tests/test_cache.py` — `negation_polarity`의 표지 목록 (`안` 단독 vs `안내`, `않`·`못`·`없`·`불가`, 영문, 부정 없음)
- [x] 6.7 `tests/test_qa_cache.py` — 긍정/부정 쌍이 L2 히트가 되지 않고, 부정끼리는 히트가 되며, 정확 매치는 영향을 받지 않는지
- [x] 6.8 `tests/test_cache_service.py` — K가 다른 같은 질의가 유사 매치 후보가 되고, 히트 응답에 항목의 K와 근거가 그대로 실리는지. `adapters/cache/store.py`(Redis)도 극성 인자를 받도록 이어 뒀다 — 7장이 벡터셋으로 옮긴다

## 7. L2 후보를 근접 이웃으로 (결정 5 / `response-cache` 델타)

- [x] 7.1 `adapters/cache/store.py` — `KEY_PREFIX`를 `qa:v2`로 올리고 벡터셋 키(`vset:{scope}:{aff|neg}`)를 추가
- [x] 7.2 `store`에서 `VADD`, `_forget`·`_sweep`·용량 정리에서 `VREM`을 함께 보낸다 (살아 있는 벡터셋 이름 목록을 `SCOPES_KEY`가 든다)
- [x] 7.3 `lookup_semantic` — `VSIM ... COUNT n`으로 지문만 받고, 기존 `vec:{fp}` `MGET` + 정확 코사인으로 임계값을 판정 (VSIM 점수는 쓰지 않는다)
- [x] 7.4 `count_candidates` — 해당 극성 벡터셋의 `VCARD`
- [x] 7.5 `config.py` — `cache_semantic_candidates`의 주석을 새 뜻(탐색할 근접 이웃 수)으로 고치고 기본값을 정한다 (design.md 「Open Questions」)
- [x] 7.6 `tests/test_cache_redis.py` (`redis` 마커) — 상한보다 많은 항목을 넣은 뒤에도 오래전에 저장된 이웃이 히트가 되는지, 극성이 다른 항목이 후보에 없는지
- [x] 7.7 `tests/test_response_cache.py` — 인메모리 구현도 근접순으로 후보를 고르도록 고치고 같은 계약(극성 제외·오래된 이웃 도달)을 단언
- [x] 7.8 `tests/test_cache_invalidation.py` — 문서 무효화가 벡터셋에서도 지문을 걷어내는지 (핵심 3경로)
- [x] 7.9 `ARCHITECTURE.md`의 「응답 캐시」에 키 구조(v2·벡터셋 둘)와 후보 선택 방식·판정 위치를 다시 적는다

## 8. 마무리 검증

- [x] 8.1 `docker compose run --build --rm test` 전체 통과
- [x] 8.2 `docker compose run --build --rm test ruff check .` 및 `python3 scripts/check_comments.py` 위반 0건
- [x] 8.3 `docker compose up -d --build --wait` 후 `sample-docs/` 두 건을 올리고 `/qa`를 실물로 확인 — 기동 로그의 `version_key` 보정 건수와 캐시 프로브 결과, 같은 질문 2회의 `cache_hit`, 긍정/부정 쌍이 히트하지 않는 것
- [x] 8.4 `README.md` — Redis 8 요구를 명시하고, 바꾼 이유(유사 매치 후보를 근접 이웃으로)를 기술 선택 이유에 한 줄로 남긴다
- [x] 8.5 `openspec/config.yaml`의 기술 스택 표에서 Redis 항목을 갱신 (버전과 이유)
- [x] 8.6 `openspec validate fix-retrieval-cache-defects --strict` 통과 확인 (아카이브는 커밋 뒤)
