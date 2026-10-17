"""Authoritative project archive; FTS and chunks are disposable derived data."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .config import DB_NAME
from .identity import canonical
from .models import SessionError

VERSION = 2

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS projects(singleton INTEGER PRIMARY KEY CHECK(singleton=1),project_uid TEXT UNIQUE NOT NULL,canonical_root TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY,agent TEXT NOT NULL,variant TEXT NOT NULL,capability TEXT NOT NULL DEFAULT 'unavailable',last_success_at TEXT,diagnostics TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS sessions(session_uid TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources,project_uid TEXT NOT NULL REFERENCES projects(project_uid),native_id TEXT NOT NULL,title TEXT NOT NULL,revision TEXT NOT NULL,completeness TEXT NOT NULL,diagnostics TEXT NOT NULL,parent_id TEXT,updated_at TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT '{}',UNIQUE(source_id,native_id));
CREATE TABLE IF NOT EXISTS artifacts(source_id TEXT NOT NULL REFERENCES sources,locator TEXT NOT NULL,fingerprint TEXT NOT NULL,generation INTEGER NOT NULL,session_uid TEXT REFERENCES sessions ON DELETE SET NULL,state TEXT NOT NULL DEFAULT 'present',PRIMARY KEY(source_id,locator));
CREATE TABLE IF NOT EXISTS messages(message_uid TEXT PRIMARY KEY,session_uid TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE,ordinal INTEGER NOT NULL,native_id TEXT NOT NULL,role TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,body_hash TEXT NOT NULL,occurred_at TEXT,call_id TEXT,locator TEXT NOT NULL,redacted INTEGER NOT NULL,truncated INTEGER NOT NULL,inherited INTEGER NOT NULL,UNIQUE(session_uid,ordinal));
CREATE TABLE IF NOT EXISTS chunks(chunk_uid INTEGER PRIMARY KEY,session_uid TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE,parts TEXT NOT NULL,body TEXT NOT NULL,index_title TEXT NOT NULL,index_body TEXT NOT NULL,index_terms TEXT NOT NULL,chunk_hash TEXT NOT NULL,occurred_at TEXT,signature TEXT NOT NULL,UNIQUE(session_uid,signature));
CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5(index_title,index_body,index_terms,content=chunks,content_rowid=chunk_uid,tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN INSERT INTO session_fts(rowid,index_title,index_body,index_terms) VALUES(new.chunk_uid,new.index_title,new.index_body,new.index_terms); END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN INSERT INTO session_fts(session_fts,rowid,index_title,index_body,index_terms) VALUES('delete',old.chunk_uid,old.index_title,old.index_body,old.index_terms); END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN INSERT INTO session_fts(session_fts,rowid,index_title,index_body,index_terms) VALUES('delete',old.chunk_uid,old.index_title,old.index_body,old.index_terms); INSERT INTO session_fts(rowid,index_title,index_body,index_terms) VALUES(new.chunk_uid,new.index_title,new.index_body,new.index_terms); END;
CREATE INDEX IF NOT EXISTS chunks_session ON chunks(session_uid);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_uid,ordinal);
CREATE TABLE IF NOT EXISTS tombstones(kind TEXT NOT NULL,uid TEXT NOT NULL,deleted_at TEXT NOT NULL,PRIMARY KEY(kind,uid));
CREATE TABLE IF NOT EXISTS sync_runs(run_id TEXT PRIMARY KEY,owner TEXT NOT NULL,heartbeat_at TEXT NOT NULL,state TEXT NOT NULL,source_ids TEXT NOT NULL,result TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT OR IGNORE INTO schema_meta VALUES('tokenizer','1');
INSERT OR IGNORE INTO schema_meta VALUES('normalizer','1');
INSERT OR IGNORE INTO schema_meta VALUES('chunker','1');
PRAGMA user_version=1;
COMMIT;
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, root, create=False, relocate=False):
        self.root = Path(root).resolve()
        self.path = self.root / DB_NAME
        if not create and not self.path.exists():
            raise SessionError("index_unavailable")
        self.connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, VERSION):
                raise SessionError("migration_required")
            if version == 0:
                tables = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if tables and self.connection.execute("PRAGMA user_version").fetchone()[0] == 0:
                    raise SessionError("migration_required")
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.executescript(SCHEMA)
            project = self.connection.execute("SELECT * FROM projects").fetchone()
            if project is None:
                with self.transaction():
                    self.connection.execute("INSERT OR IGNORE INTO projects VALUES(1,?,?)", ("p_" + uuid.uuid4().hex, canonical(self.root)))
                project = self.connection.execute("SELECT * FROM projects").fetchone()
            if project["canonical_root"] != canonical(self.root):
                if not relocate:
                    raise SessionError("project_mismatch")
                with self.transaction():
                    self.connection.execute("UPDATE projects SET canonical_root=?", (canonical(self.root),))
            self.previous_root = project["canonical_root"]
            self.project_uid = project["project_uid"]
        except Exception:
            self.connection.close()
            raise

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path.resolve() or destination.exists():
            raise SessionError("backup_destination_exists")
        descriptor = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        target = sqlite3.connect(str(destination))
        try:
            self.connection.backup(target)
        finally:
            target.close()

    def source(self, source):
        row = self.connection.execute("SELECT agent,variant FROM sources WHERE source_id=?", (source.source_id,)).fetchone()
        if row and (row["agent"] != source.agent or row["variant"] != source.variant):
            raise SessionError("source_mismatch")
        self.connection.execute("INSERT OR IGNORE INTO sources(source_id,agent,variant) VALUES(?,?,?)", (source.source_id, source.agent, source.variant))

    def forget(self, sid, apply=False):
        row = self.connection.execute("SELECT session_uid FROM sessions WHERE session_uid=?", (sid,)).fetchone()
        if not row:
            raise SessionError("not_found_or_not_allowed")
        count = self.connection.execute("SELECT count(*) FROM messages WHERE session_uid=?", (sid,)).fetchone()[0]
        if apply:
            with self.transaction():
                self.connection.execute("INSERT OR REPLACE INTO tombstones VALUES('session',?,?)", (sid, now()))
                self.connection.execute("DELETE FROM sessions WHERE session_uid=?", (sid,))
        return dict(session_id=sid, messages=count, applied=apply)

    def freshness(self, allowed=None):
        rows = self.connection.execute("SELECT source_id,capability,last_success_at FROM sources ORDER BY source_id").fetchall()
        if allowed is not None:
            rows = [row for row in rows if row["source_id"] in allowed]
        return dict(sources=[dict(r) for r in rows], last_success_at=max((r["last_success_at"] for r in rows if r["last_success_at"]), default=None),
                    partial_sources=[r["source_id"] for r in rows if r["capability"] != "ready"])
