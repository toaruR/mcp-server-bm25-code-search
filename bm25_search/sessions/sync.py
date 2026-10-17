from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from .adapters import adapter_for
from .adapters.base import fingerprint
from .chunker import chunks
from .db import Database, dumps, now
from .identity import digest, historical_root, matches_project, session_uid
from .models import SessionError
from .normalize import normalize

OWNER = str(os.getpid()) + ":" + uuid.uuid4().hex
ACTIVE = set()
ACTIVE_LOCK = threading.Lock()


def process_alive(pid):
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Access denied is not proof of death.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _abandoned(row):
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["heartbeat_at"])).total_seconds()
    pid = int(row["owner"].split(":", 1)[0])
    with ACTIVE_LOCK:
        local_missing = row["owner"] == OWNER and row["run_id"] not in ACTIVE
    return not process_alive(pid) or (age > 300 and local_missing) or age > 900


def reconcile_chunks(db, sid, messages, title, config):
    connection = db.connection
    existing = {r["signature"]: r["chunk_uid"] for r in connection.execute("SELECT chunk_uid,signature FROM chunks WHERE session_uid=?", (sid,))}
    incoming = set()
    for chunk in chunks(messages, title, config.data.get("tool_output", {}).get("index_limit_chars", 2000)):
        refs = dumps(chunk["parts"])
        signature = digest(refs, chunk["chunk_hash"], chunk["index_title"], chunk["index_body"], chunk["index_terms"], chunk["occurred_at"])
        incoming.add(signature)
        if signature not in existing:
            connection.execute("INSERT INTO chunks(session_uid,parts,body,index_title,index_body,index_terms,chunk_hash,occurred_at,signature) VALUES(?,?,?,?,?,?,?,?,?)",
                               (sid, refs, chunk["body"], chunk["index_title"], chunk["index_body"], chunk["index_terms"], chunk["chunk_hash"], chunk["occurred_at"], signature))
    for signature, uid in existing.items():
        if signature not in incoming:
            connection.execute("DELETE FROM chunks WHERE chunk_uid=?", (uid,))


def save_session(db, source, artifact, parsed, normalized, fp, expected_generation, expected_revision, run_id=None):
    sid = session_uid(source, parsed.native_id)
    title = parsed.title or next((m["body"] for m in normalized if m["role"] == "user"), parsed.native_id)
    title = title[:160]
    revision = digest(title, [(m["uid"], m["body_hash"], m["role"], m["kind"], m["occurred_at"], m["call_id"], m["truncated"], m["redacted"], m["inherited"], m["locator"]) for m in normalized], parsed.diagnostics)
    connection = db.connection
    with db.transaction():
        previous = connection.execute("SELECT generation FROM artifacts WHERE source_id=? AND locator=?", (source.source_id, artifact.key)).fetchone()
        current = connection.execute("SELECT revision FROM sessions WHERE session_uid=?", (sid,)).fetchone()
        if (previous[0] if previous else None) != expected_generation or (current[0] if current else None) != expected_revision:
            raise SessionError("source_changed")
        if run_id:
            run = connection.execute("SELECT state,owner FROM sync_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["state"] != "running" or run["owner"] != OWNER:
                raise SessionError("interrupted")
        if connection.execute("SELECT 1 FROM tombstones WHERE (kind='session' AND uid=?) OR (kind='source' AND uid=?)", (sid, source.source_id)).fetchone():
            return "forgotten"
        connection.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_uid) DO UPDATE SET title=excluded.title,revision=excluded.revision,completeness=excluded.completeness,diagnostics=excluded.diagnostics,parent_id=excluded.parent_id,updated_at=excluded.updated_at,metadata=excluded.metadata",
                           (sid, source.source_id, db.project_uid, parsed.native_id, title, revision, parsed.completeness, dumps(parsed.diagnostics[:100]), parsed.parent_id, now(), dumps(parsed.metadata)))
        old = {r["message_uid"]: dict(r) for r in connection.execute("SELECT * FROM messages WHERE session_uid=?", (sid,))}
        incoming = {m["uid"] for m in normalized}
        reordered = any(m["uid"] in old and old[m["uid"]]["ordinal"] != m["ordinal"] for m in normalized)
        if reordered:
            connection.execute("UPDATE messages SET ordinal=-ordinal-1 WHERE session_uid=?", (sid,))
        for uid in old.keys() - incoming:
            connection.execute("DELETE FROM messages WHERE message_uid=?", (uid,))
        for message in normalized:
            values = (message["uid"], sid, message["ordinal"], message["native_id"], message["role"], message["kind"], message["body"], message["body_hash"], message["occurred_at"], message["call_id"], dumps(message["locator"]), int(message["redacted"]), int(message["truncated"]), int(message["inherited"]))
            if reordered or message["uid"] not in old or tuple(old[message["uid"]].values()) != values:
                connection.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(message_uid) DO UPDATE SET ordinal=excluded.ordinal,native_id=excluded.native_id,role=excluded.role,kind=excluded.kind,body=excluded.body,body_hash=excluded.body_hash,occurred_at=excluded.occurred_at,call_id=excluded.call_id,locator=excluded.locator,redacted=excluded.redacted,truncated=excluded.truncated,inherited=excluded.inherited", values)
        reconcile_chunks(db, sid, normalized, title, db.config)
        connection.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,'present') ON CONFLICT(source_id,locator) DO UPDATE SET fingerprint=excluded.fingerprint,generation=excluded.generation,session_uid=excluded.session_uid,state='present'",
                           (source.source_id, artifact.key, fp, (expected_generation or 0) + 1, sid))
        if run_id:
            connection.execute("UPDATE sync_runs SET heartbeat_at=? WHERE run_id=?", (now(), run_id))
    return "added" if not current else ("unchanged" if current[0] == revision else "updated")


def apply_policy(db, config):
    """Apply stricter redaction to archived text too, including missing sources."""
    previous = db.connection.execute("SELECT value FROM schema_meta WHERE key='policy'").fetchone()
    if previous and previous[0] == config.policy_hash():
        return
    with db.transaction():
        sessions = db.connection.execute("SELECT session_uid,title FROM sessions").fetchall()
        for session in sessions:
            rows = [dict(r) for r in db.connection.execute("SELECT * FROM messages WHERE session_uid=? ORDER BY ordinal", (session["session_uid"],))]
            for row in rows:
                body = config.replace(row["body"])
                if body != row["body"]:
                    row.update(body=body, body_hash=digest(body), redacted=1)
                    db.connection.execute("UPDATE messages SET body=?,body_hash=?,redacted=1 WHERE message_uid=?", (body, row["body_hash"], row["message_uid"]))
            title = config.replace(session["title"])
            db.connection.execute("UPDATE sessions SET title=?,revision=? WHERE session_uid=?", (title, digest([(r["message_uid"], r["body_hash"]) for r in rows], title), session["session_uid"]))
            adapted = [dict(r, uid=r["message_uid"], locator=json.loads(r["locator"])) for r in rows]
            reconcile_chunks(db, session["session_uid"], adapted, title, config)
        from .knowledge import redact_archived
        redact_archived(db, config)
        db.connection.execute("INSERT OR REPLACE INTO schema_meta VALUES('policy',?)", (config.policy_hash(),))


def sync_sources(config, sources, run_id=None, verify=False):
    results = []
    cache = {}
    aliases = config.data.get("project_aliases", [])
    belongs = lambda paths: matches_project(paths, config.root, aliases)
    with Database(config.root, create=True) as db:
        db.config = config
        apply_policy(db, config)
        for source in sources:
            db.source(source)
            counts, diagnostics, seen = Counter(), [], set()
            available = any(Path(p).exists() for p in source.roots)
            if not available:
                diagnostics.append(dict(code="source_unavailable"))
            else:
                try:
                    adapter = adapter_for(source)
                    for artifact in adapter.discover(source):
                        seen.add(artifact.key)
                        if any(fnmatch.fnmatch(str(path), pattern) for path in artifact.paths for pattern in config.data.get("exclude_paths", [])):
                            counts["excluded"] += 1
                            continue
                        for attempt in range(3):
                            try:
                                previous = db.connection.execute("SELECT * FROM artifacts WHERE source_id=? AND locator=?", (source.source_id, artifact.key)).fetchone()
                                expected_revision = None
                                if previous and previous["session_uid"]:
                                    row = db.connection.execute("SELECT revision FROM sessions WHERE session_uid=?", (previous["session_uid"],)).fetchone()
                                    expected_revision = row[0] if row else None
                                raw_fp = fingerprint(artifact.paths, config.data.get("max_artifact_bytes", 64 * 1024 * 1024), None if verify else cache)
                                fp = digest(raw_fp, config.policy_hash(), 1)
                                if previous and previous["fingerprint"] == fp and previous["state"] == "present":
                                    counts["unchanged"] += 1
                                    old_session = db.connection.execute("SELECT completeness FROM sessions WHERE session_uid=?", (previous["session_uid"],)).fetchone()
                                    if old_session and old_session[0] != "complete":
                                        diagnostics.append(dict(code="partial_session", session_id=previous["session_uid"]))
                                    break
                                parsed = adapter.read(artifact, belongs)
                                if not belongs(parsed.projects):
                                    ambiguous = not parsed.projects or any(belongs([project]) for project in parsed.projects)
                                    try:
                                        for project in parsed.projects:
                                            historical_root(project)
                                    except (SessionError, OSError, ValueError):
                                        ambiguous = True
                                    counts["project_unresolved" if ambiguous else "project_filtered"] += 1
                                    if ambiguous:
                                        diagnostics.append(dict(code="project_unresolved", artifact=digest(artifact.key)[:16]))
                                    break
                                if not parsed.native_id or not parsed.messages:
                                    diagnostics.append(dict(code="unsupported_format", artifact=digest(artifact.key)[:16]))
                                    counts["unsupported"] += 1
                                    break
                                sid = session_uid(source, parsed.native_id)
                                if previous and previous["session_uid"] != sid:
                                    expected_revision = None
                                normalized = normalize(parsed, sid, config)
                                parsed.title = config.replace(parsed.title)
                                if fingerprint(artifact.paths, config.data.get("max_artifact_bytes", 64 * 1024 * 1024)) != raw_fp:
                                    raise SessionError("source_changed")
                                outcome = save_session(db, source, artifact, parsed, normalized, fp,
                                                       previous["generation"] if previous else None, expected_revision, run_id)
                                counts[outcome] += 1
                                if parsed.diagnostics:
                                    diagnostics.append(dict(code="partial_session", session_id=sid, details=parsed.diagnostics[:5]))
                                break
                            except SessionError as error:
                                if error.code == "source_changed" and attempt < 2:
                                    cache.clear()
                                    continue
                                counts["errors"] += 1
                                diagnostics.append(dict(code=error.code, artifact=digest(artifact.key)[:16]))
                                break
                            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
                                counts["errors"] += 1
                                diagnostics.append(dict(code="unsupported_or_unreadable_artifact", artifact=digest(artifact.key)[:16]))
                                break
                        if run_id:
                            db.connection.execute("UPDATE sync_runs SET heartbeat_at=? WHERE run_id=? AND owner=? AND state='running'", (now(), run_id, OWNER))
                except (OSError, sqlite3.Error, ValueError, SessionError):
                    diagnostics.append(dict(code="source_unavailable"))
                    available = False
                if available:
                    for row in db.connection.execute("SELECT locator FROM artifacts WHERE source_id=?", (source.source_id,)).fetchall():
                        if row[0] not in seen:
                            db.connection.execute("UPDATE artifacts SET state='source_missing' WHERE source_id=? AND locator=?", (source.source_id, row[0]))
            capability = "unavailable" if not available else ("partial" if diagnostics else "ready")
            db.connection.execute("UPDATE sources SET capability=?,last_success_at=COALESCE(?,last_success_at),diagnostics=? WHERE source_id=?",
                                  (capability, now() if available else None, dumps(diagnostics[:100]), source.source_id))
            results.append(dict(source_id=source.source_id, agent=source.agent, variant=source.variant, capability=capability,
                                counts=dict(counts), diagnostics=diagnostics[:100]))
        try:
            db.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass
    state = "succeeded" if results and all(r["capability"] == "ready" for r in results) else "partial"
    if not results or all(r["capability"] == "unavailable" for r in results):
        state = "failed"
    return dict(status=state, sources=results)


def start(config, sources, wait=False, verify=False):
    if not sources:
        raise SessionError("source_required")
    wanted = {s.source_id for s in sources}
    run_id = "r_" + uuid.uuid4().hex
    with Database(config.root, create=True) as db:
        with db.transaction():
            for row in db.connection.execute("SELECT * FROM sync_runs WHERE state IN ('queued','running')").fetchall():
                if _abandoned(row):
                    db.connection.execute("UPDATE sync_runs SET state='interrupted' WHERE run_id=?", (row["run_id"],))
                elif wanted.intersection(json.loads(row["source_ids"])):
                    return dict(status="already_running", run_id=row["run_id"])
            with ACTIVE_LOCK:
                ACTIVE.add(run_id)
            db.connection.execute("INSERT INTO sync_runs(run_id,owner,heartbeat_at,state,source_ids) VALUES(?,?,?,'running',?)",
                                  (run_id, OWNER, now(), dumps(sorted(wanted))))

    def execute():
        try:
            result = sync_sources(config, sources, run_id, verify)
        except Exception:
            result = dict(status="failed", code="sync_failed")
        try:
            with Database(config.root) as db:
                db.connection.execute("UPDATE sync_runs SET state=?,heartbeat_at=?,result=? WHERE run_id=? AND owner=? AND state='running'",
                                      (result["status"], now(), dumps(result), run_id, OWNER))
        finally:
            with ACTIVE_LOCK:
                ACTIVE.discard(run_id)
        return dict(result, run_id=run_id)

    if wait:
        return execute()
    threading.Thread(target=execute, name="session-sync", daemon=True).start()
    return dict(status="accepted", run_id=run_id, source_ids=sorted(wanted))


def status(config, run_id):
    with Database(config.root) as db:
        row = db.connection.execute("SELECT * FROM sync_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise SessionError("not_found_or_not_allowed")
        if row["state"] in ("queued", "running") and _abandoned(row):
            db.connection.execute("UPDATE sync_runs SET state='interrupted' WHERE run_id=?", (run_id,))
            return dict(status="interrupted", run_id=run_id)
        return dict(json.loads(row["result"]), status=row["state"], run_id=run_id)
