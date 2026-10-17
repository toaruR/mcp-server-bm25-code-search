from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from bm25_search.sessions.config import Config, DB_NAME
from bm25_search.sessions.db import Database
from bm25_search.sessions.identity import canonical, matches_project
from bm25_search.sessions.models import SessionError
from bm25_search.sessions.service import Service
from bm25_search.sessions.sync import sync_sources

FIX = Path(__file__).parent / "fixtures"


def vi(n):
    data = bytearray()
    while n >= 128:
        data.append((n & 127) | 128)
        n >>= 7
    return bytes(data + bytes([n]))


def pb(field, value):
    value = value.encode() if isinstance(value, str) else value
    return vi((field << 3) | 2) + vi(len(value)) + value


def dump_events(path, events):
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")


def setup_project(tmp_path, agents=("claude_code", "codex", "agy", "cursor")):
    root = tmp_path / "project"
    root.mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    config = Config(root)
    for agent in agents:
        source = inputs / agent
        source.mkdir()
        if agent in ("claude_code", "codex"):
            relative = "claude_code/jsonl-v2/session.jsonl" if agent == "claude_code" else "codex/rollout-v1/session.jsonl"
            text = (FIX / relative).read_text(encoding="utf-8").replace("C:/fixture/project", root.as_posix())
            (source / "session.jsonl").write_text(text, encoding="utf-8")
        elif agent == "agy":
            directory = source / "conversations"
            directory.mkdir()
            connection = sqlite3.connect(str(directory / "synthetic-agy.db"))
            connection.executescript((FIX / "agy/trajectory-v0/conversation.sql").read_text(encoding="utf-8"))
            connection.execute("UPDATE trajectory_metadata_blob SET data=?", (pb(1, pb(1, root.as_uri())) + pb(7, root.as_uri()),))
            connection.commit()
            connection.close()
            transcript = source / "brain/synthetic-agy/.system_generated/logs"
            transcript.mkdir(parents=True)
            shutil.copyfile(FIX / "agy/trajectory-v0/transcript.jsonl", transcript / "transcript.jsonl")
        else:
            global_dir = source / "globalStorage"
            global_dir.mkdir()
            connection = sqlite3.connect(str(global_dir / "state.vscdb"))
            connection.executescript((FIX / "cursor/storage-v1/global.sql").read_text(encoding="utf-8"))
            connection.close()
            workspace = source / "workspaceStorage/synthetic-workspace"
            workspace.mkdir(parents=True)
            connection = sqlite3.connect(str(workspace / "state.vscdb"))
            connection.executescript((FIX / "cursor/storage-v1/workspace.sql").read_text(encoding="utf-8"))
            connection.close()
            (workspace / "workspace.json").write_text(json.dumps(dict(folder=root.as_uri())), encoding="utf-8")
        config.add_source(agent + "-test", agent, [str(source)], "cli" if agent == "agy" else "")
    return root, inputs


def hit(service, query="generation", **kwargs):
    result = service.call("session_search", dict(query=query, **kwargs))
    assert result["results"], result
    return result["results"][0]


def test_all_adapters_project_db_search_read_and_idempotence(tmp_path):
    root, _ = setup_project(tmp_path)
    service = Service(root, "codex")
    first = service.sync()
    assert first["status"] == "partial", first  # Cursor fixture deliberately lacks some message timestamps.
    assert (root / DB_NAME).exists()
    assert not (root / ".bm25_index.db").exists()
    initial = service.status()
    assert initial["sessions"] == 5  # Cursor has inline and external sessions.
    for agent in ("claude_code", "codex", "agy", "cursor"):
        result = hit(service, agent=agent)
        read = service.call("session_read", dict(session_id=result["session_id"], anchor=result["anchor"]))
        assert any("generation" in m["body"] for m in read["messages"])
        assert all("thinking" not in m["body"] and "private reasoning" not in m["body"] for m in read["messages"])
    assert service.sync()["status"] == "partial"
    again = service.status()
    assert (again["sessions"], again["messages"], again["chunks"]) == (initial["sessions"], initial["messages"], initial["chunks"])
    assert hit(service, agent="self")["source_agent"] == "codex"


def test_unknown_project_is_not_imported_and_other_root_rejected(tmp_path):
    root, inputs = setup_project(tmp_path, ("codex",))
    path = inputs / "codex/session.jsonl"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace(root.as_posix(), (tmp_path / "other").as_posix()), encoding="utf-8")
    service = Service(root)
    result = service.sync()
    assert result["sources"][0]["counts"]["project_filtered"] == 1
    assert service.status()["sessions"] == 0
    other = tmp_path / "other"
    other.mkdir()
    with Database(root) as db:
        db.backup(other / DB_NAME)
    with pytest.raises(SessionError, match="project_mismatch"):
        Database(other)
    with Database(other, relocate=True) as moved:
        assert moved.project_uid


def test_append_preserves_anchor_edit_invalidates_and_cursor_is_bounded(tmp_path):
    root, inputs = setup_project(tmp_path, ("codex",))
    path = inputs / "codex/session.jsonl"
    service = Service(root)
    service.sync()
    result = hit(service)
    old_anchor = result["anchor"]
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    events.append(dict(type="response_item", timestamp="2026-01-02T00:00:00Z", payload=dict(type="message", id="synthetic-long", role="assistant", content=[dict(type="output_text", text="日本語🙂" * 10000)])))
    dump_events(path, events)
    assert service.sync()["status"] == "succeeded"
    service.call("session_read", dict(session_id=result["session_id"], anchor=old_anchor))
    page = service.call("session_read", dict(session_id=result["session_id"], max_bytes=2048))
    assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 2048
    pieces, pages = [], 0
    while True:
        for message in page["messages"]:
            if message["body"].startswith("日本語") or message["message_id"].endswith("never") or message["start_char"] > 0:
                pieces.append(message["body"])
        pages += 1
        if not page["next_cursor"]:
            break
        page = service.call("session_read", dict(session_id=result["session_id"], cursor=page["next_cursor"], max_bytes=2048))
        assert pages < 1000
    assert "".join(pieces) == "日本語🙂" * 10000
    for event in events:
        if event.get("payload", {}).get("id") == "synthetic-answer":
            event["payload"]["content"][0]["text"] = "[synthetic] changed body"
    dump_events(path, events)
    service.sync()
    with pytest.raises(SessionError, match="stale_anchor"):
        service.call("session_read", dict(session_id=result["session_id"], anchor=old_anchor))


def test_archive_forget_and_redaction_policy(tmp_path):
    root, inputs = setup_project(tmp_path, ("codex",))
    service = Service(root)
    service.sync()
    result = hit(service)
    source = inputs / "codex/session.jsonl"
    source.unlink()
    service.sync()
    assert service.call("session_read", dict(session_id=result["session_id"], anchor=result["anchor"]))["messages"]
    config = Config(root)
    config.data["redaction"]["rules"] = [dict(pattern="generation", replacement="MASKED")]
    config.save()
    service = Service(root)
    service.sync()
    assert not service.call("session_search", dict(query="generation"))["results"]
    masked = hit(service, "MASKED")
    with Database(root) as db:
        assert db.forget(masked["session_id"])["applied"] is False
        assert db.forget(masked["session_id"], True)["applied"] is True
    source.write_text((FIX / "codex/rollout-v1/session.jsonl").read_text(encoding="utf-8").replace("C:/fixture/project", root.as_posix()), encoding="utf-8")
    service.sync()
    assert service.status()["sessions"] == 0


def test_malformed_tail_unknown_agy_format_and_missing_cursor_bubble(tmp_path):
    root, inputs = setup_project(tmp_path)
    path = inputs / "codex/session.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"type":')
    connection = sqlite3.connect(str(inputs / "agy/conversations/synthetic-agy.db"))
    connection.execute("UPDATE steps SET step_format=99 WHERE step_type=15")
    connection.commit()
    connection.close()
    connection = sqlite3.connect(str(inputs / "cursor/globalStorage/state.vscdb"))
    connection.execute("UPDATE cursorDiskKV SET value=NULL WHERE key='bubbleId:synthetic-composer:synthetic-a'")
    connection.commit()
    connection.close()
    service = Service(root)
    result = service.sync()
    assert result["status"] == "partial", result
    assert sum(s["capability"] == "partial" for s in result["sources"]) == 3
    assert service.sync()["status"] == "partial"  # An unchanged partial input stays partial.


def test_numerical_queries_dates_and_invalid_scope(tmp_path):
    root, inputs = setup_project(tmp_path, ("codex",))
    path = inputs / "codex/session.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    events[-2]["payload"]["content"][0]["text"] += " 404 RTX 3060"
    dump_events(path, events)
    service = Service(root)
    service.sync()
    assert hit(service, "404")
    assert hit(service, "3060")
    assert not service.call("session_search", dict(query="generation", since="2026-02-01T00:00:00Z"))["results"]
    for arguments in (dict(query="x", scope="all"), dict(query="()"), dict(query="x", top_k=True), dict(query="x", until="yesterday")):
        with pytest.raises(SessionError):
            service.call("session_search", arguments)


def test_native_source_readonly_and_scope_for_read_and_status(tmp_path):
    root, _ = setup_project(tmp_path, ("codex", "claude_code"))
    service = Service(root)
    service.sync()
    codex = hit(service, agent="codex")
    config = Config(root)
    config.data["allowed_source_ids"] = ["claude_code-test"]
    config.save()
    restricted = Service(root)
    with pytest.raises(SessionError, match="not_found_or_not_allowed"):
        restricted.call("session_read", dict(session_id=codex["session_id"]))
    assert all(r["source_agent"] == "claude_code" for r in restricted.call("session_search", dict(query="generation"))["results"])


def test_two_writers_do_not_duplicate_sessions(tmp_path):
    root, _ = setup_project(tmp_path, ("codex",))
    with Database(root, create=True):
        pass
    config = Config(root)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: sync_sources(Config(root), config.sources), range(2)))
    assert all(r["status"] == "succeeded" for r in results), results
    assert Service(root).status()["sessions"] == 1
    with Database(root) as db:
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.connection.execute("INSERT INTO session_fts(session_fts) VALUES('integrity-check')")


def test_mcp_transport_and_legacy_catalogue(tmp_path):
    root, _ = setup_project(tmp_path, ("codex",))
    Service(root).sync()
    commands = [dict(jsonrpc="2.0", id=1, method="initialize", params=dict(protocolVersion="2024-11-05", capabilities={}, clientInfo=dict(name="synthetic", version="1"))),
                dict(jsonrpc="2.0", id=2, method="tools/list"),
                dict(jsonrpc="2.0", id=3, method="tools/call", params=dict(name="session_search", arguments=dict(query="generation")))]
    process = subprocess.run([sys.executable, "-m", "bm25_search.mcp_server", "--root", str(root), "--sessions", "--client-agent", "codex"],
                             input="\n".join(map(json.dumps, commands)) + "\n", text=True, encoding="utf-8", capture_output=True, cwd=Path(__file__).resolve().parents[2], timeout=20)
    assert process.returncode == 0, process.stderr
    replies = [json.loads(line) for line in process.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert {t["name"] for t in replies[1]["result"]["tools"]} == {"search", "session_search", "session_read", "session_sync"}
    assert replies[2]["result"]["structuredContent"]["results"]
    old = subprocess.run([sys.executable, "-m", "bm25_search.mcp_server", "--root", str(root)], input=json.dumps(commands[1]) + "\n", text=True, capture_output=True, cwd=Path(__file__).resolve().parents[2], timeout=20)
    assert [t["name"] for t in json.loads(old.stdout)["result"]["tools"]] == ["search"]


def test_auto_sources_fall_back_to_existing_default_locations(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".codex" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in ("APPDATA", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    assert Config(root).sources == []
    auto = Config(root, auto_sources=True)
    assert {(s.source_id, s.agent) for s in auto.sources} == {("claude-local", "claude_code"), ("codex-local", "codex")}
    auto.data["allowed_source_ids"] = ["codex-local"]
    auto.save()
    assert [s.source_id for s in Service(root, auto_sources=True).sources()] == ["codex-local"]
    (tmp_path / "registered").mkdir()
    registered, _ = setup_project(tmp_path / "registered", ("codex",))
    assert [s.source_id for s in Config(registered, auto_sources=True).sources] == ["codex-test"]


def test_search_excludes_calling_session_and_demotes_tool_only_chunks(tmp_path, monkeypatch):
    root, inputs = setup_project(tmp_path, ("claude_code",))
    base = dict(cwd=root.as_posix(), sessionId="other-claude", version="2.1.300", isSidechain=False)
    events = [dict(base, timestamp="2026-01-02T00:00:00Z", type="assistant", uuid="o-a1", parentUuid=None,
                    message=dict(id="o-api", role="assistant", content=[dict(type="tool_use", id="o-call", name="Bash", input=dict(command="echo quokka quokka quokka"))])),
               dict(base, timestamp="2026-01-02T00:01:00Z", type="assistant", uuid="o-a2", parentUuid="o-a1",
                    message=dict(id="o-api-2", role="assistant", content=[dict(type="text", text="quokka の設定理由を説明します。ほかにも長い説明が続きます。")]))]
    dump_events(inputs / "claude_code" / "other.jsonl", events)
    service = Service(root)
    service.sync()
    first = hit(service, "quokka")
    assert first["snippet"].startswith("quokka の設定理由")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-claude")
    result = service.call("session_search", dict(query="quokka"))
    assert result["status"] == "zero_match" and "current_session_excluded" in result["warnings"]
    assert service.call("session_search", dict(query="quokka", include_current=True))["results"]


def test_current_session_follows_switch_and_guardian_threads_are_skipped(tmp_path, monkeypatch):
    root, inputs = setup_project(tmp_path, ("claude_code", "codex"))
    folder = inputs / "claude_code"
    base = dict(cwd=root.as_posix(), version="2.1.300", isSidechain=False, type="user", parentUuid=None, timestamp="2026-01-02T00:00:00Z")
    dump_events(folder / "fresh.jsonl", [dict(base, sessionId="fresh-claude", uuid="f-u1", message=dict(role="user", content="wombat の件"))])
    guardian = dict(type="session_meta", timestamp="2026-01-02T00:00:00Z", payload=dict(id="guardian-thread", cwd=root.as_posix(), thread_source="guardian_review"))
    reply = dict(type="response_item", timestamp="2026-01-02T00:00:01Z", payload=dict(type="message", role="user", id="g1", content=[dict(type="input_text", text="wombat review")]))
    dump_events(inputs / "codex" / "guardian.jsonl", [guardian, reply])
    service = Service(root)
    counts = {r["agent"]: r["counts"] for r in service.sync()["sources"]}
    assert counts["codex"].get("automated_skipped") == 1
    assert all(r["source_agent"] == "claude_code" for r in service.call("session_search", dict(query="wombat"))["results"])
    stale = time.time() - 3600
    os.utime(folder / "session.jsonl", (stale, stale))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "synthetic-claude")
    result = service.call("session_search", dict(query="wombat"))
    assert result["status"] == "zero_match" and len(result["excluded_current"]) == 1
    assert hit(service, "generation")["session_id"] not in result["excluded_current"]


def test_client_without_session_env_excludes_its_latest_active_session(tmp_path):
    root, inputs = setup_project(tmp_path, ("claude_code",))
    stale = time.time() - 3600
    os.utime(inputs / "claude_code" / "session.jsonl", (stale, stale))
    Service(root).sync()
    assert hit(Service(root, "claude_code"))
    os.utime(inputs / "claude_code" / "session.jsonl")
    result = Service(root, "claude_code").call("session_search", dict(query="generation"))
    assert result["status"] == "zero_match" and len(result["excluded_current"]) == 1
    assert hit(Service(root))


def test_cursor_size_limit_and_fingerprint_are_per_conversation(tmp_path):
    root, inputs = setup_project(tmp_path, ("cursor",))
    global_db = inputs / "cursor" / "globalStorage" / "state.vscdb"
    connection = sqlite3.connect(str(global_db))
    connection.execute("INSERT INTO cursorDiskKV VALUES('unrelated:blob',?)", ("x" * 200000,))
    connection.commit()
    connection.close()
    config = Config(root)
    config.data["max_artifact_bytes"] = 100000
    config.save()
    service = Service(root)
    first = service.sync()["sources"][0]["counts"]
    assert first.get("added", 0) >= 1 and "errors" not in first
    connection = sqlite3.connect(str(global_db))
    connection.execute("UPDATE cursorDiskKV SET value=? WHERE key='unrelated:blob'", ("y" * 200000,))
    connection.commit()
    connection.close()
    again = service.sync()["sources"][0]["counts"]
    assert again.get("unchanged") == first["added"] and "updated" not in again


def test_cursor_current_session_uses_conversation_activity(tmp_path):
    root, inputs = setup_project(tmp_path, ("cursor",))
    Service(root).sync()
    assert hit(Service(root, "cursor"))
    connection = sqlite3.connect(str(inputs / "cursor" / "globalStorage" / "state.vscdb"))
    connection.execute("UPDATE composerHeaders SET lastUpdatedAt=? WHERE composerId='synthetic-composer'", (int(time.time() * 1000),))
    connection.commit()
    connection.close()
    result = Service(root, "cursor").call("session_search", dict(query="generation", agent="cursor"))
    assert len(result["excluded_current"]) == 1
    assert all(r["session_id"] not in result["excluded_current"] for r in result["results"])
