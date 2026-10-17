"""Optional, explicitly recorded knowledge with pinned source evidence."""
from __future__ import annotations

import json
import uuid

from .db import Database, dumps, now
from .models import AGENTS, SessionError
from .search import authorize, decode, integer, size
from .tokenizer import match_query, tokenize

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS knowledge(kid INTEGER PRIMARY KEY,uid TEXT NOT NULL UNIQUE,project_uid TEXT NOT NULL REFERENCES projects(project_uid),title TEXT NOT NULL,claim TEXT NOT NULL,conditions TEXT NOT NULL,procedure TEXT NOT NULL,applies_to TEXT NOT NULL,status TEXT NOT NULL,verification TEXT NOT NULL,generated_by TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,index_title TEXT NOT NULL,index_body TEXT NOT NULL,supersedes_id TEXT);
CREATE TABLE IF NOT EXISTS knowledge_evidence(kid INTEGER NOT NULL REFERENCES knowledge ON DELETE CASCADE,session_uid TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE,message_uid TEXT NOT NULL,body_hash TEXT NOT NULL,start_char INTEGER NOT NULL,end_char INTEGER NOT NULL,snapshot TEXT NOT NULL,locator TEXT NOT NULL,kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS knowledge_evidence_session ON knowledge_evidence(session_uid);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(index_title,index_body,content=knowledge,content_rowid=kid);
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN INSERT INTO knowledge_fts(rowid,index_title,index_body) VALUES(new.kid,new.index_title,new.index_body); END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN INSERT INTO knowledge_fts(knowledge_fts,rowid,index_title,index_body) VALUES('delete',old.kid,old.index_title,old.index_body); END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN INSERT INTO knowledge_fts(knowledge_fts,rowid,index_title,index_body) VALUES('delete',old.kid,old.index_title,old.index_body); INSERT INTO knowledge_fts(rowid,index_title,index_body) VALUES(new.kid,new.index_title,new.index_body); END;
PRAGMA user_version=2;
COMMIT;
"""


def ensure(db):
    if db.connection.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return
    backup = db.root / (".bm25_sessions.db.before-v2-" + uuid.uuid4().hex + ".bak")
    db.backup(backup)
    db.connection.executescript(SCHEMA)


def tools():
    from .mcp_tools import definition
    maximum = dict(type="integer", minimum=2048, maximum=65536, default=16384)
    evidence = dict(type="array", minItems=1, maxItems=8, items=dict(type="object", required=["session_id", "anchor"], additionalProperties=False,
                                                                  properties=dict(session_id=dict(type="string"), anchor=dict(type="string"))))
    return [definition("knowledge_search", "Search explicit knowledge in this project; excludes lost or changed evidence. Applicability and verification are separate from search rank.",
                       dict(query=dict(type="string"), top_k=dict(type="integer", minimum=1, maximum=50, default=5), max_bytes=maximum), ["query"]),
            definition("knowledge_read", "Read a knowledge claim, applicability conditions and pinned source evidence; reports stale/missing evidence.",
                       dict(knowledge_id=dict(type="string"), max_bytes=maximum), ["knowledge_id"]),
            definition("knowledge_record", "Explicitly create/revise a proposed claim with live session anchors. tested requires a native successful execution result; accepted is a separate explicit status.",
                       dict(knowledge_id=dict(type="string"), title=dict(type="string"), claim=dict(type="string"), conditions=dict(type="string"), procedure=dict(type="string"),
                            evidence=evidence, generated_by=dict(type="string", enum=list(AGENTS)), applies_to=dict(type="object"),
                            status=dict(type="string", enum=["proposed", "accepted", "superseded", "rejected"], default="proposed"),
                            verification=dict(type="string", enum=["unverified", "observed", "tested"], default="unverified"), supersedes_id=dict(type="string")),
                       ["title", "claim", "evidence", "generated_by"])]


def validity(db, config, row):
    evidence = db.connection.execute("SELECT e.*,m.body_hash AS current_hash,s.source_id FROM knowledge_evidence e LEFT JOIN messages m ON m.message_uid=e.message_uid LEFT JOIN sessions s ON s.session_uid=e.session_uid WHERE e.kid=?", (row["kid"],)).fetchall()
    allowed = config.data.get("allowed_source_ids")
    if allowed is not None and any(e["source_id"] not in allowed for e in evidence):
        raise SessionError("not_found_or_not_allowed")
    return "missing_evidence" if not evidence else ("stale_evidence" if any(e["current_hash"] != e["body_hash"] for e in evidence) else "current"), evidence


def record(db, config, args):
    for field, limit in (("title", 160), ("claim", 2000), ("conditions", 2000), ("procedure", 4000)):
        if not isinstance(args.get(field, ""), str) or len(args.get(field, "")) > limit:
            raise SessionError("invalid_arguments")
    if not args.get("title") or not args.get("claim") or args.get("generated_by") not in AGENTS:
        raise SessionError("invalid_arguments")
    status = args.get("status", "proposed")
    verification = args.get("verification", "unverified")
    if status not in ("proposed", "accepted", "superseded", "rejected") or verification not in ("unverified", "observed", "tested"):
        raise SessionError("invalid_arguments")
    applies = args.get("applies_to", {})
    if not isinstance(applies, dict) or set(applies) - {"agent", "os", "versions"} or len(dumps(applies)) > 2048:
        raise SessionError("invalid_arguments")
    if applies.get("agent", "all") not in ("all", *AGENTS):
        raise SessionError("invalid_arguments")
    items = args.get("evidence")
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise SessionError("invalid_arguments")
    with db.transaction():
        evidence, tested = [], False
        for item in items:
            if not isinstance(item, dict) or set(item) != {"session_id", "anchor"}:
                raise SessionError("invalid_arguments")
            authorize(db, config, item["session_id"])
            ref = decode(item["anchor"])
            message = db.connection.execute("SELECT * FROM messages WHERE message_uid=? AND session_uid=?", (ref.get("mid"), item["session_id"])).fetchone()
            if ref.get("sid") != item["session_id"] or not message or message["body_hash"] != ref.get("hash"):
                raise SessionError("stale_anchor")
            if message["kind"] == "retrieved_context":
                raise SessionError("recursive_evidence")
            start = integer(ref.get("start"), 0, len(message["body"]))
            end = integer(ref.get("end"), start, min(len(message["body"]), start + 4000))
            if end <= start:
                raise SessionError("invalid_arguments")
            locator = json.loads(message["locator"])
            executed = message["role"] == "tool" and message["kind"] == "tool_result" and type(locator.get("exit_code")) is int and locator["exit_code"] == 0
            if executed:
                executed = bool(db.connection.execute("SELECT 1 FROM messages WHERE session_uid=? AND call_id=? AND kind='tool_call'", (item["session_id"], message["call_id"])).fetchone())
            tested = tested or executed
            excerpt = message["body"][start:min(end, start + 1000)]
            evidence.append((item["session_id"], message["message_uid"], message["body_hash"], start, min(end, start + 1000), excerpt, message["locator"], "execution" if executed else "source"))
        if verification == "tested" and not tested:
            raise SessionError("execution_evidence_required")
        uid = args.get("knowledge_id") or "k_" + uuid.uuid4().hex
        old = db.connection.execute("SELECT * FROM knowledge WHERE uid=?", (uid,)).fetchone()
        if args.get("knowledge_id") and not old:
            raise SessionError("not_found_or_not_allowed")
        if old:
            validity(db, config, old)
        text = {field: config.replace(args.get(field, "")) for field in ("title", "claim", "conditions", "procedure")}
        applies_text = config.replace(dumps(applies))
        try:
            json.loads(applies_text)
        except ValueError:
            raise SessionError("invalid_arguments") from None
        current_time = now()
        values = (uid, db.project_uid, text["title"], text["claim"], text["conditions"], text["procedure"], applies_text, status, verification, args["generated_by"],
                  old["created_at"] if old else current_time, current_time, tokenize(text["title"]), tokenize("\n".join(text.values()) + "\n" + applies_text), args.get("supersedes_id"))
        db.connection.execute("INSERT INTO knowledge(uid,project_uid,title,claim,conditions,procedure,applies_to,status,verification,generated_by,created_at,updated_at,index_title,index_body,supersedes_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET title=excluded.title,claim=excluded.claim,conditions=excluded.conditions,procedure=excluded.procedure,applies_to=excluded.applies_to,status=excluded.status,verification=excluded.verification,generated_by=excluded.generated_by,updated_at=excluded.updated_at,index_title=excluded.index_title,index_body=excluded.index_body,supersedes_id=excluded.supersedes_id", values)
        kid = db.connection.execute("SELECT kid FROM knowledge WHERE uid=?", (uid,)).fetchone()[0]
        db.connection.execute("DELETE FROM knowledge_evidence WHERE kid=?", (kid,))
        db.connection.executemany("INSERT INTO knowledge_evidence VALUES(?,?,?,?,?,?,?,?,?)", [(kid, *item) for item in evidence])
        if args.get("supersedes_id"):
            target = db.connection.execute("SELECT * FROM knowledge WHERE uid=? AND uid<>?", (args["supersedes_id"], uid)).fetchone()
            if not target:
                raise SessionError("not_found_or_not_allowed")
            validity(db, config, target)
            db.connection.execute("UPDATE knowledge SET status='superseded',updated_at=? WHERE uid=?", (current_time, target["uid"]))
    return dict(status="ok", knowledge_id=uid, record_status=status, verification=verification)


def call(config, name, arguments):
    definitions = {tool["name"]: tool["inputSchema"] for tool in tools()}
    if name not in definitions or not isinstance(arguments, dict) or set(arguments) - set(definitions[name]["properties"]) or set(definitions[name]["required"]) - set(arguments):
        raise SessionError("invalid_arguments")
    maximum = integer(arguments.get("max_bytes", 16384), 2048, 65536)
    with Database(config.root) as db:
        ensure(db)
        if name == "knowledge_record":
            return record(db, config, arguments)
        if name == "knowledge_read":
            row = db.connection.execute("SELECT * FROM knowledge WHERE uid=?", (arguments["knowledge_id"],)).fetchone()
            if not row:
                raise SessionError("not_found_or_not_allowed")
            state, evidence = validity(db, config, row)
            fields = ("uid", "title", "claim", "conditions", "procedure", "status", "verification", "generated_by", "created_at", "updated_at", "supersedes_id")
            payload = dict(status="ok", knowledge={key: row[key] for key in fields}, applicability=json.loads(row["applies_to"]), evidence_state=state,
                           evidence=[{key: e[key] for key in ("session_uid", "message_uid", "body_hash", "start_char", "end_char", "snapshot", "kind")} for e in evidence])
            if size(payload) > maximum:
                raise SessionError("budget_too_small")
            return payload
        top_k = integer(arguments.get("top_k", 5), 1, 50)
        expression = match_query(arguments["query"])
        rows = db.connection.execute("SELECT k.*,bm25(knowledge_fts,2.0,1.0) AS score FROM knowledge_fts JOIN knowledge k ON k.kid=knowledge_fts.rowid WHERE knowledge_fts MATCH ? AND k.status IN ('proposed','accepted') ORDER BY score,k.uid", (expression,))
        results = []
        for row in rows:
            try:
                state, _ = validity(db, config, row)
            except SessionError:
                continue
            if state != "current":
                continue
            results.append(dict(knowledge_id=row["uid"], title=row["title"], snippet=row["claim"][:300], record_status=row["status"],
                                verification=row["verification"], applicability=json.loads(row["applies_to"]), score=row["score"]))
            if len(results) >= top_k:
                break
        payload = dict(status="ok" if results else "zero_match", results=results)
        while results and size(payload) > maximum:
            results.pop()
        return payload


def redact_archived(db, config):
    if db.connection.execute("PRAGMA user_version").fetchone()[0] < 2:
        return
    for row in db.connection.execute("SELECT * FROM knowledge").fetchall():
        values = [config.replace(row[key]) for key in ("title", "claim", "conditions", "procedure")]
        # Applicability is structured data; redact its string leaves recursively.
        def clean(value):
            if isinstance(value, str):
                return config.replace(value)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value
        applies = dumps(clean(json.loads(row["applies_to"])))
        db.connection.execute("UPDATE knowledge SET title=?,claim=?,conditions=?,procedure=?,applies_to=?,index_title=?,index_body=? WHERE kid=?",
                              (*values, applies, tokenize(values[0]), tokenize("\n".join(values) + applies), row["kid"]))
    for row in db.connection.execute("SELECT rowid,snapshot FROM knowledge_evidence").fetchall():
        db.connection.execute("UPDATE knowledge_evidence SET snapshot=? WHERE rowid=?", (config.replace(row["snapshot"]), row["rowid"]))
