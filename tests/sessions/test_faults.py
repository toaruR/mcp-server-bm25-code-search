import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from bm25_search.sessions.adapters import codex
from bm25_search.sessions.config import Config
from bm25_search.sessions.db import Database, now
from bm25_search.sessions.models import SessionError
from bm25_search.sessions.service import Service
from bm25_search.sessions.sync import OWNER, start, status
from test_runtime import setup_project, hit, dump_events


def test_snapshot_replaced_mid_parse_even_with_preserved_mtime(tmp_path, monkeypatch):
    root, inputs = setup_project(tmp_path, ("codex",))
    path = inputs / "codex/session.jsonl"
    original = codex.read
    changed = []
    def racing(artifact, belongs=None):
        result = original(artifact, belongs)
        if not changed:
            stat = path.stat()
            data = path.read_text(encoding="utf-8")
            assert "generation" in data
            path.write_text(data.replace("generation", "freshstate"), encoding="utf-8")
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            changed.append(True)
        return result
    monkeypatch.setattr(codex, "read", racing)
    service = Service(root)
    assert service.sync()["status"] == "succeeded"
    assert not service.call("session_search", dict(query="generation"))["results"]
    assert hit(service, "freshstate")


def test_transaction_rollback_on_derived_index_failure(tmp_path, monkeypatch):
    root, inputs = setup_project(tmp_path, ("codex",))
    service = Service(root)
    service.sync()
    old = hit(service)
    path = inputs / "codex/session.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace("generation", "new_revision"), encoding="utf-8")
    import bm25_search.sessions.sync as sync
    original = sync.reconcile_chunks
    def fail(*args):
        raise sqlite3.OperationalError("synthetic fault")
    monkeypatch.setattr(sync, "reconcile_chunks", fail)
    assert service.sync()["status"] == "partial"
    assert service.call("session_read", dict(session_id=old["session_id"], anchor=old["anchor"]))["messages"]
    assert hit(service)
    monkeypatch.setattr(sync, "reconcile_chunks", original)
    assert service.sync()["status"] == "succeeded"
    assert hit(service, "new_revision")


def test_async_job_status_and_abandoned_owner(tmp_path):
    root, _ = setup_project(tmp_path, ("codex",))
    service = Service(root)
    accepted = service.call("session_sync", {})
    assert accepted["status"] == "accepted"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = service.call("session_sync", dict(action="status", run_id=accepted["run_id"]))
        if result["status"] not in ("queued", "running"):
            break
        time.sleep(.01)
    assert result["status"] == "succeeded", result
    with Database(root) as db:
        db.connection.execute("INSERT INTO sync_runs VALUES(?,?,?,'running',?,'{}')", ("r_abandoned", OWNER, "2000-01-01T00:00:00+00:00", '["codex-test"]'))
    assert status(Config(root), "r_abandoned")["status"] == "interrupted"


def test_killed_writer_rolls_back_and_future_schema_preserved(tmp_path):
    root, _ = setup_project(tmp_path, ("codex",))
    service = Service(root)
    service.sync()
    before = service.status()
    script = "import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA foreign_keys=ON'); c.execute('BEGIN IMMEDIATE'); c.execute('DELETE FROM sessions'); os._exit(23)"
    process = subprocess.run([sys.executable, "-c", script, str(root / ".bm25_sessions.db")], timeout=10)
    assert process.returncode == 23
    assert service.status()["sessions"] == before["sessions"]
    with Database(root) as db:
        db.connection.execute("PRAGMA user_version=99")
    with pytest.raises(SessionError, match="migration_required"):
        Database(root)
    connection = sqlite3.connect(str(root / ".bm25_sessions.db"))
    try:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == before["messages"]
    finally:
        connection.close()


def test_worktrees_are_separate_roots_and_sources_do_not_cross_import(tmp_path):
    root, inputs = setup_project(tmp_path, ("codex",))
    other = tmp_path / "another-worktree"
    other.mkdir()
    first, second = Config(root), Config(other)
    second.add_source("codex-test", "codex", [str(inputs / "codex")])
    assert Service(root).sync()["status"] == "succeeded"
    assert Service(other).sync()["status"] == "succeeded"
    assert Service(other).status()["sessions"] == 0
    with Database(root) as one, Database(other) as two:
        assert one.project_uid != two.project_uid
        assert one.path != two.path


def test_claude_sidechain_does_not_collide_with_parent_session(tmp_path):
    root, inputs = setup_project(tmp_path, ("claude_code",))
    path = inputs / "claude_code/session.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for event in events:
        event["isSidechain"] = True
        event["agentId"] = "synthetic-subagent"
    dump_events(path.with_name("agent.jsonl"), events)
    service = Service(root)
    assert service.sync()["status"] == "succeeded"
    assert service.status()["sessions"] == 2
    with Database(root) as db:
        assert {r[0] for r in db.connection.execute("SELECT native_id FROM sessions")} == {"synthetic-claude", "synthetic-claude::agent:synthetic-subagent"}
