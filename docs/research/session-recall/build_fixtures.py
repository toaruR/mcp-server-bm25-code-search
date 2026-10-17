"""Build fully synthetic fixtures from T0's observed envelopes, never user logs."""
from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[3]
DEST = ROOT / "tests/sessions/fixtures"
WHEN = "2026-01-01T00:00:00Z"
PROJECT = "C:/fixture/project"
URI = "file:///C:/fixture/project"
USER = "[synthetic] 差分取り込みの競合を調べてください。"
ANSWER = "[synthetic] generationを比較して古いsnapshotを破棄します。"
TOOL_OUTPUT = "[synthetic] exit_code=0; 2 checks passed"


def save(relative, value):
    path = DEST / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def jsonl(relative, events):
    save(relative, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))


def varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out + bytes([value]))


def number(field, value):
    return varint(field << 3) + varint(value)


def blob(field, value):
    value = value.encode("utf-8") if isinstance(value, str) else value
    return varint((field << 3) | 2) + varint(len(value)) + value


def literal(value):
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


def insert(table, row):
    return "INSERT INTO " + table + " VALUES(" + ",".join(map(literal, row)) + ");\n"


def build():
    common = {"cwd": PROJECT, "sessionId": "synthetic-claude", "version": "2.1.300", "timestamp": WHEN, "isSidechain": False}
    events = [
        dict(common, type="user", uuid="synthetic-u1", parentUuid=None, message={"role": "user", "content": USER}),
        dict(common, type="assistant", uuid="synthetic-a1", parentUuid="synthetic-u1", message={"id": "synthetic-api", "role": "assistant", "content": [
            {"type": "thinking", "thinking": "[synthetic] private reasoning, must be excluded", "signature": "synthetic-signature"},
            {"type": "tool_use", "id": "synthetic-call", "name": "Bash", "input": {"command": "echo synthetic"}},
        ]}),
        dict(common, type="user", uuid="synthetic-result", parentUuid="synthetic-a1", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "synthetic-call", "content": TOOL_OUTPUT, "is_error": False},
        ]}),
        dict(common, type="assistant", uuid="synthetic-a2", parentUuid="synthetic-result", message={"id": "synthetic-api-2", "role": "assistant", "content": [{"type": "text", "text": ANSWER}]}),
    ]
    jsonl("claude_code/jsonl-v2/session.jsonl", events)
    events = [
        {"type": "session_meta", "timestamp": WHEN, "payload": {"id": "synthetic-child", "session_id": "synthetic-child", "cwd": PROJECT, "cli_version": "0.162.0-alpha.17.2", "thread_source": "subagent", "agent_path": "/root/synthetic"}},
        {"type": "session_meta", "timestamp": WHEN, "payload": {"id": "synthetic-parent", "cwd": PROJECT}},
        {"type": "response_item", "timestamp": WHEN, "payload": {"type": "message", "id": "synthetic-user", "role": "user", "content": [{"type": "input_text", "text": USER}]}},
        {"type": "event_msg", "timestamp": WHEN, "payload": {"type": "item_completed", "item": {"type": "UserMessage", "id": "synthetic-user-event", "content": [{"type": "input_text", "text": USER}]}}},
        {"type": "response_item", "timestamp": WHEN, "payload": {"type": "custom_tool_call", "id": "synthetic-tool", "call_id": "synthetic-call", "name": "functions.exec", "input": "text('synthetic')"}},
        {"type": "response_item", "timestamp": WHEN, "payload": {"type": "custom_tool_call_output", "id": "synthetic-result", "call_id": "synthetic-call", "output": [{"type": "text", "text": TOOL_OUTPUT}]}},
        {"type": "response_item", "timestamp": WHEN, "payload": {"type": "reasoning", "summary": [], "encrypted_content": "synthetic-encrypted-placeholder"}},
        {"type": "response_item", "timestamp": WHEN, "payload": {"type": "message", "id": "synthetic-answer", "role": "assistant", "content": [{"type": "output_text", "text": ANSWER}]}},
        {"type": "event_msg", "timestamp": WHEN, "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "id": "synthetic-answer", "content": [{"type": "output_text", "text": ANSWER}]}}},
    ]
    jsonl("codex/rollout-v1/session.jsonl", events)
    stamp = blob(1, number(1, 1767225600))
    call = blob(1, "synthetic-call") + blob(2, "run_command")
    tool_meta = stamp + blob(4, call)
    user = number(1, 14) + number(4, 3) + blob(5, stamp) + blob(19, blob(2, USER))
    assistant = number(1, 15) + number(4, 3) + blob(5, stamp) + blob(20, blob(1, ANSWER) + blob(3, "[synthetic] excluded thinking") + blob(7, call))
    command = blob(23, "echo synthetic") + blob(2, PROJECT) + number(6, 0) + blob(21, blob(1, TOOL_OUTPUT))
    tool = number(1, 21) + number(4, 3) + blob(5, tool_meta) + blob(28, command)
    trajectory = blob(1, blob(1, URI)) + blob(2, number(1, 1767225600)) + blob(7, URI)
    sql = """-- Fully synthetic protobuf values; field mapping documented in T0 report.
CREATE TABLE trajectory_meta(trajectory_id TEXT,cascade_id TEXT,trajectory_type INTEGER,source INTEGER);
CREATE TABLE steps(idx INTEGER PRIMARY KEY,step_type INTEGER,status INTEGER,has_subtrajectory NUMERIC,metadata BLOB,error_details BLOB,permissions BLOB,task_details BLOB,render_info BLOB,step_payload BLOB,step_format INTEGER);
CREATE TABLE trajectory_metadata_blob(id TEXT PRIMARY KEY,data BLOB);
"""
    sql += insert("trajectory_meta", ("synthetic-trajectory", "synthetic-agy", 0, 0))
    sql += insert("trajectory_metadata_blob", ("synthetic-meta", trajectory))
    for idx, kind, metadata, payload in ((0, 14, stamp, user), (1, 15, stamp, assistant), (2, 21, tool_meta, tool)):
        sql += insert("steps", (idx, kind, 3, 0, metadata, None, None, None, None, payload, 0))
    save("agy/trajectory-v0/conversation.sql", sql)
    jsonl("agy/trajectory-v0/transcript.jsonl", [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": WHEN, "content": USER},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": WHEN, "content": "[synthetic] generation…", "truncated_fields": ["content"], "tool_calls": [{"name": "run_command", "args": {"CommandLine": "echo synthetic", "Cwd": PROJECT}}]},
        {"step_index": 2, "source": "MODEL", "type": "RUN_COMMAND", "status": "DONE", "created_at": WHEN, "exit_code": 0, "content": TOOL_OUTPUT},
    ])
    global_sql = """CREATE TABLE cursorDiskKV(key TEXT,value BLOB);
CREATE TABLE composerHeaders(composerId TEXT,workspaceId TEXT,createdAt INTEGER,lastUpdatedAt INTEGER,isArchived INTEGER,isSubagent INTEGER,recency INTEGER,checkpointAt INTEGER,value TEXT,subagentTypeName TEXT);
"""
    composer = {"composerId": "synthetic-composer", "conversation": [], "createdAt": 1767225600000, "fullConversationHeadersOnly": [{"bubbleId": "synthetic-u", "type": 1}, {"bubbleId": "synthetic-a", "type": 2}, {"bubbleId": "synthetic-tool", "type": 2}]}
    global_sql += insert("cursorDiskKV", ("composerData:synthetic-composer", json.dumps(composer)))
    for bid, value in (("synthetic-u", {"bubbleId": "synthetic-u", "type": 1, "text": USER, "createdAt": WHEN}), ("synthetic-a", {"bubbleId": "synthetic-a", "type": 2, "text": ANSWER, "isThought": False}), ("synthetic-tool", {"bubbleId": "synthetic-tool", "type": 2, "text": "", "toolFormerData": {"toolCallId": "synthetic-call", "name": "run_terminal_cmd", "rawArgs": "{\"command\":\"echo synthetic\"}", "status": "completed", "result": TOOL_OUTPUT}, "timingInfo": {"clientStartTime": 1767225600000}})):
        global_sql += insert("cursorDiskKV", ("bubbleId:synthetic-composer:" + bid, json.dumps(value, ensure_ascii=False)))
    # A null/tombstoned value must not be reported as a successful empty message.
    global_sql += insert("cursorDiskKV", ("bubbleId:synthetic-deleted:synthetic-null", None))
    inline = {"composerId": "synthetic-inline", "conversation": [{"bubbleId": "synthetic-inline-u", "type": 1, "text": USER}, {"bubbleId": "synthetic-inline-a", "type": 2, "text": ANSWER}]}
    global_sql += insert("cursorDiskKV", ("composerData:synthetic-inline", json.dumps(inline, ensure_ascii=False)))
    global_sql += insert("composerHeaders", ("synthetic-composer", "synthetic-workspace", 1767225600000, 1767225601000, 0, 0, 0, 0, json.dumps({"composerId": "synthetic-composer", "workspaceIdentifier": {"id": "synthetic-workspace"}}), None))
    save("cursor/storage-v1/global.sql", global_sql)
    workspace_sql = "CREATE TABLE ItemTable(key TEXT,value BLOB);\n"
    workspace_sql += insert("ItemTable", ("composer.composerData", json.dumps({"selectedComposerIds": ["synthetic-composer"], "allComposers": [{"type": "head", "composerId": "synthetic-inline", "createdAt": 1767225600000}]})))
    save("cursor/storage-v1/workspace.sql", workspace_sql)
    save("cursor/storage-v1/workspace.json", json.dumps({"folder": URI}, indent=2) + "\n")
    save("manifest.json", json.dumps({
        "provenance": "Fully synthetic content and identifiers; native field structure observed locally. No original transcript copied.",
        "project": PROJECT, "user_text": USER, "assistant_text": ANSWER, "tool_output": TOOL_OUTPUT,
        "variants": {"claude_code": "jsonl-v2", "codex": "rollout-v1", "agy": "trajectory-v0 (legacy, ide, cli)", "cursor": "storage-v1"},
        "edge_cases": ["Claude user-envelope tool_result", "Codex inherited session_meta", "Codex duplicate UserMessage event", "agy truncated transcript with full SQLite body", "Cursor inline and external bubbles", "Cursor null tombstone"],
    }, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    build()
