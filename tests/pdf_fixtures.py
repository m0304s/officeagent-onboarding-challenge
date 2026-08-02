"""테스트용 PDF 를 그 자리에서 만든다.

바이너리 픽스처를 리포에 커밋하지 않는 이유는 세 가지다. 무엇이 들어 있는지 diff 로
볼 수 없고, 텍스트 레이어가 있는지 없는지를 파일 이름으로만 주장하게 되며, 내용을 바꾸려면
매번 외부 도구가 필요하다. 코드로 만들면 "이 PDF 에는 2쪽이 있고 1쪽에만 텍스트가 있다"가
테스트에 그대로 적힌다.

만드는 데도 PyMuPDF 를 쓴다. 별도 라이브러리를 들이면 그 라이브러리가 만든 PDF 를 이
파서가 읽는지 확인하는 셈이 되어, 검증 대상이 흐려진다.
"""

from collections.abc import Sequence

import pymupdf

#: 텍스트 레이어 없는 쪽을 뜻하는 표식. `None` 을 쓰면 호출부에서 "빈 문자열과 뭐가
#: 다른가"를 매번 되짚게 되어, 이름을 붙였다.
BLANK_PAGE = None

#: 내장 한국어 CJK 폰트. 기본 폰트(Helvetica)로 한글을 그리면 글리프가 없어 추출 결과가
#: `·· ···· ··` 가 된다 — 파서가 아니라 픽스처 탓에 실패하는 테스트가 되므로 명시한다.
#: 이 폰트는 ASCII 도 함께 다루므로 픽스처 전체에 하나만 쓴다.
_FONT = "korea"


def make_pdf(pages: Sequence[str | None]) -> bytes:
    """쪽마다 텍스트를 넣은 PDF 를 만든다.

    원소가 문자열이면 그 텍스트를 그린 쪽이고, `BLANK_PAGE` 면 **텍스트 레이어가 없는**
    쪽이다. 후자는 도형만 그려 둔다 — 완전히 비면 뷰어에 따라 쪽 자체가 없는 것처럼
    다뤄질 수 있어, 스캔본("쪽은 있는데 텍스트만 없다")을 재현하지 못한다.
    """
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
