"""SQLite 문서 레지스트리.

**왜 별도 저장소인가.** "이 문서의 지금 유효한 리비전"을 벡터 스토어 메타데이터에서
유도하면, 교체 도중에는 두 리비전의 청크가 공존하므로 그 순간 답이 둘이 된다. 청크
전체의 메타데이터를 한 번에 바꾸는 원자적 연산도 없다.

**왜 SQLite 인가.** 표준 라이브러리라 의존성도 이미지 크기도 늘지 않고, 트랜잭션이
있어 리비전 교체가 원자적이다. 벡터 스토어와 같은 볼륨에 두면 영속화 경로가 하나다.
기각한 대안은 `ARCHITECTURE.md` "문서 레지스트리" 참조.

연결은 **연산마다 새로 연다.** 각 연산이 서로 다른 워커 스레드에서 실행되는데
`sqlite3.Connection` 은 스레드 간 공유가 안전하지 않다. 문서 수십 건 규모에서 연결
비용은 무시할 수 있고, 연결을 들고 다니면 스레드 소유권을 직접 관리해야 한다.
"""

import asyncio
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

from app.core.documents import Document, DocumentFormat, IndexStatus
from app.core.exceptions import StorageUnavailable

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,
    filename        TEXT    NOT NULL,
    format          TEXT    NOT NULL,
    revision        TEXT    NOT NULL,
    index_signature TEXT    NOT NULL,
    index_status    TEXT    NOT NULL,
    chunk_count     INTEGER NOT NULL,
    byte_size       INTEGER NOT NULL,
    ingested_at     TEXT    NOT NULL
)
"""

_COLUMNS = (
    "document_id, filename, format, revision, index_signature, "
    "index_status, chunk_count, byte_size, ingested_at"
)


class SqliteDocumentRegistry:
    """문서 레코드를 SQLite 파일 하나에 보관한다."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    # ── 조회 ────────────────────────────────────────────────────────────

    async def get(self, document_id: str) -> Document | None:
        return await self._offload(self._get, document_id)

    async def list_all(self) -> list[Document]:
        return await self._offload(self._list_all)

    # ── 변경 ────────────────────────────────────────────────────────────

    async def commit(self, document: Document) -> None:
        """레코드를 단일 트랜잭션으로 기록한다. 이 순간 교체가 확정된다."""
        await self._offload(self._commit, document)

    async def delete(self, document_id: str) -> Document | None:
        return await self._offload(self._delete, document_id)

    # ── 블로킹 구현 (스레드풀에서만 실행된다) ───────────────────────────

    def _get(self, document_id: str) -> Document | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
        return _to_document(row) if row else None

    def _list_all(self) -> list[Document]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM documents ORDER BY ingested_at DESC, document_id"
            ).fetchall()
        return [_to_document(row) for row in rows]

    def _commit(self, document: Document) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""
                INSERT INTO documents ({_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    filename        = excluded.filename,
                    format          = excluded.format,
                    revision        = excluded.revision,
                    index_signature = excluded.index_signature,
                    index_status    = excluded.index_status,
                    chunk_count     = excluded.chunk_count,
                    byte_size       = excluded.byte_size,
                    ingested_at     = excluded.ingested_at
                """,
                (
                    document.document_id,
                    document.filename,
                    document.format.value,
                    document.revision,
                    document.index_signature,
                    document.index_status.value,
                    document.chunk_count,
                    document.byte_size,
                    document.ingested_at.isoformat(),
                ),
            )

    def _delete(self, document_id: str) -> Document | None:
        # 조회와 삭제를 한 트랜잭션에 묶는다. 나눠 두면 그 사이에 값이 바뀔 수 있고,
        # 호출자는 이 반환값으로 벡터 스토어를 정리한다.
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
        return _to_document(row)

    # ── 연결과 스키마 ───────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """블로킹. 스레드풀에서만 호출한다."""
        self._ensure_schema()
        return sqlite3.connect(self._path)

    def _ensure_schema(self) -> None:
        """스키마는 최초 접근 시 만든다 — 기동 조건을 늘리지 않기 위해서다.

        마이그레이션 도구도, 준비 스크립트도 두지 않는다. 평가자가 새 볼륨에서
        `docker compose up` 한 줄로 시작할 수 있어야 한다.
        """
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._path)) as connection, connection:
                connection.execute(_SCHEMA)
            self._schema_ready = True

    async def _offload(self, operation, *args):
        """파일 I/O 를 이벤트 루프 밖으로 내보내고, 실패를 도메인 예외로 바꾼다.

        `sqlite3.OperationalError` 가 라우터까지 새면 계층 경계가 무의미해지고 내부
        메시지(파일 경로 등)가 응답에 노출된다.
        """
        try:
            return await asyncio.to_thread(operation, *args)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("문서 레지스트리 접근 실패", exc_info=exc)
            raise StorageUnavailable("문서 레지스트리에 접근할 수 없습니다") from exc


def _to_document(row: tuple) -> Document:
    return Document(
        document_id=row[0],
        filename=row[1],
        format=DocumentFormat(row[2]),
        revision=row[3],
        index_signature=row[4],
        index_status=IndexStatus(row[5]),
        chunk_count=row[6],
        byte_size=row[7],
        ingested_at=datetime.fromisoformat(row[8]),
    )
