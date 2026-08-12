import sqlite3
import pytest
from pathlib import Path
import sys

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bm25_search.db import init_db, SCHEMA_SQL


def test_schema_initialization(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    try:
        cur = conn.cursor()
        # Verify chunks table
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'")
        assert cur.fetchone() is not None

        # Verify code_fts virtual table
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='code_fts'")
        assert cur.fetchone() is not None

        # Verify file_metadata table
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_metadata'")
        assert cur.fetchone() is not None

        # Verify repo_state table
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='repo_state'")
        assert cur.fetchone() is not None
    finally:
        conn.close()


def test_fts_triggers_sync(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    try:
        cur = conn.cursor()
        # Test INSERT trigger
        cur.execute(
            "INSERT INTO chunks (filepath, start_line, end_line, raw_snippet, index_text) VALUES (?, ?, ?, ?, ?)",
            ("src/db.py", 1, 10, "def init_db(): pass", "def init db pass"),
        )
        conn.commit()
        chunk_id = cur.lastrowid

        cur.execute("SELECT filepath, index_text FROM code_fts WHERE rowid=?", (chunk_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "src/db.py"

        # Test UPDATE trigger
        cur.execute(
            "UPDATE chunks SET index_text=? WHERE chunk_id=?",
            ("def init db updated", chunk_id),
        )
        conn.commit()

        cur.execute("SELECT index_text FROM code_fts WHERE rowid=?", (chunk_id,))
        row = cur.fetchone()
        assert row is not None
        assert "updated" in row[0]

        # Test DELETE trigger
        cur.execute("DELETE FROM chunks WHERE chunk_id=?", (chunk_id,))
        conn.commit()

        cur.execute("SELECT * FROM code_fts WHERE rowid=?", (chunk_id,))
        assert cur.fetchone() is None
    finally:
        conn.close()


def test_metadata_tables(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    try:
        cur = conn.cursor()
        # Test file_metadata
        cur.execute(
            "INSERT INTO file_metadata (filepath, mtime, file_hash) VALUES (?, ?, ?)",
            ("src/main.py", 1234567.89, "abc123hash"),
        )
        conn.commit()

        cur.execute("SELECT mtime, file_hash FROM file_metadata WHERE filepath=?", ("src/main.py",))
        row = cur.fetchone()
        assert row == (1234567.89, "abc123hash")

        # Test repo_state
        cur.execute(
            "INSERT INTO repo_state (key, value) VALUES (?, ?)",
            ("last_commit_hash", "deadbeef"),
        )
        conn.commit()

        cur.execute("SELECT value FROM repo_state WHERE key=?", ("last_commit_hash",))
        row = cur.fetchone()
        assert row[0] == "deadbeef"
    finally:
        conn.close()
