"""테스트용 PDF 를 그 자리에서 만든다.

바이너리를 커밋하면 무엇이 들어 있는지 diff 로 볼 수 없고 텍스트 레이어의 유무를 파일
이름으로만 주장하게 된다. 만드는 데도 PyMuPDF 를 쓰는 근거는 `tests/README.md` 에 있다.
"""

from collections.abc import Sequence

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
