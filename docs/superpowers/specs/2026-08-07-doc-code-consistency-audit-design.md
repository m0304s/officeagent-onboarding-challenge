# 문서-코드 일치 감사 설계

평가 산출물 4종(`README.md` · `ARCHITECTURE.md` · `PROMPT_DESIGN.md` · `tests/README.md`)의
서술이 현재 소스와 어긋나는 자리를 찾아 문서를 코드에 맞춘다.

## 왜 지금인가

과제 불합격 기준에 "문서에 구현했다고 적었으나 해당 코드가 없음(허위 기재)"이 있다.
드리프트는 최근 변경을 따라온다 — 착수 전 표본 조사에서 `README.md:652`가
`APP_CACHE_SEMANTIC_CANDIDATES` 기본값을 `200`으로 적고 있으나 `src/app/config.py:156`은
`20`이었다. `fix-retrieval-cache-defects`가 값을 내리면서 README를 남겨 둔 자리다.

## 범위

대상은 평가 산출물 4종. `openspec/`·`retros/`·`CLAUDE.md`·`docs/`는 이번 범위 밖이다.
`retros/`는 과거 시점의 기록이라 현재 코드와 어긋나는 것이 정상이고, `openspec/specs/`는
평가자가 보는 문서가 아니다.

## 방법 — 2패스

### Pass 1: 기계적 전수 대조

문서의 주장 중 코드가 정답을 갖고 있는 클래스만 뽑아 대조한다. 클래스마다 오라클을
하나로 고정해 판정 근거가 파일에 있게 한다.

| 주장 클래스 | 오라클 |
|---|---|
| 설정 기본값 | `src/app/config.py` |
| 라우트·메서드·상태코드 | `src/app/api/routes/*.py` |
| 응답 필드명과 열거값 | `src/app/core/models.py` |
| SSE 이벤트명 | `src/app/api/sse.py`, `src/app/api/routes/qa.py` |
| 오류 코드 → HTTP 매핑 | `src/app/core/exceptions.py`, `src/app/api/errors.py` |
| 서비스명·포트·프로필·이미지 | `docker-compose.yml` |
| 테스트 인벤토리 | `tests/` 실제 파일과 `def test_` |

대조 스크립트는 스크래치패드에 두고 리포에 남기지 않는다 — 일회성 감사 도구가 리포에
남으면 다음 사람이 유지보수 대상으로 오해한다.

### Pass 2: 표적 정독

정독 대상을 두 축으로 좁힌다.

- 최근 변경 축 — 마지막 3개 change(`fix-retrieval-cache-defects`·`add-cross-encoder`·
  `add-pymupdf4llm-parser`)가 건드린 소스를 서술하는 절.
- 배점 축 — ARCHITECTURE 「검색 파이프라인」·「답변 생성」·「응답 캐시」, README
  「현재 구현 범위」 표와 검색·답변·캐시 API 절.

`PROMPT_DESIGN.md`는 258줄뿐이라 전수 정독하고 `src/app/core/prompting.py`와 나란히 본다.
프롬프트 문서는 실제 문자열과 어긋나기 가장 쉬운 자리다.

## 수정 정책

- 코드를 진실로 본다. 문서를 고친다.
- 수치 하나를 고치면 그 수치를 논거로 쓴 문장까지 함께 고친다. 표만 고치면 불일치가
  산문에 남는다.
- 코드 쪽이 결함으로 보이면 고치지 않고 보고한다 — 감사 커밋에 동작 변경을 섞지 않는다.
- 커밋은 하지 않는다.

## 판정할 수 없는 것

ARCHITECTURE의 실측값(검색 품질표, 지연 수치, 점수 분포)은 재현 없이 진위를 가릴 수 없다.
코드와 대조 가능한 것은 그 수치가 설정 기본값으로 반영된 부분뿐이다. 나머지는 대장에
미판정으로 남기고 손대지 않는다 — 재현 없이 고치면 없는 근거를 지어내는 것이 된다.

## 검증

- 수정 항목마다 재-grep으로 잔존 0건 확인.
- `docker compose run --build --rm test` 1회 — 테스트 개수·통과 주장을 실측으로 확인.
- `python3 scripts/check_comments.py` 로 위반 0건 유지 확인.
