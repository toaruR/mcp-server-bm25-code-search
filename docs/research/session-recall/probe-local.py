"""T0 read-only format survey; stdout/output contain counts, never conversation text.

This is research tooling, not a production importer. No client is launched and no
source database is written. --local explicitly opts into the known local roots.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys


EVENT_TYPES = frozenset((
    "user", "assistant", "session_meta", "response_item", "event_msg",
    "turn_context", "USER_INPUT", "PLANNER_RESPONSE", "RUN_COMMAND", "GENERIC",
    "VIEW_FILE", "MCP_TOOL", "ERROR_MESSAGE", "SYSTEM_MESSAGE", "CHECKPOINT",
    "CONVERSATION_HISTORY", "KNOWLEDGE_ARTIFACTS", "CODE_ACTION", "GREP_SEARCH",
    "LIST_DIRECTORY", "READ_URL_CONTENT", "ASK_QUESTION",
))
PAYLOAD_TYPES = frozenset((
    "message", "function_call", "function_call_output", "custom_tool_call",
    "custom_tool_call_output", "reasoning", "user_message", "agent_message",
    "item_completed", "task_started", "task_complete", "token_count",
))


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("truncated_varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("overflow_varint")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("overflow_varint")


def wire_fields(data):
    """Bounded protobuf wire witness; unknown groups deliberately unsupported."""
    if not isinstance(data, bytes) or len(data) > 16 * 1024 * 1024:
        raise ValueError("invalid_blob")
    result, offset = [], 0
    while offset < len(data):
        key, offset = varint(data, offset)
        number, wire = key >> 3, key & 7
        if not 0 < number < (1 << 29):
            raise ValueError("invalid_field")
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if offset + size > len(data):
                raise ValueError("truncated_field")
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError("unsupported_wire")
        result.append((number, wire, value))
        if len(result) > 100000:
            raise ValueError("field_budget_exceeded")
    return result


def wire_bytes(data, number):
    return [value for field, wire, value in wire_fields(data)
            if field == number and wire == 2]


def agy_text_witness(step_type, step_format, payload):
    """Witness for observed format=0 wrappers and scalar text only.

    Does not interpret thinking, scopes/media, unknown steps or generic tools.
    Returned text is used in-memory by tests/probes, never printed by main().
    """
    if step_format != 0 or step_type not in (14, 15):
        raise ValueError("unsupported_step")
    fields = wire_fields(payload)
    if not any(n == 1 and w == 0 and v == step_type for n, w, v in fields):
        raise ValueError("wrapper_type_mismatch")
    parts = wire_bytes(payload, 19 if step_type == 14 else 20)
    if len(parts) != 1:
        raise ValueError("missing_payload")
    if step_type == 14:
        candidates = wire_bytes(parts[0], 2) or wire_bytes(parts[0], 1)
    else:
        candidates = wire_bytes(parts[0], 8) or wire_bytes(parts[0], 1)
    return "\n".join(value.decode("utf-8") for value in candidates)


@contextmanager
def readonly_db(path, historical=False):
    """Never retry with write access. Immutable is for stable no-WAL survey only."""
    path = Path(path).resolve()
    wal = Path(str(path) + "-wal")
    before = (path.stat().st_size, path.stat().st_mtime_ns)
    if historical and wal.exists():
        raise ValueError("immutable_requires_no_wal")
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if historical else "")
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()
    if historical and (wal.exists() or before != (path.stat().st_size, path.stat().st_mtime_ns)):
        raise ValueError("historical_source_changed")


def jsonl_survey(root, pattern, samples):
    files = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    stats, kinds, subtypes, blocks, versions = Counter(), Counter(), Counter(), Counter(), Counter()
    for path in files[:samples]:
        stats["sample_files"] += 1
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            for index, line in enumerate(stream):
                if index >= 5000:
                    stats["line_budget_files"] += 1
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    stats["invalid_lines"] += 1
                    continue
                if not isinstance(event, dict):
                    stats["non_object_lines"] += 1
                    continue
                stats["records"] += 1
                kind = event.get("type")
                kinds[kind if isinstance(kind, str) and kind in EVENT_TYPES else "other"] += 1
                stats["truncated_records"] += bool(event.get("truncated_fields"))
                stats["with_cwd"] += isinstance(event.get("cwd"), str)
                stats["with_timestamp"] += isinstance(event.get("timestamp", event.get("created_at")), str)
                payload = event.get("payload")
                if isinstance(payload, dict):
                    subtype = payload.get("type")
                    subtypes[subtype if isinstance(subtype, str) and subtype in PAYLOAD_TYPES else "other"] += 1
                version = event.get("version")
                if kind == "session_meta" and isinstance(payload, dict):
                    version = payload.get("cli_version")
                if isinstance(version, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+_-]{0,60}", version):
                    versions[version] += 1
                message = event.get("message", payload)
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                tag = block.get("type")
                                blocks[tag if isinstance(tag, str) and tag in ("text", "input_text", "output_text", "tool_use", "tool_result", "thinking", "image") else "other"] += 1
    return {"artifact_count": len(files), "counts": dict(stats), "event_types": dict(kinds),
            "payload_types": dict(subtypes), "content_block_types": dict(blocks), "writer_versions": dict(versions)}


def codex_survey(root, samples):
    result = jsonl_survey(root, "**/*.jsonl", samples)
    counts = Counter()
    files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns, reverse=True)[:samples]
    for path in files:
        ids, items = set(), []
        with path.open(encoding="utf-8") as stream:
            for index, line in enumerate(stream):
                if index >= 5000:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if event.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") in ("user", "assistant"):
                    if isinstance(payload.get("id"), str):
                        ids.add(payload["id"])
                item = payload.get("item")
                if event.get("type") == "event_msg" and payload.get("type") == "item_completed" and isinstance(item, dict) and item.get("type") in ("UserMessage", "AgentMessage"):
                    items.append(item.get("id"))
        counts["user_agent_events"] += len(items)
        counts["id_matches_response_item"] += sum(isinstance(item, str) and item in ids for item in items)
    result["event_id_correlation"] = dict(counts)
    return result


def agy_survey(root, samples):
    result = {"transcript": jsonl_survey(root, "brain/*/.system_generated/logs/transcript.jsonl", samples)}
    files = sorted((root / "conversations").glob("*.db"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    result["database_count"] = len(files)
    # Avoid active files when the sandbox cannot create SQLite shared-memory files.
    selected = [p for p in files if not Path(str(p) + "-wal").exists()][:samples]
    stats, formats, types = Counter(), Counter(), Counter()
    for path in selected:
        try:
            with readonly_db(path, historical=True) as connection:
                planner_ids, result_ids = set(), []
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("integrity_check_failed")
                metadata = connection.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
                if metadata:
                    uris = wire_bytes(metadata[0], 7)
                    for workspace in wire_bytes(metadata[0], 1):
                        uris.extend(wire_bytes(workspace, 1))
                    stats["with_workspace_uri"] += bool(uris)
                for kind, fmt, payload, meta in connection.execute("SELECT step_type,step_format,step_payload,metadata FROM steps"):
                    stats["steps"] += 1
                    formats[str(fmt) if isinstance(fmt, int) else "other"] += 1
                    types[str(kind) if isinstance(kind, int) else "other"] += 1
                    if kind == 15:
                        for body in wire_bytes(payload, 20):
                            for call in wire_bytes(body, 7):
                                planner_ids.update(wire_bytes(call, 1))
                    else:
                        for call in wire_bytes(meta, 4):
                            result_ids.extend(wire_bytes(call, 1))
                    if kind not in (14, 15):
                        continue
                    stats["user_assistant_steps"] += 1
                    text = agy_text_witness(kind, fmt, payload)
                    stats["with_scalar_body"] += bool(text)
                    stats["embedded_metadata_matches"] += wire_bytes(payload, 5) == [meta]
                stats["historical_db_samples"] += 1
                stats["planner_call_ids"] += len(planner_ids)
                stats["result_call_ids"] += len(result_ids)
                stats["matching_result_call_ids"] += sum(value in planner_ids for value in result_ids)
        except (sqlite3.Error, OSError, ValueError, UnicodeError):
            stats["unsupported_or_unreadable_db_samples"] += 1
    result.update({"database_counts": dict(stats), "step_formats": dict(formats), "step_types": dict(types),
                   "survey_mode": "stable_historical_no_wal_only"})
    return result


def cursor_survey(root):
    stats, roles = Counter(), Counter()
    workspace_ids = set()
    for folder in (root / "workspaceStorage").iterdir():
        if not folder.is_dir():
            continue
        workspace_ids.add(folder.name)
        meta = folder / "workspace.json"
        if meta.exists():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                for key in ("folder", "workspace", "configuration"):
                    stats["workspace_" + key] += key in data
            except (ValueError, OSError):
                stats["invalid_workspace_metadata"] += 1
        db = folder / "state.vscdb"
        if not db.exists():
            continue
        try:
            with readonly_db(db) as connection:
                row = connection.execute("SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()
                if row:
                    data = json.loads(row[0])
                    stats["workspace_composer_metadata"] += 1
                    stats["workspace_all_composers"] += len(data.get("allComposers", []))
        except (sqlite3.Error, ValueError, OSError):
            stats["unreadable_workspace_db"] += 1
    with readonly_db(root / "globalStorage/state.vscdb") as connection:
        for _, wid in connection.execute("SELECT composerId,workspaceId FROM composerHeaders"):
            stats["headers"] += 1
            stats["header_workspace_dir_match"] += wid in workspace_ids
        for key, value in connection.execute("SELECT key,value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
            stats["composer_rows"] += 1
            if not value:
                stats["null_composer_rows"] += 1
                continue
            data = json.loads(value)
            inline = data.get("conversation", [])
            refs = data.get("fullConversationHeadersOnly", [])
            stats["composers_with_inline"] += bool(inline)
            stats["composers_with_header_refs"] += bool(refs)
            for header in refs:
                bid = header.get("bubbleId")
                if not isinstance(bid, str):
                    stats["invalid_bubble_ref"] += 1
                    continue
                bkey = "bubbleId:" + key.split(":", 1)[1] + ":" + bid
                row = connection.execute("SELECT value FROM cursorDiskKV WHERE key=?", (bkey,)).fetchone()
                stats["bubble_refs"] += 1
                if not row or not row[0]:
                    stats["missing_bubble_refs"] += 1
            for bubble in inline:
                if isinstance(bubble, dict):
                    kind = bubble.get("type")
                    roles[str(kind) if kind in (1, 2) else "other"] += 1
        for (value,) in connection.execute("SELECT value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
            stats["bubble_rows"] += 1
            if not value:
                stats["null_bubble_rows"] += 1
                continue
            data = json.loads(value)
            stats["bubble_json_rows"] += 1
            stats["bubble_with_text"] += isinstance(data.get("text"), str) and bool(data["text"])
            stats["bubble_with_tool"] += isinstance(data.get("toolFormerData"), dict)
            stats["bubble_with_created_at"] += bool(data.get("createdAt"))
    return {"counts": dict(stats), "inline_role_types": dict(roles)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="survey known roots under the current user's home")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.local or not 1 <= args.samples <= 10:
        parser.error("--local and --samples 1..10 required")
    home = Path.home()
    result = {"format": "t0-counts-v1", "contains_conversation_text": False,
              "observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": ".".join(map(str, sys.version_info[:3])), "sqlite": sqlite3.sqlite_version},
              "sample_policy": {"latest_files_per_source": args.samples, "max_lines_per_jsonl": 5000}, "sources": {}}
    jobs = {
        "claude_code": lambda: jsonl_survey(home / ".claude/projects", "**/*.jsonl", args.samples),
        "codex": lambda: codex_survey(home / ".codex/sessions", args.samples),
        "agy_legacy": lambda: agy_survey(home / ".gemini/antigravity", args.samples),
        "agy_ide": lambda: agy_survey(home / ".gemini/antigravity-ide", args.samples),
        "agy_cli": lambda: agy_survey(home / ".gemini/antigravity-cli", args.samples),
        "cursor": lambda: cursor_survey(Path(os.environ["APPDATA"]) / "Cursor/User"),
    }
    for label, job in jobs.items():
        try:
            result["sources"][label] = job()
        except (OSError, sqlite3.Error, ValueError, UnicodeError):
            result["sources"][label] = {"status": "unavailable_or_unsupported"}
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
