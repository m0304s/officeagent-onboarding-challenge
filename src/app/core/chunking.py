"""청킹 전략.

구현된 전략만 값으로 존재한다 — 없는 이름을 고를 수 있으면 허위 기재가 된다.
분할이 지키는 불변식 다섯은 `ARCHITECTURE.md` 문서 청킹에 있다. 표준 라이브러리만 쓴다.
"""

from collections.abc import Callable, Sequence
from enum import StrEnum

from app.core.documents import ChunkLocation, TextChunk, TextSegment

# 경계가 달라지는 수정을 하면 올린다 — 설정값은 그대로인데 경계만 달라지는 변경을
# 잡을 다른 수단이 없다. 소스 해시로 자동 유도하면 주석 수정에도 전면 재색인이 돈다.
CHUNK_STRATEGY_VERSION = 1


class ChunkStrategy(StrEnum):
    """구현된 분할 전략. 열거형이라 허용 값 목록이 검증 오류 메시지에 실린다."""

    RECURSIVE = "recursive"


#: 분할 함수의 공통 시그니처. 전략을 더할 때 이 모양을 지킨다.
Splitter = Callable[[Sequence[TextSegment], int, int], tuple[TextChunk, ...]]

# 우선순위 순. "문자"가 목록에 없는 것은 어떤 구분자도 못 찾으면 상한에서 그대로
# 자르는 것이 곧 문자 분할이기 때문이다.
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", "다. ", "요. ", ". ", " ")


def _split_units(
    text: str, start: int, end: int, budget: int, level: int = 0
) -> list[tuple[int, int]]:
    """구간 `[start, end)` 를 `budget` 이하 조각들로 재귀적으로 쪼갠다.

    구간을 재귀하는 이유는 오프셋이 재귀 인자 그 자체가 되어 어긋날 여지가 없어서다."""
    if end - start <= budget:
        return [(start, end)]

    if level >= len(_SEPARATORS):
        return [(offset, min(offset + budget, end)) for offset in range(start, end, budget)]

    separator = _SEPARATORS[level]
    pieces: list[tuple[int, int]] = []
    cursor = start
    position = text.find(separator, start, end)
    while position != -1:
        # 구분자를 앞 조각에 붙여야 뒤 조각이 공백·줄바꿈으로 시작하지 않는다.
        boundary = position + len(separator)
        pieces.append((cursor, boundary))
        cursor = boundary
        position = text.find(separator, cursor, end)
    if cursor < end:
        pieces.append((cursor, end))

    if len(pieces) <= 1:
        return _split_units(text, start, end, budget, level + 1)

    units: list[tuple[int, int]] = []
    for piece_start, piece_end in pieces:
        if piece_end - piece_start > budget:
            units.extend(_split_units(text, piece_start, piece_end, budget, level + 1))
        else:
            units.append((piece_start, piece_end))
    return units


def _merge_units(units: list[tuple[int, int]], budget: int) -> list[tuple[int, int]]:
    """연속한 조각을 `budget` 까지 합친다.

    없으면 구조 경계가 이른 자리에 하나만 있을 때 10자짜리 청크가 나온다."""
    cores: list[tuple[int, int]] = []
    core_start, core_end = units[0]
    for unit_start, unit_end in units[1:]:
        if unit_end - core_start <= budget:
            core_end = unit_end
        else:
            cores.append((core_start, core_end))
            core_start, core_end = unit_start, unit_end
    cores.append((core_start, core_end))
    return cores


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    """청크 앞뒤 공백을 구간째로 덜어낸다.

    본문만 다듬으면 `text == source[start:end]` 가 깨진다. 공백뿐이면 청크가 없다."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if end > start else None


def _split_segment(segment: TextSegment, size: int, overlap: int) -> list[TextChunk]:
    """세그먼트 하나를 청크로 나눈다 — 쪼개고, 합치고, 겹침을 붙인다.

    `size - overlap` 까지만 합치는 것은 겹침을 붙일 자리를 남기려는 것이다."""
    text = segment.text
    if not text:
        return []

    if len(text) <= size:
        # budget 으로 내려가 불필요하게 쪼개지는 것을 막는다.
        cores = [(0, len(text))]
    else:
        budget = size - overlap
        units = _split_units(text, 0, len(text), budget)
        cores = _merge_units(units, budget)

    chunks: list[TextChunk] = []
    previous_start = -1
    previous_end = -1

    for index, (core_start, core_end) in enumerate(cores):
        # 첫 청크 앞에는 겹칠 것이 없다.
        start = core_start if index == 0 else max(core_start - overlap, 0)

        # 겹침 폭이 앞 청크보다 크면 앞 청크가 통째로 삼켜져 같은 내용이 두 번
        # 저장된다. 시작이 전진하게 막는다.
        start = max(start, previous_start + 1)

        span = _trim(text, start, core_end)
        if span is None:
            continue
        trimmed_start, trimmed_end = span

        # 끝이 전진하지 못했다면 앞 청크에 없는 내용이 없다는 뜻이다.
        if trimmed_end <= previous_end:
            continue

        chunks.append(
            TextChunk(
                text=text[trimmed_start:trimmed_end],
                location=ChunkLocation(
                    char_start=trimmed_start,
                    char_end=trimmed_end,
                    page=segment.page,
                ),
            )
        )
        previous_start, previous_end = trimmed_start, trimmed_end

    return chunks


def split_recursive(
    segments: Sequence[TextSegment],
    size: int,
    overlap: int,
) -> tuple[TextChunk, ...]:
    """구조 경계 우선 분할. 청크는 세그먼트 경계를 넘지 않는다.

    두 쪽에 걸치면 "몇 쪽인가"에 답이 둘이 되어 출처가 모호해진다."""
    _validate(size, overlap)
    chunks: list[TextChunk] = []
    for segment in segments:
        chunks.extend(_split_segment(segment, size, overlap))
    return tuple(chunks)


#: 전략 이름 → 분할 함수. 새 전략은 여기 한 줄로 붙는다.
CHUNK_SPLITTERS: dict[ChunkStrategy, Splitter] = {
    ChunkStrategy.RECURSIVE: split_recursive,
}


def get_splitter(strategy: ChunkStrategy) -> Splitter:
    """전략 이름으로 분할 함수를 얻는다.

    여기까지 미등록 이름이 오면 설정이 아니라 함수 등록을 빠뜨린 것이다."""
    try:
        return CHUNK_SPLITTERS[strategy]
    except KeyError as exc:  # pragma: no cover - 등록 누락은 개발 시점 실수다
        raise ValueError(
            f"전략 '{strategy}' 에 분할 함수가 등록되어 있지 않습니다 — "
            f"등록된 전략: {sorted(s.value for s in CHUNK_SPLITTERS)}"
        ) from exc


def resplit(chunk: TextChunk, *, size: int, overlap: int) -> tuple[TextChunk, ...]:
    """이미 만들어진 청크를 더 작게 쪼갠다 — 토큰 가드가 부른다.

    크기가 문자 기준이라 토큰 수를 보장하지 못하고, 넘기면 뒷부분이 조용히 잘린다."""
    _validate(size, overlap)
    base = chunk.location.char_start
    pieces = _split_segment(TextSegment(text=chunk.text, page=chunk.location.page), size, overlap)
    return tuple(
        TextChunk(
            text=piece.text,
            location=ChunkLocation(
                char_start=base + piece.location.char_start,
                char_end=base + piece.location.char_end,
                page=chunk.location.page,
            ),
        )
        for piece in pieces
    )


def clamp_overlap(*, size: int, preferred: int) -> int:
    """줄어든 청크 크기에 맞는 겹침. 언제나 `0 < 반환값 < size` 다.

    호출부가 손으로 맞추면 `_validate` 의 불변식을 아는 곳이 둘이 되어 한쪽이 낡는다."""
    # 조용히 무효한 값을 돌려주는 것보다 낫다. 호출부의 바닥값 덕에 실제로 닿지는 않는다.
    if size < 2:
        raise ValueError("청크 크기가 2 미만이면 겹치면서 전진하는 분할이 불가능하다")
    return max(1, min(preferred, size - 1))


def _validate(size: int, overlap: int) -> None:
    if size <= 0:
        raise ValueError("청크 크기는 1 이상이어야 한다")
    if overlap <= 0:
        # 설정을 거치지 않는 호출 경로가 있어 여기서도 막는다 — 한쪽만 막으면
        # "겹친다"는 계약이 경로에 따라 달라진다.
        raise ValueError("겹침은 1 이상이어야 한다")
    if overlap >= size:
        raise ValueError("겹침이 청크 크기 이상이면 분할이 전진하지 않는다")
