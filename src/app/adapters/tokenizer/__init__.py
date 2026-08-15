"""`Tokenizer` 프로토콜의 kiwipiepy 구현. 서드파티라 `core/` 밖 어댑터에 산다."""

from app.adapters.tokenizer.kiwi import CONTENT_TAGS, KiwiTokenizer

__all__ = ["CONTENT_TAGS", "KiwiTokenizer"]
