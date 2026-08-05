"""테스트용 PDF 를 그 자리에서 만든다.

바이너리를 커밋하면 무엇이 들어 있는지 diff 로 볼 수 없고 텍스트 레이어의 유무를 파일
이름으로만 주장하게 된다. 만드는 데도 PyMuPDF 를 쓰는 근거는 `tests/README.md` 에 있다.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import pymupdf

#: 텍스트 레이어 없는 쪽을 뜻하는 표식. `None` 을 쓰면 호출부에서 "빈 문자열과 뭐가
#: 다른가"를 매번 되짚게 되어, 이름을 붙였다.
BLANK_PAGE = None

#: 내장 한국어 CJK 폰트. 기본 폰트로 한글을 그리면 글리프가 없어 추출 결과가 깨지고,
#: 파서가 아니라 픽스처 탓에 실패하는 테스트가 된다.
_FONT = "korea"


def make_pdf(pages: Sequence[str | None]) -> bytes:
    """쪽마다 텍스트를 넣은 PDF 를 만든다. `BLANK_PAGE` 는 텍스트 레이어가 없는 쪽이다.

    빈 쪽에 도형을 그리는 이유는 완전히 비면 쪽 자체가 없는 것처럼 다뤄지기 때문이다."""
    document = pymupdf.open()
    try:
        for content in pages:
            page = document.new_page()
            if content is BLANK_PAGE:
                page.draw_rect(pymupdf.Rect(72, 72, 300, 300), color=(0, 0, 0))
            else:
                page.insert_text(pymupdf.Point(72, 100), content, fontsize=11, fontname=_FONT)
        return document.tobytes()
    finally:
        document.close()


#: 조판 크기 세 가지. 헤딩 판정이 폰트 크기 분포로 갈리므로 넉넉히 벌린다 — 붙여 놓으면
#: 두 제목이 같은 레벨로 접히고, 본문까지 제목으로 승격된다(실측).
_BODY_SIZE = 11
_SUBHEADING_SIZE = 17
_HEADING_SIZE = 28

_TABLE_LEFT = 72.0
_TABLE_SPLIT = 260.0
_TABLE_RIGHT = 440.0
_ROW_HEIGHT = 26.0


@dataclass(frozen=True)
class StructuredPage:
    """구조가 있는 PDF 한 쪽의 원문. 어느 문자열이 몇 쪽에 있었는지를 테스트가 여기서 읽는다."""

    heading: str
    subheading: str
    body: tuple[str, ...]
    table: tuple[tuple[str, str], ...]


#: 본문 줄 수를 표 셀과 합쳐 제목보다 많게 둔다 — 가장 흔한 크기가 본문으로 판정된다.
STRUCTURED_PAGES: tuple[StructuredPage, ...] = (
    StructuredPage(
        heading="복리후생 안내",
        subheading="교육비 지원",
        body=(
            "회사는 직무와 관련된 교육 과정의 수강료를 지원합니다.",
            "지원을 받으려면 수강 시작 전에 팀장 승인을 받아야 합니다.",
            "수료하지 못한 과정의 비용은 환수 대상이 됩니다.",
        ),
        table=(("항목", "금액"), ("교육비", "연 200만원"), ("도서비", "월 5만원")),
    ),
    StructuredPage(
        heading="근무 제도",
        subheading="재택근무",
        body=(
            "재택근무는 주 2회까지 신청할 수 있습니다.",
            "신청은 전주 금요일까지 사내 시스템에 등록합니다.",
            "장비 지원금은 입사 후 1회에 한해 지급됩니다.",
        ),
        table=(("구분", "한도"), ("재택근무", "주 2회"), ("장비 지원금", "50만원")),
    ),
)


def make_structured_pdf(pages: Sequence[StructuredPage] = STRUCTURED_PAGES) -> bytes:
    """제목 두 단계·격자로 그린 표·본문이 쪽마다 있는 PDF 를 만든다."""
    document = pymupdf.open()
    try:
        for content in pages:
            page = document.new_page()
            page.insert_text(
                pymupdf.Point(72, 90), content.heading, fontsize=_HEADING_SIZE, fontname=_FONT
            )
            page.insert_text(
                pymupdf.Point(72, 150),
                content.subheading,
                fontsize=_SUBHEADING_SIZE,
                fontname=_FONT,
            )
            for index, line in enumerate(content.body):
                position = pymupdf.Point(72, 190 + index * 22)
                page.insert_text(position, line, fontsize=_BODY_SIZE, fontname=_FONT)
            _draw_table(page, content.table, top=280.0)
        return document.tobytes()
    finally:
        document.close()


def _draw_table(page: pymupdf.Page, rows: Sequence[tuple[str, str]], top: float) -> None:
    # 변환기는 벡터 선으로 표 격자를 판정한다 — 셀마다 테두리를 실제로 그어야 표가 된다.
    for index, (left_cell, right_cell) in enumerate(rows):
        row_top = top + index * _ROW_HEIGHT
        row_bottom = row_top + _ROW_HEIGHT
        cell = pymupdf.Rect(_TABLE_LEFT, row_top, _TABLE_RIGHT, row_bottom)
        page.draw_rect(cell, color=(0, 0, 0))
        page.draw_line(
            pymupdf.Point(_TABLE_SPLIT, row_top), pymupdf.Point(_TABLE_SPLIT, row_bottom)
        )
        baseline = row_bottom - 8
        page.insert_text(
            pymupdf.Point(_TABLE_LEFT + 8, baseline), left_cell, fontsize=_BODY_SIZE, fontname=_FONT
        )
        page.insert_text(
            pymupdf.Point(_TABLE_SPLIT + 8, baseline),
            right_cell,
            fontsize=_BODY_SIZE,
            fontname=_FONT,
        )
