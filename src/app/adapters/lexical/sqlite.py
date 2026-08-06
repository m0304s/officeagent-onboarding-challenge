"""SQLite FTS5 어휘 색인 — 등록·제거·집계·BM25 검색.

`bm25()` 부호 뒤집기와 드문 토큰 겹침 판정이 계약이고 근거는 `ARCHITECTURE.md` 「어휘 색인」
에 있다. 연결은 연산마다 새로 연다 — `sqlite3.Connection` 은 스레드 간 공유가 안전하지 않다.
"""

import asyncio
import logging
import math
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import closing
from pathlib import Path

from app.core.documents import Chunk, ChunkLocation, DocumentFormat, StoredIndexVersion
from app.core.exceptions import StorageUnavailable
from app.core.lexical import DEFAULT_TOKENIZER, Tokenizer
from app.core.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)

#: 절단을 파이썬으로 미뤄 동점의 순서를 정체성으로 고정한다 — SQL 이 딱 `top_k` 만 읽으면
#: 경계의 동점 항목 중 어느 것이 남을지 정해지지 않는다. 배수를 두는 것은 메모리 때문이다.
CANDIDATE_OVERFETCH = 10

#: 토큰이 "드물다"고 인정받는 하한. 설정(`APP_LEXICAL_MIN_TOKEN_RARITY`)이 덮어쓴다.
#: 실측 근거는 `ARCHITECTURE.md` 「어휘 색인」에 있다.
DEFAULT_MIN_TOKEN_RARITY = 0.3

# 토큰은 이미 정규화·소문자화되어 공백으로 이어 붙여 들어온다. `remove_diacritics 0` 은
# fts5 가 그것을 다시 손대지 않게 해 `fts5vocab` 의 term 이 우리 토큰과 글자 그대로 같아진다.
_SCHEMA = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
        tokens,
        document_id     UNINDEXED,
        revision        UNINDEXED,
        index_signature UNINDEXED,
        chunk_index     UNINDEXED,
        text            UNINDEXED,
        filename        UNINDEXED,
        format          UNINDEXED,
        char_start      UNINDEXED,
        char_end        UNINDEXED,
        page            UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 0'
    )
    """,
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_terms USING fts5vocab(chunks, 'row')",
)

_COLUMNS = (
    "tokens, document_id, revision, index_signature, chunk_index, "
    "text, filename, format, char_start, char_end, page"
)

_AXES = ("document_id", "revision", "index_signature")


def fts5_is_available() -> bool:
    """이 파이썬의 sqlite3 가 FTS5 를 갖고 있는가. 기동 경로가 경고를 내는 근거다."""
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            row = connection.execute(
                "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
            ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


class SqliteLexicalIndex:
    """청크의 토큰 문자열을 SQLite FTS5 파일 하나에 보관한다."""

    def __init__(
        self,
        path: Path,
        *,
        tokenizer: Tokenizer = DEFAULT_TOKENIZER,
        min_token_rarity: float = DEFAULT_MIN_TOKEN_RARITY,
    ) -> None:
        self._path = Path(path)
        # 쓰기와 읽기가 같은 인스턴스를 공유한다. 갈리면 색인은 오류 없이 아무것도 찾지 못한다.
        self._tokenizer = tokenizer
        self._floor = min_token_rarity
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    # ── 쓰기 ────────────────────────────────────────────────────────────

    async def add_chunks(
        self,
        chunks: Sequence[Chunk],
        *,
        filename: str,
        document_format: DocumentFormat,
    ) -> None:
        if not chunks:
            return
        await self._offload(self._add_chunks, chunks, filename, document_format)

    async def delete_document(
        self,
        document_id: str,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int:
        return await self._offload(self._delete, document_id, revision, index_signature)

    # ── 조회 ────────────────────────────────────────────────────────────

    async def count_chunks(
        self,
        document_id: str | None = None,
        *,
        revision: str | None = None,
        index_signature: str | None = None,
    ) -> int:
        return await self._offload(self._count, document_id, revision, index_signature)

    async def list_stored_versions(self) -> list[StoredIndexVersion]:
        return await self._offload(self._list_versions)

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        versions: Sequence[StoredIndexVersion],
    ) -> list[RetrievedChunk]:
        """대상 삼중항으로 좁힌 뒤 어휘가 겹치는 청크부터 돌려준다."""
        # 빈 목록은 조건 없음이 아니라 대상 없음이다 — 흘려보내면 잔여 청크만 남은
        # 색인에서 사용자가 지운 문서가 검색된다.
        if not versions:
            return []
        tokens = tuple(dict.fromkeys(self._tokenizer.tokenize(query)))
        if not tokens:
            return []
        return await self._offload(self._search, tokens, top_k, tuple(versions))

    # ── 블로킹 구현 (스레드풀에서만 실행된다) ───────────────────────────

    def _add_chunks(
        self, chunks: Sequence[Chunk], filename: str, document_format: DocumentFormat
    ) -> None:
        with closing(self._connect()) as connection, connection:
            for chunk in chunks:
                # FTS5 에 UNIQUE 제약이 없어 재등록이 같은 청크를 두 번 검색되게 만든다.
                # 먼저 지우는 것이 중복을 막는 유일한 수단이다.
                connection.execute(
                    "DELETE FROM chunks WHERE document_id = ? AND revision = ?"
                    " AND index_signature = ? AND chunk_index = ?",
                    (chunk.document_id, chunk.revision, chunk.index_signature, chunk.chunk_index),
                )
                connection.execute(
                    f"INSERT INTO chunks ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        " ".join(self._tokenizer.tokenize(chunk.text)),
                        chunk.document_id,
                        chunk.revision,
                        chunk.index_signature,
                        chunk.chunk_index,
                        chunk.text,
                        filename,
                        document_format.value,
                        chunk.location.char_start,
                        chunk.location.char_end,
                        chunk.location.page,
                    ),
                )

    def _delete(self, document_id: str, revision: str | None, index_signature: str | None) -> int:
        clause, params = _where(document_id, revision, index_signature)
        with closing(self._connect()) as connection, connection:
            # 삭제한 개수를 먼저 센다. 가상 테이블에서 `rowcount` 는 신뢰할 수 없고,
            # 호출자는 이 값으로 "정말 지워졌는가"를 확인한다.
            removed = connection.execute(
                f"SELECT count(*) FROM chunks{clause}", params
            ).fetchone()[0]
            if removed:
                connection.execute(f"DELETE FROM chunks{clause}", params)
        return int(removed)

    def _count(
        self, document_id: str | None, revision: str | None, index_signature: str | None
    ) -> int:
        clause, params = _where(document_id, revision, index_signature)
        with closing(self._connect()) as connection:
            return int(
                connection.execute(f"SELECT count(*) FROM chunks{clause}", params).fetchone()[0]
            )

    def _list_versions(self) -> list[StoredIndexVersion]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT document_id, revision, index_signature FROM chunks"
                " ORDER BY document_id, revision, index_signature"
            ).fetchall()
        return [StoredIndexVersion(*row) for row in rows]

    def _search(
        self, tokens: tuple[str, ...], top_k: int, versions: tuple[StoredIndexVersion, ...]
    ) -> list[RetrievedChunk]:
        window = max(top_k, 1) * CANDIDATE_OVERFETCH
        clause, params = _version_filter(versions)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                # 정렬은 원값 오름차순이고 노출 점수는 그것을 뒤집은 값이다. fts5 의
                # `bm25()` 가 관련도가 높을수록 더 작은 음수를 돌려주기 때문이다.
                f"SELECT {_COLUMNS}, -bm25(chunks) FROM chunks"
                f" WHERE chunks MATCH ?{clause}"
                " ORDER BY bm25(chunks) ASC LIMIT ?",
                (_match_expression(tokens), *params, window),
            ).fetchall()
            rarities = self._token_rarities(connection, tokens)

        if len(rows) == window:
            logger.debug(
                "어휘 후보 창이 가득 찼습니다 — 뒤쪽 후보는 읽지 않았습니다",
                extra={"candidate_window": window, "top_k": top_k},
            )

        # 변별력 판정이 목록을 자르지 않는다 — 자르면 남은 항목의 순위가 당겨져 융합이
        # 볼 교집합이 좁아진다 (`lexical-index` 스펙).
        found = [
            _retrieved_chunk(row, gate_passed=_has_rare_overlap(row[0], rarities, self._floor))
            for row in rows
        ]
        # 점수가 같은 청크의 순서를 정체성으로 고정한다 — SQL 정렬에 맡기면 UNINDEXED
        # 컬럼이 TEXT 라 `chunk_index` 가 사전순으로 비교된다.
        found.sort(key=lambda chunk: (-chunk.native_score, chunk.document_id, chunk.chunk_index))
        return found[:top_k]

    def _token_rarities(
        self, connection: sqlite3.Connection, tokens: tuple[str, ...]
    ) -> dict[str, float]:
        """질의 토큰마다의 희소도. 색인 전체를 모수로 삼는다 — 대상 목록으로 좁히지 않는다."""
        total_rows = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        placeholders = ", ".join("?" * len(tokens))
        frequencies = dict(
            connection.execute(
                f"SELECT term, doc FROM chunk_terms WHERE term IN ({placeholders})", tokens
            ).fetchall()
        )
        return {token: _rarity(total_rows, frequencies.get(token, 0)) for token in tokens}

    # ── 연결과 스키마 ───────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """블로킹. 스레드풀에서만 호출한다."""
        self._ensure_schema()
        return sqlite3.connect(self._path)

    def _ensure_schema(self) -> None:
        """스키마를 최초 접근 시 만든다 — 색인 파일이 없어도 기동은 성공해야 한다."""
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            if not fts5_is_available():
                # 기동을 세우지 않고 첫 사용에서 드러낸다. 빈 결과로 위장하면 어휘 검색이
                # 꺼진 채로 도는 것과 구별되지 않는다.
                raise StorageUnavailable("이 런타임의 sqlite3 에 FTS5 가 없습니다")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._path)) as connection, connection:
                for statement in _SCHEMA:
                    connection.execute(statement)
            self._schema_ready = True

    async def _offload(self, operation, *args):
        """파일 I/O 를 루프 밖으로 내보내고 실패를 도메인 예외로 바꾼다.

        `sqlite3.OperationalError` 가 새면 파일 경로 같은 내부 정보가 응답에 노출된다."""
        try:
            return await asyncio.to_thread(operation, *args)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("어휘 색인 접근 실패", exc_info=exc)
            raise StorageUnavailable("어휘 색인에 접근할 수 없습니다") from exc


def _idf(total_rows: int, document_frequency: int) -> float:
    """BM25 표준 IDF. `df=0` 에서도 유한하고 항상 양수다."""
    return math.log(
        1 + (total_rows - document_frequency + 0.5) / (document_frequency + 0.5)
    )


def _rarity(total_rows: int, document_frequency: int) -> float:
    """같은 코퍼스의 최대 IDF 로 나눈 `(0, 1]` 희소도 — 하한을 코퍼스 크기에서 뗀다."""
    ceiling = _idf(total_rows, 0)
    return _idf(total_rows, document_frequency) / ceiling if ceiling > 0 else 0.0


def _has_rare_overlap(stored_tokens: str, rarities: dict[str, float], floor: float) -> bool:
    """이 청크가 질의의 드문 토큰을 하나라도 담고 있는가.

    비율이 아니라 개별 토큰의 희소도를 본다 — 비율은 토큰 하나짜리 질의를 걸러내지 못한다."""
    present = set(stored_tokens.split())
    return any(rarity >= floor for token, rarity in rarities.items() if token in present)


def _match_expression(tokens: Iterable[str]) -> str:
    """토큰들을 FTS5 의 OR 질의로 잇는다. 큰따옴표는 겹쳐 써서 이스케이프한다."""
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _version_filter(versions: Sequence[StoredIndexVersion]) -> tuple[str, tuple]:
    """대상 삼중항 목록을 `WHERE` 조각과 바인딩 값으로 옮긴다."""
    group = " AND ".join(f"{axis} = ?" for axis in _AXES)
    clause = " OR ".join(f"({group})" for _ in versions)
    params = tuple(
        value
        for version in versions
        for value in (version.document_id, version.revision, version.index_signature)
    )
    return f" AND ({clause})", params


def _where(
    document_id: str | None, revision: str | None, index_signature: str | None
) -> tuple[str, tuple]:
    """조건을 `WHERE` 조각으로 옮긴다. 조건이 없으면 빈 문자열(= 전체)."""
    pairs = [
        (axis, value)
        for axis, value in zip(_AXES, (document_id, revision, index_signature), strict=True)
        if value is not None
    ]
    if not pairs:
        return "", ()
    clause = " AND ".join(f"{axis} = ?" for axis, _ in pairs)
    return f" WHERE {clause}", tuple(value for _, value in pairs)


def _retrieved_chunk(row: tuple, *, gate_passed: bool) -> RetrievedChunk:
    """조회 한 줄을 결과 값 객체로 옮긴다. 열 순서는 `_COLUMNS` 가 정한다."""
    return RetrievedChunk(
        document_id=row[1],
        revision=row[2],
        index_signature=row[3],
        chunk_index=int(row[4]),
        text=row[5],
        location=ChunkLocation(
            char_start=int(row[8]),
            char_end=int(row[9]),
            page=int(row[10]) if row[10] is not None else None,
        ),
        filename=row[6],
        format=DocumentFormat(row[7]),
        # fts5 는 `idf <= 0` 을 눌러 두므로 뒤집은 값이 음수가 되지 않는다.
        native_score=max(0.0, float(row[11])),
        gate_passed=gate_passed,
    )
