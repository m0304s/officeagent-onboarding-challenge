"""일반 텍스트·마크다운 파서.

마크다운을 별도 파서로 두지 않는다. 마크다운은 그 자체가 사람이 읽는 텍스트로 설계된
포맷이라, 문법을 걷어내고 본문만 남기면 오히려 정보가 준다 — `## 배포 프로세스` 라는
제목이 `배포 프로세스` 가 되면 그것이 제목이었다는 사실이 사라지고, 표는 열 구분이 사라져
행이 뭉갠다. 검색 대상은 원문 그대로가 낫다. 응답의 `format` 값만 `txt` 와 `md` 로 갈린다.
"""

import codecs

from app.adapters.parsers.normalization import normalize_newlines
from app.core.documents import DocumentFormat, ExtractedDocument, TextSegment
from app.core.exceptions import DocumentParseError

# BOM 이 있으면 인코딩이 확정된다 — 추측할 필요가 없다. 긴 것부터 본다.
# UTF-8 BOM 은 UTF-16 BOM 의 접두사가 아니지만, UTF-32 를 추가하게 되면 순서가 중요해진다.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

# BOM 이 없을 때 시도하는 순서.
#
# `cp949` 를 두는 이유는 한국어 문서다. 윈도우 메모장이 오래 쓰던 인코딩이라 실무 문서에
# 흔하고, UTF-8 로 읽으면 디코딩 자체가 실패한다. 반대 순서로 두면 안 된다 — cp949 는
# UTF-8 로 인코딩된 한글 바이트열도 (뜻 없는 글자로) 디코딩해내는 경우가 있어, 먼저 걸면
# 멀쩡한 UTF-8 문서가 깨진 글자로 저장된다. 실패가 조용해지는 방향이라 더 나쁘다.
_ENCODINGS: tuple[str, ...] = ("utf-8", "cp949")


class TextParser:
    """`.txt` · `.md` 를 세그먼트 하나로 추출한다.

    쪽 개념이 없으므로 `page` 는 비우고 `page_count` 도 `None` 이다. 그 결과 추출이 비면
    `page_count` 가 없는 채로 세그먼트가 0개가 되어, 스캔본 PDF(쪽은 있고 텍스트만 없음)와
    자연히 구분된다.
    """

    formats = frozenset({DocumentFormat.TXT, DocumentFormat.MD})

    def parse(self, data: bytes) -> ExtractedDocument:
        text = normalize_newlines(_decode(data))
        segments = (TextSegment(text=text),) if text.strip() else ()
        return ExtractedDocument(segments=segments)


def _decode(data: bytes) -> str:
    """바이트를 텍스트로 바꾼다. 어느 후보로도 읽히지 않으면 파싱 실패다.

    `errors="replace"` 로 넘기지 않는 이유: 깨진 글자가 섞인 문서가 그대로 색인되면
    검색은 되는데 답변에 인용된 근거가 읽을 수 없는 문자열이 된다. 실패를 조용히
    삼키는 대신 422 로 돌려보내는 편이 낫다.
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return _decode_with(data, encoding)

    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise DocumentParseError(
        "텍스트 인코딩을 판별할 수 없습니다 (UTF-8 또는 CP949 로 저장해 주세요)"
    )


def _decode_with(data: bytes, encoding: str) -> str:
    """BOM 으로 인코딩이 확정된 경우. 여기서 실패하면 내용이 손상된 것이다."""
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise DocumentParseError("텍스트를 읽을 수 없습니다 (내용이 손상되었습니다)") from exc
