# 어휘 토크나이저 kiwipiepy 교체 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 어휘 검색의 규칙 기반 토크나이저를 kiwipiepy 형태소 분석기로 교체하되, `core/` 순수성과 테스트 속도를 지킨다.

**Architecture:** `core/lexical.py` 에 `Tokenizer` 프로토콜을 두고 기존 규칙 구현을 `RuleTokenizer` 로 남긴다. kiwipiepy 구현은 어댑터(`adapters/tokenizer/kiwi.py`)로 격리해 `core/` 서드파티 금지를 지킨다. `create_app` 이 프로덕션에서 `KiwiTokenizer` 를 배선하고, 테스트 하버스는 `RuleTokenizer` 를 그대로 써 모델 로드를 피한다.

**Tech Stack:** Python 3.11+, FastAPI, kiwipiepy(형태소 분석 + 번들 모델), SQLite FTS5.

## Global Constraints

- **언어/런타임:** Python `>=3.11`, FastAPI. 호스트에서는 실행 불가 — 검증은 컨테이너뿐.
- **검증 명령은 항상 `--build`:** `docker compose run --build --rm test <args>`. `--build` 없으면 직전 이미지의 코드를 검사한다.
- **`core/` 는 표준 라이브러리만.** 서드파티(kiwipiepy) import 는 `adapters/` 에만 둔다.
- **주석 규칙:** `python3 scripts/check_comments.py` 위반 0. 모듈 docstring ≤5줄, 함수 docstring 1~3줄, 인라인은 "왜"만.
- **핵심 3경로 테스트(ingestion·retrieval·캐시 무효화)는 목·스텁으로 구독 없이 실행 가능해야 한다.** 어휘 검색 계약 테스트는 모델이 필요 없는 `RuleTokenizer` 로 돈다.
- **커밋:** 작업한 날은 하루 1회 이상. 커밋 메시지는 짧은 한 줄 요약, 끝에 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **문서-코드 일치:** 문서에 적은 것과 구현이 일치해야 한다. 되돌린 결정은 이유를 남긴다.

---

### Task 1: kiwipiepy 의존성 추가 + 오프라인 번들 실측

**Files:**
- Modify: `pyproject.toml` (dependencies 블록)
- Modify: `Dockerfile` (임베딩 런타임 레이어 뒤)

**Interfaces:**
- Produces: 컨테이너 안에서 `from kiwipiepy import Kiwi; Kiwi()` 가 네트워크 없이 동작. 뒤 태스크의 kiwi 테스트가 이 위에서 돈다.

- [ ] **Step 1: pyproject 에 의존성 추가**

`pyproject.toml` 의 `dependencies` 리스트에서 `pymupdf4llm` 줄 아래에 추가한다.

```toml
    "pymupdf==1.28.0",
    "pymupdf4llm==1.28.0",
    # 어휘 형태소 토크나이저. 모델이 동반 패키지(kiwipiepy_model)로 wheel 에 번들되어
    # 런타임 다운로드가 없다. 토큰이 저장물(index_signature 의 축)이라, 판이 흔들리면
    # 같은 설정에서 같은 문서가 다른 청크가 된다 — chromadb·pymupdf 와 같은 이유로
    # 정확 고정하되, 정확 버전은 Step 4 에서 컨테이너가 실제로 설치한 값으로 박는다.
    "kiwipiepy>=0.18",
```

- [ ] **Step 2: Dockerfile 에 kiwipiepy 레이어 추가**

`Dockerfile` 에서 `RUN pip install --no-cache-dir --extra-index-url ... torch && pip install --no-cache-dir sentence-transformers` 줄 **바로 아래**에 추가한다. 코드(`COPY src`)보다 위라 캐시된다.

```dockerfile
# ── 어휘 토크나이저(kiwipiepy) ──────────────────────────────────────────
#
# 모델이 동반 패키지로 wheel 에 번들되어 런타임 다운로드가 없다. 코드보다 먼저 굳혀
# 캐시하고, 빌드 시점에 한 번 돌려 **오프라인 번들과 prebuilt wheel 을 실측**한다 —
# 별도 다운로드가 필요하면 여기서 드러난다.
RUN pip install --no-cache-dir kiwipiepy \
    && python -c "from kiwipiepy import Kiwi; print(Kiwi().tokenize('워밍업'))"
```

- [ ] **Step 3: 컨테이너 빌드로 번들·wheel 실측**

Run: `docker compose build --no-cache api`
Expected: 빌드 성공. kiwipiepy 설치 로그에 `kiwipiepy_model` 이 함께 잡히고, `python -c` 줄이 네트워크 오류 없이 토큰 리스트를 출력한다. (별도 모델 다운로드 로그가 보이면 그 다운로드를 이 RUN 안으로 옮긴다.)

- [ ] **Step 4: 설치된 정확 버전을 pyproject 에 고정**

Run: `docker compose run --build --rm test python -c "import kiwipiepy; print(kiwipiepy.__version__)"`
Expected: 버전 문자열(예: `0.20.4`) 출력.

출력된 값으로 `pyproject.toml` 의 `"kiwipiepy>=0.18"` 를 `"kiwipiepy==<그 값>"` 으로 바꾼다.

- [ ] **Step 5: 커밋**

```bash
git add pyproject.toml Dockerfile
git commit -m "build: kiwipiepy 의존성 추가 (번들 모델, 오프라인)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `Tokenizer` 프로토콜 + `RuleTokenizer` 개명

**Files:**
- Modify: `src/app/core/lexical.py`
- Modify: `src/app/adapters/lexical/sqlite.py:18` (import 은 그대로, 확인만)
- Modify: `tests/test_lexical_tokenizer.py`
- Modify: `tests/test_ingestion_pipeline.py:27,809`
- Modify: `tests/test_documents.py:33,134,135,221`

**Interfaces:**
- Produces:
  - `Tokenizer` — `typing.Protocol`, 메서드 `tokenize(self, text: str) -> tuple[str, ...]` 과 프로퍼티 `signature_material -> str`.
  - `RuleTokenizer` — 기존 규칙 구현(dataclass, 필드 `version: int`·`suffixes: tuple[str, ...]`, 메서드 `tokenize`·`strip_suffix`·프로퍼티 `signature_material`).
  - `DEFAULT_TOKENIZER: RuleTokenizer` — 변함없이 존재.
- Consumes: 없음.

- [ ] **Step 1: `core/lexical.py` 에 프로토콜 추가 + 개명**

`from dataclasses import dataclass` 아래에 `from typing import Protocol, runtime_checkable` 를 더한다. 클래스 정의 `@dataclass(frozen=True) class Tokenizer:` 위에 프로토콜을 추가하고, 그 dataclass 이름을 `RuleTokenizer` 로 바꾼다. 파일 끝 `DEFAULT_TOKENIZER = Tokenizer()` 를 `DEFAULT_TOKENIZER = RuleTokenizer()` 로 바꾼다.

추가하는 프로토콜(클래스 정의 바로 앞):

```python
@runtime_checkable
class Tokenizer(Protocol):
    """텍스트를 검색 단위로 자르는 구성. 쓰기와 읽기가 같은 구현을 공유한다."""

    def tokenize(self, text: str) -> tuple[str, ...]: ...

    @property
    def signature_material(self) -> str: ...
```

개명(본문은 그대로, 클래스 줄만):

```python
@dataclass(frozen=True)
class RuleTokenizer:
```

파일 끝:

```python
#: 배선이 따로 고르지 않으면 쓰이는 규칙 기반 구성. 쓰기와 읽기가 같은 값을 공유한다.
DEFAULT_TOKENIZER = RuleTokenizer()
```

- [ ] **Step 2: `sqlite.py` 의 어노테이션이 프로토콜을 가리키는지 확인**

`src/app/adapters/lexical/sqlite.py:18` 은 `from app.core.lexical import DEFAULT_TOKENIZER, Tokenizer` 그대로 둔다. 이제 `Tokenizer` 는 프로토콜이고 `tokenizer: Tokenizer = DEFAULT_TOKENIZER` 어노테이션은 그대로 유효하다(런타임 영향 없음). 변경 없음 — 읽어서 확인만 한다.

- [ ] **Step 3: 규칙 구현을 직접 인스턴스화하던 테스트를 `RuleTokenizer` 로**

세 파일의 `Tokenizer(...)` 직접 생성과 import 를 `RuleTokenizer` 로 바꾼다. 프로토콜은 인스턴스화할 수 없으므로 필수다.

`tests/test_lexical_tokenizer.py`:
- import 줄 `from app.core.lexical import DEFAULT_TOKENIZER, MIN_STEM_LENGTH, Tokenizer` → `from app.core.lexical import DEFAULT_TOKENIZER, MIN_STEM_LENGTH, RuleTokenizer, Tokenizer`
- 함수 시그니처 `def tokens(text: str, tokenizer: Tokenizer = DEFAULT_TOKENIZER)` 는 그대로(프로토콜 어노테이션).
- `Tokenizer()` → `RuleTokenizer()` (75줄 2곳)
- `Tokenizer(version=1)`·`Tokenizer(version=2)` → `RuleTokenizer(version=1)`·`RuleTokenizer(version=2)` (78줄)
- `Tokenizer(suffixes=("는", "은"))` → `RuleTokenizer(suffixes=("는", "은"))` (82·84줄)

`tests/test_ingestion_pipeline.py`:
- import 줄 27 `from app.core.lexical import DEFAULT_TOKENIZER, Tokenizer` → `from app.core.lexical import DEFAULT_TOKENIZER, RuleTokenizer`
- 809줄 `tokenizer=Tokenizer(suffixes=(*DEFAULT_TOKENIZER.suffixes, "께서"))` → `tokenizer=RuleTokenizer(suffixes=(*DEFAULT_TOKENIZER.suffixes, "께서"))`

`tests/test_documents.py`:
- import 줄 33 `from app.core.lexical import DEFAULT_TOKENIZER, Tokenizer` → `from app.core.lexical import DEFAULT_TOKENIZER, RuleTokenizer`
- 134·135·221줄 `Tokenizer(...)` → `RuleTokenizer(...)` (동일 인자 유지)

- [ ] **Step 4: 린트 + 주석 규칙 + 영향 테스트 실행**

Run:
```bash
docker compose run --build --rm test ruff check .
docker compose run --build --rm test python3 scripts/check_comments.py
docker compose run --build --rm test pytest tests/test_lexical_tokenizer.py tests/test_documents.py tests/test_ingestion_pipeline.py -q
```
Expected: ruff 통과, 주석 위반 0건, 세 테스트 파일 전부 PASS.

- [ ] **Step 5: 커밋**

```bash
git add src/app/core/lexical.py tests/test_lexical_tokenizer.py tests/test_ingestion_pipeline.py tests/test_documents.py
git commit -m "refactor: Tokenizer 프로토콜 도입, 규칙 구현을 RuleTokenizer 로

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `KiwiTokenizer` 어댑터

**Files:**
- Create: `src/app/adapters/tokenizer/__init__.py`
- Create: `src/app/adapters/tokenizer/kiwi.py`
- Test: `tests/test_kiwi_tokenizer.py`

**Interfaces:**
- Consumes: `Tokenizer` 프로토콜(Task 2), `app.core.exceptions.ConfigurationError`.
- Produces:
  - `KiwiTokenizer(*, content_tags: frozenset[str] = CONTENT_TAGS)` — `tokenize(text) -> tuple[str,...]`, `signature_material -> str`, `async warm_up() -> None`.
  - `CONTENT_TAGS: frozenset[str]` — 유지 태그 집합.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_kiwi_tokenizer.py`:

```python
"""kiwipiepy 어댑터의 계약 — 내용어만 남고 조사·어미·기호는 버려진다.

실물 형태소 분석기라 모델이 필요하다. 검색 계약 테스트(`test_retrieval_*`)는 모델
없는 `RuleTokenizer` 로 돌고, 이 파일만 kiwipiepy 를 실제로 부른다.
"""

import pytest

from app.adapters.tokenizer import CONTENT_TAGS, KiwiTokenizer


@pytest.fixture(scope="module")
def kiwi() -> KiwiTokenizer:
    return KiwiTokenizer()


def test_particles_and_endings_are_dropped(kiwi: KiwiTokenizer):
    result = set(kiwi.tokenize("재택근무를 합니다"))
    assert "를" not in result
    assert "습니다" not in result


def test_a_noun_survives(kiwi: KiwiTokenizer):
    result = set(kiwi.tokenize("재택근무 규정"))
    assert "재택근무" in result or {"재택", "근무"} & result


def test_a_verb_stem_survives(kiwi: KiwiTokenizer):
    assert "승인" in set(kiwi.tokenize("승인되나요"))


def test_latin_is_casefolded(kiwi: KiwiTokenizer):
    assert "pr" in set(kiwi.tokenize("PR 리뷰"))


def test_a_number_survives(kiwi: KiwiTokenizer):
    assert "200" in set(kiwi.tokenize("200만원"))


def test_write_and_read_share_the_convention(kiwi: KiwiTokenizer):
    """색인('교육비 지원')과 질의('교육비는 얼마인가요')가 같은 토큰에 닿는다."""
    assert set(kiwi.tokenize("교육비 지원")) & set(kiwi.tokenize("교육비는 얼마인가요"))


def test_signature_changes_with_tag_set(kiwi: KiwiTokenizer):
    narrowed = KiwiTokenizer(content_tags=frozenset({"NNG"}))
    assert narrowed.signature_material != kiwi.signature_material


def test_signature_names_the_engine(kiwi: KiwiTokenizer):
    assert "kiwipiepy" in kiwi.signature_material
```

- [ ] **Step 2: 실패 확인**

Run: `docker compose run --build --rm test pytest tests/test_kiwi_tokenizer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.adapters.tokenizer'`.

- [ ] **Step 3: 어댑터 구현**

`src/app/adapters/tokenizer/kiwi.py`:

```python
"""kiwipiepy 형태소 토크나이저 — 어휘 색인용 내용어 토큰을 낸다.

내용어 태그만 남기고 조사·어미·기호는 버린다. 규칙 기반에서 넘어온 근거는
`docs/superpowers/specs/2026-08-14-kiwipiepy-tokenizer-design.md` 에 있다.
"""

import asyncio
import json
import logging
import threading
from typing import Any

import kiwipiepy

from app.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

KIWI_TOKENIZER_VERSION = 1

#: 남기는 형태소 태그 — 명사·동사·형용사·어근·외국어·숫자·한자. 조사·어미·기호는 버린다.
CONTENT_TAGS = frozenset({"NNG", "NNP", "VV", "VA", "XR", "SL", "SN", "SH"})

_WARM_UP_TEXT = "워밍업"


class KiwiTokenizer:
    """kiwipiepy 로 형태소를 뽑아 내용어만 토큰으로 낸다."""

    def __init__(self, *, content_tags: frozenset[str] = CONTENT_TAGS) -> None:
        self._content_tags = content_tags
        self._kiwi: Any = None
        # 잠그지 않으면 서로 다른 워커의 첫 호출이 모델을 두 벌 올린다.
        self._load_lock = threading.Lock()

    def tokenize(self, text: str) -> tuple[str, ...]:
        """내용어 형태소만 casefold 해 낸다. 중복은 그대로 둔다 — BM25 의 빈도 항이 읽는다."""
        kiwi = self._ensure_kiwi()
        return tuple(
            token.form.casefold()
            for token in kiwi.tokenize(text)
            if token.tag in self._content_tags
        )

    @property
    def signature_material(self) -> str:
        """`derive_index_signature` 재료. 분석기·판·태그 집합이 바뀌면 값이 달라진다."""
        return json.dumps(
            {
                "tokenizer": "kiwipiepy",
                "version": KIWI_TOKENIZER_VERSION,
                "engine_version": kiwipiepy.__version__,
                "content_tags": sorted(self._content_tags),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    async def warm_up(self) -> None:
        """모델을 미리 올린다. 임베더·리랭커와 같은 자리, 같은 처분이다."""
        await asyncio.to_thread(self.tokenize, _WARM_UP_TEXT)

    def _ensure_kiwi(self) -> Any:
        """Kiwi 인스턴스를 한 번만 만든다. 블로킹이라 스레드풀 안에서 불린다."""
        if self._kiwi is not None:
            return self._kiwi
        with self._load_lock:
            if self._kiwi is None:
                self._kiwi = self._load()
        return self._kiwi

    def _load(self) -> Any:
        """가중치를 올린다. 실패는 도메인 예외로 세운다 — 빈 결과로 위장하지 않는다."""
        try:
            from kiwipiepy import Kiwi
        except ImportError as exc:  # pragma: no cover - 의존성이 빠진 배포에서만 발생
            raise ConfigurationError(
                "어휘 토크나이저 런타임(kiwipiepy)이 설치되어 있지 않습니다"
            ) from exc
        logger.info("kiwipiepy 모델을 로딩합니다")
        return Kiwi()
```

`src/app/adapters/tokenizer/__init__.py`:

```python
"""`Tokenizer` 프로토콜의 kiwipiepy 구현. 서드파티라 `core/` 밖 어댑터에 산다."""

from app.adapters.tokenizer.kiwi import CONTENT_TAGS, KiwiTokenizer

__all__ = ["CONTENT_TAGS", "KiwiTokenizer"]
```

- [ ] **Step 4: 통과 확인 + 주석 규칙**

Run:
```bash
docker compose run --build --rm test pytest tests/test_kiwi_tokenizer.py -q
docker compose run --build --rm test ruff check .
docker compose run --build --rm test python3 scripts/check_comments.py
```
Expected: 8개 테스트 PASS, ruff 통과, 주석 위반 0건.

만약 `test_a_verb_stem_survives` 또는 `test_a_noun_survives` 가 실패하면, 컨테이너에서 실제 태그를 확인해 `CONTENT_TAGS` 를 조정한다:
```bash
docker compose run --build --rm test python -c "from kiwipiepy import Kiwi; print(Kiwi().tokenize('승인되나요 재택근무 200만원'))"
```
스펙 초안 집합(명사·동사·형용사·어근·외국어·숫자·한자) 밖의 태그가 필요하면 그 태그를 더하고, 더한 이유를 커밋 메시지에 남긴다.

- [ ] **Step 5: 커밋**

```bash
git add src/app/adapters/tokenizer/ tests/test_kiwi_tokenizer.py
git commit -m "feat: kiwipiepy 어휘 토크나이저 어댑터

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `create_app` 배선 — 프로덕션은 Kiwi, 테스트는 규칙

**Files:**
- Modify: `src/app/main.py` (import, `create_app` 시그니처·본문, `_lifespan`, 새 `_warm_up_tokenizer`)
- Modify: `tests/conftest.py` (`make_app` 기본 대역에 `RuleTokenizer` 주입)

**Interfaces:**
- Consumes: `KiwiTokenizer`(Task 3), `RuleTokenizer`·`Tokenizer`(Task 2).
- Produces: `create_app(..., tokenizer: Tokenizer | None = None)` — 미주입 시 `KiwiTokenizer()`. `app.state.tokenizer` 에 저장.

- [ ] **Step 1: import 추가**

`src/app/main.py` 상단 import 부에 더한다(기존 `from app.core.lexical import DEFAULT_TOKENIZER` 는 남겨둔다 — 다른 곳이 쓰지 않으면 Step 3 에서 제거).

```python
from app.adapters.tokenizer import KiwiTokenizer
from app.core.lexical import DEFAULT_TOKENIZER, Tokenizer
```

- [ ] **Step 2: `create_app` 시그니처에 `tokenizer` 파라미터 추가**

```python
def create_app(
    settings: Settings | None = None,
    probes: Sequence[HealthProbe] | None = None,
    parsers: Sequence[DocumentParser] | None = None,
    embedder: Embedder | None = None,
    vector_store: VectorStore | None = None,
    lexical_index: LexicalIndex | None = None,
    registry: DocumentRegistry | None = None,
    generator: AnswerGenerator | None = None,
    reranker: Reranker | None = None,
    tokenizer: Tokenizer | None = None,
) -> FastAPI:
```

- [ ] **Step 3: 본문에서 토크나이저를 한 번 고르고 양쪽에 같은 것을 준다**

`if lexical_index is None:` 블록 **앞**에 토크나이저 결정을 넣고, 그 아래 `lexical_index` 생성과 `derive_index_signature` 를 그 인스턴스로 바꾼다.

```python
    # 프로덕션은 kiwipiepy, 주입 시(테스트)는 그 값을 쓴다. 색인과 서명이 같은 인스턴스를
    # 봐야 업로드 직후 문서가 검색된다 — 한 번만 고른다.
    tokenizer = tokenizer or KiwiTokenizer()
    if lexical_index is None:
        lexical_index = SqliteLexicalIndex(
            settings.lexical_index_path,
            tokenizer=tokenizer,
            min_token_rarity=settings.lexical_min_token_rarity,
        )
```

그리고 `derive_index_signature(...)` 의 `tokenizer_signature=DEFAULT_TOKENIZER.signature_material,` 줄을 바꾼다.

```python
        tokenizer_signature=tokenizer.signature_material,
```

`app.state.embedder = embedder` 근처(다른 `app.state.*` 저장부)에 추가한다.

```python
    app.state.tokenizer = tokenizer
```

이제 `DEFAULT_TOKENIZER` 가 `main.py` 에서 더 쓰이지 않으면 Step 1 의 import 를 `from app.core.lexical import Tokenizer` 로 줄인다. Run 으로 확인: `grep -n "DEFAULT_TOKENIZER" src/app/main.py` 가 import 줄 외에 없으면 제거한다.

- [ ] **Step 4: `_lifespan` 에 토크나이저 선로딩 추가 + 함수 정의**

`_lifespan` 안 `await _warm_up_reranker(app)` 아래에 한 줄 추가한다.

```python
    await _warm_up_embedder(app)
    await _warm_up_reranker(app)
    await _warm_up_tokenizer(app)
```

`_warm_up_reranker` 정의 아래에 새 함수를 추가한다. `RuleTokenizer` 는 `warm_up` 이 없으므로 `getattr` 로 건너뛴다 — 테스트 대역이 모델 로드를 물지 않는다.

```python
async def _warm_up_tokenizer(app: FastAPI) -> None:
    """어휘 토크나이저 모델을 미리 올린다. `warm_up` 이 없는 구현(규칙 기반)은 건너뛴다.

    실패해도 뜨는 이유는 지연 로딩이 백스톱이라서다 — 첫 수집에서 다시 시도한다."""
    warm_up = getattr(app.state.tokenizer, "warm_up", None)
    if warm_up is None:
        return
    try:
        await warm_up()
    except Exception as exc:
        logger.warning(
            "어휘 토크나이저 선로딩에 실패했습니다 — 첫 수집 요청에서 다시 시도합니다",
            exc_info=exc,
        )
```

- [ ] **Step 5: conftest 가 테스트 앱에 규칙 토크나이저를 주입하게 한다**

`tests/conftest.py` 의 `make_app` 픽스처에서 `defaults` 딕트에 `RuleTokenizer` 를 더한다. import 도 추가한다.

import 부:
```python
from app.core.lexical import RuleTokenizer
```

`defaults` 딕트(embedder/vector_store 등과 나란히):
```python
    defaults = {
        "settings": settings,
        "probes": healthy_probes,
        "embedder": embedder,
        "vector_store": vector_store,
        "lexical_index": lexical_index,
        "registry": registry,
        "tokenizer": RuleTokenizer(),
    }
```

- [ ] **Step 6: 전체 스위트 실행 (핵심 3경로 포함)**

Run:
```bash
docker compose run --build --rm test pytest -q
docker compose run --build --rm test ruff check .
docker compose run --build --rm test python3 scripts/check_comments.py
```
Expected: 전체 PASS(신규 kiwi 테스트 포함), ruff 통과, 주석 위반 0건. 특히 `test_search_api`·`test_retrieval_*`·`test_cache_invalidation` 이 초록이어야 한다 — 이들은 `RuleTokenizer` 대역으로 돌아 모델 없이 통과한다.

- [ ] **Step 7: 실물 기동 스모크 — 서명이 바뀌어 옛 문서가 stale 이 되는지**

Run:
```bash
docker compose up -d --build --wait
docker compose logs api | grep -i "기동 정리\|stale\|kiwipiepy"
docker compose down
```
Expected: 기동 로그에 kiwipiepy 로딩이 보이고, 기존 `./data` 에 규칙 기반으로 색인된 문서가 있었다면 `기동 정리`(stale) 로그가 나온다. (깨끗한 볼륨이면 정리 로그는 없다 — 그때는 로딩 로그만 확인.)

- [ ] **Step 8: 커밋**

```bash
git add src/app/main.py tests/conftest.py
git commit -m "feat: create_app 이 kiwipiepy 토크나이저를 배선

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 문서 — 되돌린 결정의 이유를 남긴다

**Files:**
- Modify: `ARCHITECTURE.md` (「어휘 색인」 절, 31줄 부근)
- Modify: `openspec/changes/archive/2026-08-04-add-rrf-algorithm-spec/design.md` (결정 2, 45줄 부근)

**Interfaces:**
- Consumes: 없음. Produces: 없음(문서).

- [ ] **Step 1: `ARCHITECTURE.md` 「어휘 색인」에 교체 사실과 이유 추가**

`### 어휘 색인: SQLite FTS5 + BM25` 절에, 토큰화를 설명하는 문단 근처에 다음 취지를 추가한다(기존 규칙 기반 서술이 있으면 교체 표지를 남기고, 실측표는 유효 범위를 표시).

```markdown
**토큰화는 kiwipiepy(형태소 분석)로 한다.** 초기에는 순수 파이썬 규칙 토크나이저였고
(`add-rrf` 결정 2), konlpy·mecab 을 의존성 비용(JVM·별도 사전·플랫폼 의존)으로 기각한
판단이었다. kiwipiepy 는 그 세 비용을 대부분 해소한다 — 순수 pip 설치, 모델이 wheel 에
번들되어 런타임 다운로드가 없고, JVM 이 필요 없다. 규칙 기반의 과벗김을 흡수하던 세 장치
(원형+어근 병기·희소도 하한·RRF 융합) 중 첫째가 형태소 분석에 흡수되어 사라졌다. 나머지
둘은 그대로 유효하다. 규칙 구현(`RuleTokenizer`)은 지우지 않고 테스트 기본값으로 남긴다 —
모델 로드가 없어 핵심 3경로 테스트가 kiwipiepy 설치에 묶이지 않는다. `core/` 서드파티
금지는 kiwipiepy 를 어댑터(`adapters/tokenizer/`)로 격리해 지킨다.
```

- [ ] **Step 2: `add-rrf` 결정 2 에 되돌림 표지**

`### 2. 토큰화는 우리가 한다 …` 결정 제목 아래(또는 결정 끝)에 되돌림 표지를 남긴다. 채택 이유가 아니라 **되돌린 이유**가 다음 사람을 막는다.

```markdown
> **되돌림(2026-08-14):** 이 결정(순수 파이썬 규칙 토큰화)은 `kiwipiepy` 로 교체됐다.
> 기각 근거였던 의존성 비용(JVM·별도 사전·플랫폼 의존)을 kiwipiepy 가 번들 모델 + prebuilt
> wheel 로 해소했기 때문이다. 설계는
> `docs/superpowers/specs/2026-08-14-kiwipiepy-tokenizer-design.md`. 규칙 구현은
> `RuleTokenizer` 로 남아 테스트 기본값으로 쓰인다.
```

- [ ] **Step 3: 주석 규칙 확인(문서는 대상 아님이지만 습관적으로)**

Run: `docker compose run --build --rm test python3 scripts/check_comments.py`
Expected: 위반 0건(문서 변경은 영향 없음).

- [ ] **Step 4: 커밋**

```bash
git add ARCHITECTURE.md openspec/changes/archive/2026-08-04-add-rrf-algorithm-spec/design.md
git commit -m "docs: 토크나이저 kiwipiepy 교체 반영 (결정 2 되돌림 근거)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- 계층(프로토콜 + RuleTokenizer + 어댑터 격리) → Task 2·3. ✓
- 내용 형태소만(POS 필터) → Task 3 `CONTENT_TAGS`. ✓
- 재색인(signature_material 에 engine_version + 태그 집합) → Task 3·4. ✓
- 의존성·빌드(번들 실측, 오프라인) → Task 1. ✓
- 에러 핸들링(조용한 폴백 없음, 도메인 예외) → Task 3 `_load`. ✓
- 테스트(계약은 RuleTokenizer, kiwi 전용 별도) → Task 3·4(conftest). ✓
- 문서(ARCHITECTURE + 결정 2 되돌림) → Task 5. ✓
- 동시성/POS 경계 검증 → Task 1 Step 3·4, Task 3 Step 4 에 실측 단계로 반영. ✓

**Placeholder scan:** kiwipiepy 정확 버전만 Task 1 Step 4 에서 실측해 박는다(구체적 실행 단계, 플레이스홀더 아님). 그 외 없음.

**Type consistency:** `Tokenizer`(프로토콜) / `RuleTokenizer`(구현) / `KiwiTokenizer`(구현) / `DEFAULT_TOKENIZER: RuleTokenizer` / `create_app(tokenizer: Tokenizer | None)` / `app.state.tokenizer` — 태스크 간 이름·시그니처 일치. `tokenize(text)->tuple[str,...]`·`signature_material->str`·`warm_up()` 이 세 곳에서 같은 형태. ✓
