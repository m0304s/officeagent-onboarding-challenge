"""문서 레지스트리 어댑터.

`DocumentRegistry` 프로토콜(`adapters/protocols.py`)의 구현체. "이 문서의 지금 유효한
리비전은 무엇인가"에 단일 답을 주는 저장소이며, 표준 라이브러리 SQLite 파일이라
컨테이너가 늘지 않는다.
"""

from app.adapters.registry.sqlite import SqliteDocumentRegistry

__all__ = ["SqliteDocumentRegistry"]
