"""어휘 토큰화 — 색인 쓰기와 질의 읽기가 같은 규약을 쓴다.

규약이 갈리면 색인은 오류를 내지 않고 아무것도 매칭하지 않는다. 규칙 넷의 근거는
`openspec/changes/add-rrf-algorithm-spec/design.md` 결정 2 에 있다. 표준 라이브러리만 쓴다.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from typing import Protocol, runtime_checkable

# 규칙(정규화·경계·혼합 토큰·접미)을 고치면 올린다. 서명이 달라져 기존 색인은 stale 이 된다.
TOKENIZER_VERSION = 1

# 어근이 이보다 짧아지면 벗기지 않는다 — `수도` 가 `수` 가 되면 다른 뜻의 토큰이 된다.
MIN_STEM_LENGTH = 2

#: 조사·어미 목록. 형태소 분석이 아니라 규칙이라 과벗김이 나오고, 그래서 원 토큰을 함께 낸다.
DEFAULT_SUFFIXES: tuple[str, ...] = (
    "입니다",
    "습니다",
    "합니다",
    "됩니다",
    "에서",
    "에게",
    "으로",
    "까지",
    "부터",
    "보다",
    "처럼",
    "마다",
    "하는",
    "한다",
    "되는",
    "된다",
    "이다",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "로",
    "와",
    "과",
    "의",
    "도",
    "만",
)

# 밑줄을 뺀 `\w` — `chunk_index` 가 한 토큰으로 남으면 `chunk` 로 찾을 수 없다.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# 음절·자모·호환 자모. 코드포인트로 적는 것은 자모 리터럴이 편집기에서 결합되어 보여서다.
_HANGUL = re.compile("[가-힣ᄀ-ᇿ㄰-㆏]")


@cache
def _longest_first(suffixes: tuple[str, ...]) -> tuple[str, ...]:
    """최장 일치를 위해 길이 내림차순으로 정렬한다. 같은 길이는 사전순으로 고정한다."""
    return tuple(sorted(suffixes, key=lambda suffix: (-len(suffix), suffix)))


def _normalize(text: str) -> str:
    """NFKC 후 소문자화 — 전각·반각과 대소문자 차이가 여기서 사라진다."""
    return unicodedata.normalize("NFKC", text).casefold()


def _is_hangul(character: str) -> bool:
    return _HANGUL.match(character) is not None


def _split_classes(word: str) -> list[str]:
    """한글과 그 밖의 영숫자 경계에서 자른다 — `200만원` → `200`, `만원`.

    라틴·숫자 연속은 한 부류라 `p1` 이 `p` 와 `1` 로 쪼개지지 않는다."""
    pieces: list[str] = []
    start = 0
    for index in range(1, len(word)):
        if _is_hangul(word[index]) != _is_hangul(word[index - 1]):
            pieces.append(word[start:index])
            start = index
    pieces.append(word[start:])
    return pieces


@runtime_checkable
class Tokenizer(Protocol):
    """텍스트를 검색 단위로 자르는 구성. 쓰기와 읽기가 같은 구현을 공유한다."""

    def tokenize(self, text: str) -> tuple[str, ...]: ...

    @property
    def signature_material(self) -> str: ...


@dataclass(frozen=True)
class RuleTokenizer:
    """텍스트를 검색 단위로 자른다.

    구성이 값이라 서명 재료가 되고, 서명이 달라지면 기존 색인이 재수집을 요구한다."""

    version: int = TOKENIZER_VERSION
    suffixes: tuple[str, ...] = DEFAULT_SUFFIXES

    def tokenize(self, text: str) -> tuple[str, ...]:
        """정규화 → 경계 분할 → 혼합 토큰 보존 → 접미 벗기기 순으로 토큰을 낸다.

        같은 토큰이 여러 번 나오는 것은 그대로 둔다 — BM25 의 빈도 항이 그것을 읽는다."""
        tokens: list[str] = []
        for word in _WORD.findall(_normalize(text)):
            pieces = _split_classes(word)
            # 원형만 넣으면 조각으로 못 찾고, 조각만 넣으면 `200` 의 변별력이 사라진다.
            candidates = [word, *pieces] if len(pieces) > 1 else [word]
            for candidate in candidates:
                tokens.append(candidate)
                stem = self.strip_suffix(candidate)
                if stem is not None:
                    tokens.append(stem)
        return tuple(tokens)

    def strip_suffix(self, token: str) -> str | None:
        """조사·어미를 최장 일치로 한 번 벗긴 어근. 벗길 것이 없으면 `None`."""
        for suffix in _longest_first(self.suffixes):
            if token.endswith(suffix):
                stem = token[: -len(suffix)]
                return stem if len(stem) >= MIN_STEM_LENGTH else None
        return None

    @property
    def signature_material(self) -> str:
        """`derive_index_signature` 에 넣을 구성 문자열. 구성이 다르면 값이 달라진다."""
        return json.dumps(
            {
                "version": self.version,
                "suffixes": list(self.suffixes),
                "min_stem_length": MIN_STEM_LENGTH,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


#: 배선이 따로 고르지 않으면 쓰이는 규칙 기반 구성. 쓰기와 읽기가 같은 값을 공유한다.
DEFAULT_TOKENIZER = RuleTokenizer()
