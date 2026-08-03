r"""파서들이 공유하는 텍스트 정규화."""


def normalize_newlines(text: str) -> str:
    r"""윈도우 개행을 통일한다 — 두면 문단 경계가 청커의 최우선 구분자에 안 걸린다."""
    return text.replace("\r\n", "\n").replace("\r", "\n")
