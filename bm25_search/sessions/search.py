from __future__ import annotations

import base64
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import time

from .adapters.base import readonly, timestamp
from .db import Database, dumps
from .models import AGENTS, SessionError
from .tokenizer import match_query, tokenize


# bm25() is negative, so scaling toward zero demotes chunks made only of tool
# calls/results below conversation text with similar term matches.
TOOL_WEIGHT = 0.5
# Native session ids the running client exposes to child processes. As of
# 2026-10 only Claude Code passes its id to MCP servers; Codex and agy expose
# theirs to shell tools only, so they rely on the log-activity fallback, as
# does Cursor (no known variable).
CURRENT_SESSION_ENV = {"CLAUDE_CODE_SESSION_ID": "claude_code", "CODEX_THREAD_ID": "codex", "ANTIGRAVITY_CONVERSATION_ID": "agy"}
ENV_FOR_AGENT = {agent: name for name, agent in CURRENT_SESSION_ENV.items()}
# A started session whose log is idle this long while a newer top-level log of
# the same agent is active is treated as replaced (/clear, /resume, new chat).
IDLE_SECONDS = 600


def touched(locator):
    if "#" in locator:
        # Cursor keeps every conversation in one state.vscdb ("<db>#<composer id>").
        database, cid = locator.rsplit("#", 1)
        try:
            with readonly(database) as connection:
                row = connection.execute("SELECT lastUpdatedAt FROM composerHeaders WHERE composerId=?", (cid,)).fetchone()
        except (OSError, sqlite3.Error, SessionError):
            return None
        return row[0] / 1000 if row and isinstance(row[0], (int, float)) else None
    path = Path(locator)
    return max((p.stat().st_mtime for p in (path, path.with_name(path.name + "-wal")) if p.exists()), default=None)


def current_sessions(db, client_agent=None):
    """Session uids of the calling sessions, following a client that switched sessions.

    A client that does not pass its id to MCP servers (agy) falls back to its
    most recently written top-level session that is still active.
    """
    found, clock = [], time.time()
    for agent in AGENTS:
        native = os.environ.get(ENV_FOR_AGENT.get(agent, ""))
        if not native and agent != client_agent:
            continue
        rows = db.connection.execute("""SELECT s.session_uid,s.native_id,s.parent_id,a.locator FROM sessions s
                                        JOIN sources src ON src.source_id=s.source_id JOIN artifacts a ON a.session_uid=s.session_uid
                                        WHERE src.agent=?""", (agent,)).fetchall()
        if not native:
            active = [(touched(r["locator"]) or 0, r["session_uid"]) for r in rows if r["parent_id"] is None]
            active = [item for item in active if clock - item[0] <= IDLE_SECONDS]
            found.extend([max(active)[1]] if active else [])
            continue
        mine = [r for r in rows if r["native_id"] == native or r["native_id"].startswith(native + "::")]
        if not mine:
            # Not imported yet (or another project): nothing to exclude or replace.
            continue
        own = max((touched(r["locator"]) or 0 for r in mine), default=0)
        if clock - own > IDLE_SECONDS:
            newer = [(touched(r["locator"]) or 0, r["session_uid"]) for r in rows if r["parent_id"] is None and r not in mine]
            newer = [item for item in newer if item[0] > own and clock - item[0] <= IDLE_SECONDS]
            if newer:
                found.append(max(newer)[1])
                continue
        found.extend(r["session_uid"] for r in mine)
    return found


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise SessionError("invalid_arguments")
    return value


def encode(value):
    return base64.urlsafe_b64encode(dumps(dict(v=1, **value)).encode("utf-8")).decode("ascii").rstrip("=")


def decode(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise SessionError("invalid_arguments")
    try:
        parsed = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(parsed, dict) or parsed.get("v") != 1:
            raise ValueError()
        return parsed
    except (ValueError, UnicodeError):
        raise SessionError("invalid_arguments") from None


def size(payload):
    return len(dumps(payload).encode("utf-8"))


def trim_freshness(payload, maximum):
    freshness = payload.get("freshness", {})
    while size(payload) > maximum and freshness.get("sources"):
        freshness["sources"].pop()
        freshness["omitted_sources"] = freshness.get("omitted_sources", 0) + 1
    while size(payload) > maximum and freshness.get("partial_sources"):
        freshness["partial_sources"].pop()


def allowed_sources(config):
    allowed = config.data.get("allowed_source_ids")
    return allowed if isinstance(allowed, list) else None


def authorize(db, config, sid):
    if not isinstance(sid, str) or len(sid) > 200:
        raise SessionError("invalid_arguments")
    session = db.connection.execute("SELECT * FROM sessions WHERE session_uid=?", (sid,)).fetchone()
    allowed = allowed_sources(config)
    if not session or (allowed is not None and session["source_id"] not in allowed):
        raise SessionError("not_found_or_not_allowed")
    return session


def search(config, query, queries=None, agent="all", top_k=5, since=None, until=None, max_bytes=16384, exclude_session_ids=None, include_current=False, client_agent=None):
    integer(top_k, 1, 50)
    integer(max_bytes, 2048, 65536)
    if agent not in ("all", *AGENTS):
        raise SessionError("invalid_arguments")
    if queries is None:
        queries = []
    if not isinstance(queries, list) or len(queries) > 4:
        raise SessionError("invalid_arguments")
    variants = list(dict.fromkeys([query, *queries])) if all(isinstance(q, str) for q in [query, *queries]) else []
    if not variants:
        raise SessionError("invalid_query")
    expressions = [match_query(q) for q in variants]
    excluded = exclude_session_ids if exclude_session_ids is not None else []
    if not isinstance(excluded, list) or len(excluded) > 16 or not all(isinstance(s, str) and len(s) <= 200 for s in excluded):
        raise SessionError("invalid_arguments")
    if not isinstance(include_current, bool):
        raise SessionError("invalid_arguments")
    if since is not None and timestamp(since) is None or until is not None and timestamp(until) is None:
        raise SessionError("invalid_arguments")
    since, until = timestamp(since), timestamp(until)
    if since and until and since >= until:
        raise SessionError("invalid_arguments")
    with Database(config.root) as db:
        filters, params = [], []
        if agent != "all":
            filters.append("src.agent=?")
            params.append(agent)
        allowed = allowed_sources(config)
        if allowed is not None:
            filters.append("s.source_id IN (" + ",".join("?" for _ in allowed) + ")" if allowed else "0")
            params.extend(allowed)
        if excluded:
            filters.append("s.session_uid NOT IN (" + ",".join("?" for _ in excluded) + ")")
            params.extend(excluded)
        current = [] if include_current else current_sessions(db, client_agent)
        if current:
            filters.append("s.session_uid NOT IN (" + ",".join("?" for _ in current) + ")")
            params.extend(current)
        if since:
            filters.append("c.occurred_at>=?")
            params.append(since)
        if until:
            filters.append("c.occurred_at<?")
            params.append(until)
        clause = " AND " + " AND ".join(filters) if filters else ""
        sql = """SELECT c.*,s.title,s.completeness,src.agent,src.source_id,
                 bm25(session_fts,2.0,1.0,1.5) * CASE WHEN EXISTS(SELECT 1 FROM json_each(c.parts) j
                   JOIN messages m ON m.message_uid=json_extract(j.value,'$.message_uid') WHERE m.kind='message')
                   THEN 1.0 ELSE ? END AS score
                 FROM session_fts JOIN chunks c ON c.chunk_uid=session_fts.rowid
                 JOIN sessions s ON s.session_uid=c.session_uid JOIN sources src ON src.source_id=s.source_id
                 WHERE session_fts MATCH ?""" + clause + " ORDER BY score,c.occurred_at DESC,c.chunk_uid LIMIT ?"
        limit, result_rows, exhausted = max(40, top_k * 4), [], False
        while True:
            fused = {}
            exhausted = True
            for expression in expressions:
                rows = db.connection.execute(sql, (TOOL_WEIGHT, expression, *params, limit)).fetchall()
                exhausted = exhausted and len(rows) < limit
                for rank, row in enumerate(rows, 1):
                    entry = fused.setdefault(row["chunk_uid"], dict(row, fusion=0.0))
                    entry["fusion"] += 1 / (60 + rank)
            ordered = sorted(fused.values(), key=lambda r: (-r["fusion"], r["score"], r["chunk_uid"]))
            groups, per_session, result_rows = {}, Counter(), []
            for row in ordered:
                group = row["chunk_hash"]
                if group in groups:
                    other = dict(source_agent=row["agent"], session_id=row["session_uid"])
                    if other not in groups[group]["other_sources"] and len(groups[group]["other_sources"]) < 8:
                        groups[group]["other_sources"].append(other)
                    continue
                if per_session[row["session_uid"]] >= 2:
                    continue
                parts = json.loads(row["parts"])
                if not parts:
                    continue
                # Prefer the part containing a query term rather than the first
                # message in a combined conversation chunk, conversation text first.
                tokens = set(tokenize(query).split())
                part, best = parts[0], None
                for candidate in parts:
                    message = db.connection.execute("SELECT body,kind FROM messages WHERE message_uid=?", (candidate["message_uid"],)).fetchone()
                    if message and tokens.intersection(tokenize(message[0][candidate["start_char"]:candidate["end_char"]]).split()):
                        if message[1] == "message":
                            part = candidate
                            break
                        best = best or candidate
                else:
                    part = best or part
                message = db.connection.execute("SELECT redacted,truncated FROM messages WHERE message_uid=?", (part["message_uid"],)).fetchone()
                excerpt = db.connection.execute("SELECT body FROM messages WHERE message_uid=?", (part["message_uid"],)).fetchone()[0][part["start_char"]:part["end_char"]]
                entry = dict(session_id=row["session_uid"], source_agent=row["agent"], title=row["title"][:160], occurred_at=row["occurred_at"],
                             anchor=encode(dict(sid=row["session_uid"], mid=part["message_uid"], hash=part["body_hash"], start=part["start_char"], end=part["end_char"])),
                             snippet=excerpt[:300], score=row["score"], completeness=row["completeness"],
                             redacted=bool(message[0]), truncated=bool(message[1]), other_sources=[])
                if len(expressions) > 1:
                    entry["fusion_score"] = row["fusion"]
                groups[group] = entry
                result_rows.append(entry)
                per_session[row["session_uid"]] += 1
            if len(result_rows) >= top_k or exhausted or limit >= 5000:
                break
            limit = min(5000, limit * 2)
        chosen = result_rows[:top_k]
        payload = dict(status="ok" if chosen else "zero_match", scope=dict(project_id=db.project_uid, agent=agent),
                       freshness=db.freshness(allowed), results=chosen, omitted_count=max(0, len(result_rows) - len(chosen)), warnings=[])
        if not exhausted and limit >= 5000:
            payload["warnings"].append("candidate_limit_reached")
        if since or until:
            payload["warnings"].append("unknown_event_timestamps_excluded")
        if current:
            payload["warnings"].append("current_session_excluded")
            payload["excluded_current"] = current
        while size(payload) > max_bytes and payload["results"]:
            payload["results"].pop()
            payload["omitted_count"] += 1
        trim_freshness(payload, max_bytes)
        if size(payload) > max_bytes:
            raise SessionError("budget_too_small")
        return payload


def read(config, session_id, anchor=None, before=3, after=3, cursor=None, max_bytes=16384):
    integer(before, 0, 20)
    integer(after, 0, 20)
    integer(max_bytes, 2048, 65536)
    if anchor is not None and cursor is not None:
        raise SessionError("invalid_arguments")
    with Database(config.root) as db:
        session = authorize(db, config, session_id)
        rows = db.connection.execute("SELECT * FROM messages WHERE session_uid=? ORDER BY ordinal", (session_id,)).fetchall()
        ordinal, offset, stop, anchor_ordinal, anchor_offset = max(0, len(rows) - before - after - 1), 0, len(rows), -1, 0
        if anchor is not None:
            value = decode(anchor)
            if value.get("sid") != session_id:
                raise SessionError("invalid_arguments")
            match = next((i for i, row in enumerate(rows) if row["message_uid"] == value.get("mid")), None)
            if match is None or rows[match]["body_hash"] != value.get("hash"):
                raise SessionError("stale_anchor")
            start = integer(value.get("start"), 0, len(rows[match]["body"]))
            end = integer(value.get("end"), start, len(rows[match]["body"]))
            ordinal, stop = max(0, match - before), min(len(rows), match + after + 1)
            anchor_ordinal, anchor_offset = match, max(0, start - 300)
        elif cursor is not None:
            value = decode(cursor)
            if value.get("sid") != session_id:
                raise SessionError("invalid_arguments")
            if value.get("rev") != session["revision"]:
                raise SessionError("stale_cursor")
            ordinal = integer(value.get("ord"), 0, len(rows))
            stop = integer(value.get("stop"), ordinal, len(rows))
            offset = integer(value.get("off"), 0, len(rows[ordinal]["body"]) if ordinal < len(rows) else 0)
            anchor_ordinal = integer(value.get("aord", -1), -1, max(-1, len(rows) - 1))
            anchor_offset = integer(value.get("aoff", 0), 0, len(rows[anchor_ordinal]["body"]) if anchor_ordinal >= 0 else 0)
        last = max((row["occurred_at"] for row in rows if row["occurred_at"]), default=None)
        payload = dict(status="ok", session=dict(session_id=session_id, title=session["title"], completeness=session["completeness"],
                                                diagnostics=json.loads(session["diagnostics"])[:5], last_event_at=last,
                                                has_later_messages=stop < len(rows), metadata=json.loads(session["metadata"])), freshness=db.freshness(allowed_sources(config)), messages=[], next_cursor=None)
        trim_freshness(payload, max_bytes // 2)

        def continuation(index, char):
            return encode(dict(sid=session_id, rev=session["revision"], ord=index, off=char, stop=stop, aord=anchor_ordinal, aoff=anchor_offset)) if index < stop else None

        while ordinal < stop:
            row = rows[ordinal]
            start = offset
            if ordinal == anchor_ordinal:
                start = max(start, anchor_offset)
            message = dict(message_id=row["message_uid"], role=row["role"], kind=row["kind"], body="", body_hash=row["body_hash"],
                           occurred_at=row["occurred_at"], call_id=row["call_id"], locator=json.loads(row["locator"]),
                           redacted=bool(row["redacted"]), truncated=bool(row["truncated"]), inherited=bool(row["inherited"]), start_char=start, end_char=start)
            payload["messages"].append(message)
            low, high = 0, len(row["body"]) - start
            message.update(body=row["body"][start:], end_char=len(row["body"]))
            payload["next_cursor"] = continuation(ordinal + 1, 0)
            if size(payload) <= max_bytes:
                low = high
            else:
                high = max(0, high - 1)
            while low < high:
                length = (low + high + 1) // 2
                message.update(body=row["body"][start:start + length], end_char=start + length)
                done = start + length == len(row["body"])
                payload["next_cursor"] = continuation(ordinal + 1, 0) if done else continuation(ordinal, start + length)
                if size(payload) <= max_bytes:
                    low = length
                else:
                    high = length - 1
            message.update(body=row["body"][start:start + low], end_char=start + low)
            done = start + low == len(row["body"])
            payload["next_cursor"] = continuation(ordinal + 1, 0) if done else continuation(ordinal, start + low)
            if size(payload) > max_bytes or (low == 0 and start < len(row["body"])):
                payload["messages"].pop()
                payload["next_cursor"] = continuation(ordinal, start)
                if not payload["messages"]:
                    raise SessionError("budget_too_small")
                break
            if not done:
                break
            ordinal, offset = ordinal + 1, 0
        if ordinal >= stop:
            payload["next_cursor"] = None
        return payload
