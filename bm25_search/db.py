"""SQLite schema + basic operations for the BM25 code-search index.

This module is intentionally built on the Python *standard library* only
(``sqlite3``). No third-party package is required.

The database has the following shape (the verified "v2" schema from the
design spec):

* ``chunks``                 -- source of truth, one row per 80-line chunk.
* ``code_fts``               -- FTS5 *external content* virtual table that
                                mirrors ``chunks`` 1:1 via ``rowid = chunk_id``.
* ``chunks_ai / chunks_ad / chunks_au``
                              -- AFTER INSERT / DELETE / UPDATE triggers that
                                keep ``code_fts`` in sync with ``chunks``.
* ``file_metadata``          -- per-file mtime/hash used for incremental sync.
* ``repo_state``             -- key/value store (e.g. last git commit hash).

The public surface is:

* :func:`init_db`            -- create the full schema on a connection (or path).
* :class:`Database`          -- small object wrapper exposing the same schema
                                plus convenience CRUD/query helpers.
* module-level helpers such as :func:`insert_chunk`, :func:`search`,
  :func:`upsert_file_metadata`, :func:`set_repo_state`, ...

Both the function- and the class-based API are provided so callers can use
whichever style they prefer.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable, Optional, Sequence


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# The schema is expressed as a single SQL script so it can be applied with one
# ``executescript`` call.  ``executescript`` correctly parses the ``;`` inside
# the trigger bodies.
SCHEMA_SQL = """
-- chunks: source of truth (chunk-level granularity, joined to FTS by rowid)
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath   TEXT NOT NULL,       -- relative path from the worktree root
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    raw_snippet TEXT NOT NULL,      -- original text, returned to the agent
    index_text  TEXT NOT NULL       -- pre-tokenized text, fed to FTS5 only
);

-- code_fts: FTS5 external-content table.  rowid == chunks.chunk_id (1:1).
CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    filepath,
    index_text,
    content='chunks',
    content_rowid='chunk_id',
    tokenize='unicode61 remove_diacritics 2'
);

-- keep code_fts in sync with chunks (standard external-content recipe)
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO code_fts(rowid, filepath, index_text)
    VALUES (new.chunk_id, new.filepath, new.index_text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO code_fts(code_fts, rowid, filepath, index_text)
    VALUES ('delete', old.chunk_id, old.filepath, old.index_text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO code_fts(code_fts, rowid, filepath, index_text)
    VALUES ('delete', old.chunk_id, old.filepath, old.index_text);
    INSERT INTO code_fts(rowid, filepath, index_text)
    VALUES (new.chunk_id, new.filepath, new.index_text);
END;

-- file_metadata: incremental-update tracking
CREATE TABLE IF NOT EXISTS file_metadata (
    filepath  TEXT PRIMARY KEY,
    mtime     REAL NOT NULL,
    file_hash TEXT NOT NULL
);

-- repo_state: key/value store (e.g. last_commit_hash)
CREATE TABLE IF NOT EXISTS repo_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Triggers expected to exist after init_db() runs.  Useful for assertions in
# callers/tests that want to confirm the sync machinery is wired up.
EXPECTED_TRIGGERS = ("chunks_ai", "chunks_ad", "chunks_au")
EXPECTED_TABLES = ("chunks", "code_fts", "file_metadata", "repo_state")


def _require_fts5(conn: sqlite3.Connection) -> None:
    """Raise a clear error if the SQLite build lacks FTS5 support."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pragma' "
        "UNION ALL SELECT 0"
    ).fetchone()
    # The cheap check: try compiling a throwaway FTS5 statement.  FTS5 column
    # declarations take no type affinity, hence the bare column name below.
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(_c)")
        conn.execute("DROP TABLE _fts5_probe")
    except sqlite3.OperationalError as exc:  # pragma: no cover - build dependent
        raise RuntimeError(
            "This SQLite build does not support the FTS5 extension, which is "
            "required by bm25_search.  Use a Python build with FTS5 enabled."
        ) from exc


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def init_db(conn) -> sqlite3.Connection:
    """Create the full schema (tables, FTS5 virtual table, triggers) on *conn*.

    *conn* may be either a :class:`sqlite3.Connection` or a path/``":memory:"``
    string.  When a string is given a fresh connection is created and returned;
    when a connection is given it is returned unchanged (after the schema is
    applied).

    The external-content triggers that keep ``code_fts`` in sync with
    ``chunks`` are created here, so callers never have to maintain FTS
    consistency by hand.
    """
    if isinstance(conn, str):
        conn = sqlite3.connect(conn)

    if not isinstance(conn, sqlite3.Connection):
        raise TypeError(
            f"init_db expects a sqlite3.Connection or a path string, "
            f"got {type(conn).__name__}"
        )

    _require_fts5(conn)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Pre-tokenization (Python side, because stdlib sqlite3 cannot register a
# custom FTS5 tokenizer).  Mirrors the verified design-spec pipeline.
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]+")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[぀-ヿ㐀-鿿]+")


def split_identifier(word: str) -> list[str]:
    """camelCase / snake_case -> lower-cased subword tokens (originals kept)."""
    parts: list[str] = []
    for piece in word.split("_"):
        if not piece:
            continue
        subs = _CAMEL_RE.findall(piece)
        parts.extend(s.lower() for s in subs if s)
    return parts


def cjk_bigrams(text: str) -> list[str]:
    """Japanese (CJK) 2-gram.  A single character passes through unchanged."""
    if len(text) <= 1:
        return [text]
    return [text[i:i + 2] for i in range(len(text) - 1)]


def pre_tokenize(raw_text: str) -> str:
    """Tokenize *raw_text* into whitespace-joined tokens suitable for FTS5.

    * ASCII identifiers keep their original form and gain subword tokens
      (camelCase / snake_case split).
    * Runs of CJK characters are expanded into 2-grams.
    """
    tokens: list[str] = []
    for m in _WORD_RE.finditer(raw_text):
        w = m.group(0)
        if _CJK_RE.fullmatch(w):
            tokens.extend(cjk_bigrams(w))
        else:
            tokens.append(w.lower())
            if "_" in w or re.search(r"[a-z][A-Z]", w):
                tokens.extend(split_identifier(w))
    return " ".join(tokens)


def build_match_expr(raw_query: str, mode: str = "OR") -> str:
    """Apply the same pre-tokenizer to a query and build an FTS5 MATCH expr.

    A raw multi-character CJK query never matches the 2-gram index unless the
    query side is tokenized the same way (unicode61 tokenizes the query too).
    """
    tokens = pre_tokenize(raw_query).split()
    if not tokens:
        return raw_query
    quoted = [f'"{t}"' for t in tokens]
    return f" {mode} ".join(quoted)


# ---------------------------------------------------------------------------
# Standalone CRUD helpers
# ---------------------------------------------------------------------------

def insert_chunk(conn: sqlite3.Connection,
                 filepath: str,
                 start_line: int,
                 end_line: int,
                 raw_snippet: str,
                 index_text: Optional[str] = None) -> int:
    """Insert one chunk and return its ``chunk_id``.

    If *index_text* is omitted it is derived from *filepath* + *raw_snippet*
    via :func:`pre_tokenize` (the design-spec convention).
    """
    if index_text is None:
        index_text = pre_tokenize(filepath) + " " + pre_tokenize(raw_snippet)
    cur = conn.execute(
        "INSERT INTO chunks (filepath, start_line, end_line, raw_snippet, index_text) "
        "VALUES (?, ?, ?, ?, ?)",
        (filepath, start_line, end_line, raw_snippet, index_text),
    )
    conn.commit()
    return int(cur.lastrowid)


def delete_chunks_by_filepath(conn: sqlite3.Connection, filepath: str) -> int:
    """Delete every chunk for *filepath* (the file-change update flow).

    The ``chunks_ad`` trigger removes the matching rows from ``code_fts``.
    Returns the number of deleted rows.
    """
    cur = conn.execute("DELETE FROM chunks WHERE filepath = ?", (filepath,))
    conn.commit()
    return cur.rowcount


def upsert_file_metadata(conn: sqlite3.Connection,
                         filepath: str,
                         mtime: float,
                         file_hash: str) -> None:
    """Insert or replace a ``file_metadata`` row."""
    conn.execute(
        "INSERT INTO file_metadata (filepath, mtime, file_hash) VALUES (?, ?, ?) "
        "ON CONFLICT(filepath) DO UPDATE SET mtime=excluded.mtime, "
        "file_hash=excluded.file_hash",
        (filepath, mtime, file_hash),
    )
    conn.commit()


def get_file_metadata(conn: sqlite3.Connection,
                      filepath: str) -> Optional[tuple[float, str]]:
    """Return ``(mtime, file_hash)`` for *filepath* or ``None``."""
    row = conn.execute(
        "SELECT mtime, file_hash FROM file_metadata WHERE filepath = ?",
        (filepath,),
    ).fetchone()
    return tuple(row) if row is not None else None


def set_repo_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Insert or replace a ``repo_state`` entry."""
    conn.execute(
        "INSERT INTO repo_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_repo_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """Return the value for *key* or ``None`` if absent."""
    row = conn.execute(
        "SELECT value FROM repo_state WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row is not None else None


def search(conn: sqlite3.Connection,
           raw_query: str,
           top_k: int = 5,
           mode: str = "OR") -> list[dict]:
    """Search the index and return up to *top_k* ranked chunk dicts.

    Each result is
    ``{"filepath", "start_line", "end_line", "raw_snippet", "score"}`` ordered
    best-first (FTS5 bm25() returns the most-negative score for the best hit).
    ``filepath`` is weighted 3.0 vs ``index_text`` 1.0 (design spec).
    """
    query = build_match_expr(raw_query, mode)
    rows = conn.execute(
        """
        SELECT c.filepath, c.start_line, c.end_line, c.raw_snippet,
               bm25(code_fts, 3.0, 1.0) AS score
        FROM code_fts
        JOIN chunks c ON c.chunk_id = code_fts.rowid
        WHERE code_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (query, top_k),
    ).fetchall()
    return [
        {
            "filepath": r[0],
            "start_line": r[1],
            "end_line": r[2],
            "raw_snippet": r[3],
            "score": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Database wrapper (object-oriented surface)
# ---------------------------------------------------------------------------

class Database:
    """Thin object wrapper around a single SQLite connection.

    Mirrors the module-level helpers as methods so callers can use whichever
    style they prefer.  The schema is created lazily by :meth:`init_db`.
    """

    def __init__(self, db_path_or_conn=':memory:'):
        if isinstance(db_path_or_conn, sqlite3.Connection):
            self.conn = db_path_or_conn
            self._owns = False
        else:
            self.conn = sqlite3.connect(db_path_or_conn)
            self._owns = True

    # -- schema ----------------------------------------------------------
    def init_db(self) -> "Database":
        init_db(self.conn)
        return self

    # -- chunks ----------------------------------------------------------
    def insert_chunk(self, filepath, start_line, end_line, raw_snippet,
                     index_text=None):
        return insert_chunk(self.conn, filepath, start_line, end_line,
                            raw_snippet, index_text)

    def delete_chunks_by_filepath(self, filepath):
        return delete_chunks_by_filepath(self.conn, filepath)

    def search(self, raw_query, top_k=5, mode="OR"):
        return search(self.conn, raw_query, top_k, mode)

    # -- metadata --------------------------------------------------------
    def upsert_file_metadata(self, filepath, mtime, file_hash):
        return upsert_file_metadata(self.conn, filepath, mtime, file_hash)

    def get_file_metadata(self, filepath):
        return get_file_metadata(self.conn, filepath)

    def set_repo_state(self, key, value):
        return set_repo_state(self.conn, key, value)

    def get_repo_state(self, key):
        return get_repo_state(self.conn, key)

    # -- introspection helpers ------------------------------------------
    def list_tables(self) -> set[str]:
        return {
            r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table','view')"
            )
        }

    def list_triggers(self) -> set[str]:
        return {
            r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }

    def close(self):
        if self._owns:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


__all__ = [
    "SCHEMA_SQL",
    "EXPECTED_TRIGGERS",
    "EXPECTED_TABLES",
    "init_db",
    "pre_tokenize",
    "split_identifier",
    "cjk_bigrams",
    "build_match_expr",
    "insert_chunk",
    "delete_chunks_by_filepath",
    "upsert_file_metadata",
    "get_file_metadata",
    "set_repo_state",
    "get_repo_state",
    "search",
    "Database",
]
