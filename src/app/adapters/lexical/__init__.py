"""`LexicalIndex` 구현체. SQLite FTS5 라 컨테이너가 늘지 않는다."""

from app.adapters.lexical.sqlite import (
    DEFAULT_MIN_TOKEN_RARITY,
    SqliteLexicalIndex,
    fts5_is_available,
)

__all__ = ["DEFAULT_MIN_TOKEN_RARITY", "SqliteLexicalIndex", "fts5_is_available"]
