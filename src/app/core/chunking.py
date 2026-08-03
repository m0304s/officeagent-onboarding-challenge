"""청킹 전략.

분할 전략은 설정 선택지다. 다만 **실제로 구현된 전략만** 값으로 존재한다 —
구현되지 않은 이름을 설정으로 고를 수 있게 두면 "설정에 있으니 지원한다"는
허위 기재가 되기 때문이다. 다음 후보(부모-자식 청킹)는 검색 경로가 생긴 뒤,
같은 열거형에 값을 더하고 `CHUNK_SPLITTERS`에 함수를 등록하는 방식으로 붙는다.

분할이 지키는 불변식은 넷이다.

1. `chunk.text == segment_text[char_start:char_end]` — 청크 본문은 언제나 원문의
   부분 문자열이다. 정규화·재작성을 하지 않으므로 출처가 원문을 가리킨다.
2. 모든 청크 길이 ≤ 설정된 상한.
3. 원문의 모든 비공백 문자가 어느 청크엔가 들어간다 — 경계에서 사라지는 내용이 없다.
4. 인접 청크의 원문 구간이 겹친다.
5. 청크는 앞으로 나아간다 — 앞 청크에 통째로 담기는 청크가 없다.

4번의 예외는 하나뿐이다. 겹침 구간이 통째로 공백이면 양쪽 청크의 공백을 다듬은 뒤
겹침이 사라질 수 있다. 그 경우 겹쳐서 보존할 문맥 자체가 없으므로 문제가 되지 않는다.

5번은 나머지 넷이 잡아 주지 않는다. `['사내 복리후생 안내', '생 안내', '내']` 같은
결과는 1~4번을 **전부 만족**하면서 쓸모없는 청크를 인덱스에 넣는다.

`core/`는 표준 라이브러리만 쓴다.
"""

from collections.abc import Callable, Sequence
from enum import StrEnum

from app.core.documents import ChunkLocation, TextChunk, TextSegment

# 분할 알고리즘이 만들어내는 **경계**가 몇 번째 판인가.
#
# 구분자 우선순위·겹침 계산 등 청크 경계가 달라지는 수정을 하면 이 값을 올린다.
# 설정값(크기·겹침)은 그대로인데 경계만 달라지는 변경을 잡을 다른 수단이 없다.
# 이 값은 `index_signature`의 재료이므로(app.core.documents), 올리는 순간
# 기존 색인이 stale 로 표시되고 재업로드 시 재색인된다. 올리지 않으면 구 경계
# 청크와 신 경계 청크가 한 인덱스에 섞인다.
#
# 자동 유도(소스 해시)를 쓰지 않는 이유: 주석 한 줄 수정에도 반응해 무의미한
# 전면 재색인을 부른다. 규율에 의존하는 지점임을 인정한다.
CHUNK_STRATEGY_VERSION = 1


class ChunkStrategy(StrEnum):
    """구현된 분할 전략.

    설정 `APP_CHUNK_STRATEGY`가 이 값 중 하나여야 하며, 그 밖의 값은 기동을 막는다.
    열거형으로 두면 받아들여지는 값 목록이 검증 오류 메시지에 그대로 실린다.
    """

    RECURSIVE = "recursive"


#: 분할 함수의 공통 시그니처. 전략을 더할 때 이 모양을 지킨다.
Splitter = Callable[[Sequence[TextSegment], int, int], tuple[TextChunk, ...]]

# 청크 경계로 삼을 구분자, 우선순위 순.
#
# 문단 → 줄 → 한국어 문장 종결 → 영어 문장 종결 → 공백. 마지막 수단인 "문자"는
# 목록에 없다 — 어떤 구분자도 못 찾으면 상한에서 그대로 자르는 것이 곧 문자 분할이다.
#
# "니다. "는 "다. "에 포함되므로 따로 두지 않는다. 같은 자리에서 끊긴다.
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", "다. ", "요. ", ". ", " ")


def _split_units(
    text: str, start: int, end: int, budget: int, level: int = 0
) -> list[tuple[int, int]]:
    """구간 `[start, end)` 를 `budget` 이하 조각들로 **재귀적으로** 쪼갠다.

    문자열이 아니라 **구간을 재귀한다.** 조각을 잘라 내려가면 각 조각이 원문 어디에서
    왔는지 따로 추적해야 하고, 그 추적이 어긋나는 순간 출처 표기가 조용히 틀어진다.
    구간을 넘기면 오프셋이 재귀 인자 그 자체라 어긋날 여지가 없다.

    현재 구분자로 조각이 하나뿐이면(= 이 구간에 그 구분자가 없으면) 다음 우선순위로
    내려간다. 구분자를 다 써도 여전히 크면 마지막 수단으로 상한에서 그대로 자른다 —
    공백 없는 긴 토큰이 그 경우다.

    조각은 `[start, end)` 를 **빈틈없이 이어 덮는다**. 이 성질이 뒤 단계(합치기)에서
    "원문의 어떤 문자도 잃지 않는다"를 공짜로 만들어 준다.
    """
    if end - start <= budget:
        return [(start, end)]

    if level >= len(_SEPARATORS):
        return [(offset, min(offset + budget, end)) for offset in range(start, end, budget)]

    separator = _SEPARATORS[level]
    pieces: list[tuple[int, int]] = []
    cursor = start
    position = text.find(separator, start, end)
    while position != -1:
        # 구분자는 **앞** 조각에 붙인다. 그래야 청크 본문이 원문의 부분 문자열로
        # 유지되고, 뒤 조각이 구분자(공백·줄바꿈)로 시작하지 않는다.
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
    """연속한 조각을 `budget` 까지 **합친다**.

    이 단계가 없으면 구조 경계가 이른 자리에 하나만 있을 때 그 자리에서 끊겨 10자짜리
    청크가 나온다. 자르기만 하고 합치지 않으면 상한은 지켜지지만 청크가 잘게 부서진다.

    조각이 빈틈없이 이어지므로 결과도 빈틈없이 이어진다 — `core[i].end == core[i+1].start`.
    """
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
    """청크 앞뒤의 공백을 구간에서 덜어낸다.

    본문만 다듬고 오프셋을 그대로 두면 `text == source[start:end]` 가 깨진다.
    구간 자체를 좁혀야 위치와 본문이 계속 일치한다. 공백뿐이면 청크를 만들지 않는다.
    """
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if end > start else None


def _split_segment(segment: TextSegment, size: int, overlap: int) -> list[TextChunk]:
    """세그먼트 하나를 청크로 나눈다. 세그먼트 경계를 넘지 않는다.

    세 단계다 — **쪼개고**(`_split_units`), **합치고**(`_merge_units`), 겹침을 붙인다.

    합치기 단계가 `size` 가 아니라 `size - overlap` 까지만 합치는 이유: 뒤에서 앞으로
    `overlap` 만큼 늘려 붙일 자리를 남겨 둬야 최종 청크가 상한을 넘지 않는다. 즉 청크
    하나가 새로 담는 내용은 최대 `size - overlap` 이고, 나머지는 앞 청크와 겹친다.

    겹침을 조각 단위가 아니라 **문자 단위로 뒤에서 당겨** 붙인다. 조각 단위로 하면
    마지막 조각이 겹침보다 길 때 겹침이 통째로 사라진다 — 문단 하나가 조각 하나인
    문서에서 바로 그렇게 된다.
    """
    text = segment.text
    if not text:
        return []

    if len(text) <= size:
        # 상한 안에 들어가면 자를 이유가 없다. budget(= size - overlap) 으로 내려가
        # 불필요하게 쪼개지는 것을 막는다.
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

        # 겹침을 당기는 폭이 앞 청크보다 크면 앞 청크가 통째로 삼켜진다. 짧은 줄
        # 하나가 긴 문단 둘 사이에 낀 문서에서 실제로 일어난다 — 같은 내용이 두 번
        # 저장되고 검색 결과에도 두 번 나온다. 시작이 반드시 전진하게 막는다.
        start = max(start, previous_start + 1)

        span = _trim(text, start, core_end)
        if span is None:
            continue
        trimmed_start, trimmed_end = span

        # 다듬고 나서 끝이 전진하지 못했다면 앞 청크에 없는 내용이 없다는 뜻이다.
        # 코어 사이가 통째로 공백일 때 이렇게 된다.
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
    """구조 경계 우선 분할.

    **청크는 세그먼트(= PDF 쪽) 경계를 넘지 않는다.** 두 쪽에 걸친 청크가 생기면
    "이 청크는 몇 쪽인가"에 답이 둘이 되어 출처 표기가 모호해진다. 대가는 짧은 쪽에서
    짧은 청크가 나온다는 것인데, 출처 정확성이 더 중요하다.
    """
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

    설정 로딩이 이미 열거형으로 값을 걸러내므로 여기까지 미등록 이름이 오면 그건
    설정 문제가 아니라 열거형에 값만 더하고 함수 등록을 빠뜨린 것이다.
    """
    try:
        return CHUNK_SPLITTERS[strategy]
    except KeyError as exc:  # pragma: no cover - 등록 누락은 개발 시점 실수다
        raise ValueError(
            f"전략 '{strategy}' 에 분할 함수가 등록되어 있지 않습니다 — "
            f"등록된 전략: {sorted(s.value for s in CHUNK_SPLITTERS)}"
        ) from exc


def resplit(chunk: TextChunk, *, size: int, overlap: int) -> tuple[TextChunk, ...]:
    """이미 만들어진 청크를 더 작게 쪼갠다.

    토큰 가드(임베딩 직전)가 부른다. 청크 크기는 문자 기준이라 토큰 수를 보장하지
    못하고, 모델 입력 창을 넘는 청크를 그대로 넘기면 뒷부분이 **조용히 잘린다** —
    저장은 됐는데 검색되지 않는 텍스트가 생기는 최악의 형태다.

    원문 오프셋은 원래 청크의 시작을 기준으로 다시 계산되므로, 재분할 후에도
    출처는 원문의 같은 자리를 가리킨다.
    """
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
    """줄어든 청크 크기에 맞는 겹침. `resplit` 에 넘기기 전에 쓴다.

    토큰 가드는 크기를 반씩 줄여가며 다시 쪼개는데, 설정된 겹침이 그대로면 어느
    순간 겹침이 크기 이상이 되어 `resplit` 이 거절한다. 호출부가 그때마다 손으로
    맞추면 **`_validate` 가 지키는 불변식을 아는 곳이 둘**이 된다 — 한쪽은 거절하고
    한쪽은 조용히 맞추므로, 불변식이 바뀌면 맞추는 쪽이 소리 없이 낡는다.

    돌려주는 값은 언제나 `0 < 반환값 < size` 다. 그래야 인접 청크가 겹치면서도
    분할이 전진한다.
    """
    # 겹침이 1 이상이면서 크기보다 작으려면 크기가 최소 2 다. 호출부가 재분할 바닥값을
    # 두므로 실제로 닿지는 않지만, 조용히 무효한 값을 돌려주는 것보다 낫다.
    if size < 2:
        raise ValueError("청크 크기가 2 미만이면 겹치면서 전진하는 분할이 불가능하다")
    return max(1, min(preferred, size - 1))


def _validate(size: int, overlap: int) -> None:
    if size <= 0:
        raise ValueError("청크 크기는 1 이상이어야 한다")
    if overlap <= 0:
        # 0 을 허용하면 인접 청크가 전혀 겹치지 않는다. 설정 계층(`Settings`)이 이미
        # 막지만, 이 함수는 설정을 거치지 않고도 호출되는 공개 계약이라 여기서도 막는다.
        # 한쪽만 막으면 "겹친다"는 계약이 호출 경로에 따라 달라진다.
        raise ValueError("겹침은 1 이상이어야 한다")
    if overlap >= size:
        raise ValueError("겹침이 청크 크기 이상이면 분할이 전진하지 않는다")
