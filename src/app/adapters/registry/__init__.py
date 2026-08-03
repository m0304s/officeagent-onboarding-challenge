"""`DocumentRegistry` 구현체 — 문서의 지금 유효한 리비전에 단일 답을 주는 저장소."""

from app.adapters.registry.sqlite import SqliteDocumentRegistry

__all__ = ["SqliteDocumentRegistry"]
