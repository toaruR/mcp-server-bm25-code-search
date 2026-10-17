"""Characterization of observed native formats; these are not importer E2E tests."""
import importlib.util
import json
from pathlib import Path
import re
import sqlite3

import pytest


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
SPEC = importlib.util.spec_from_file_location(
    "t0_probe", ROOT / "docs/research/session-recall/probe_local.py"
)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def lines(relative):
    return [json.loads(line) for line in (FIXTURES / relative).read_text(encoding="utf-8").splitlines()]


def database(relative):
    connection = sqlite3.connect(":memory:")
    connection.executescript((FIXTURES / relative).read_text(encoding="utf-8"))
    return connection


@pytest.fixture
def expected():
    return json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def test_claude_tool_result_user_envelope_is_not_a_human_message(expected):
    events = lines("claude_code/jsonl-v2/session.jsonl")
    human = [e for e in events if e["type"] == "user" and isinstance(e["message"]["content"], str)]
    assert len(human) == 1
    assert human[0]["message"]["content"] == expected["user_text"]
    call = events[1]["message"]["content"][1]
    result = events[2]["message"]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == call["id"]
    assert result["content"] == expected["tool_output"]
    # Thinking is a separate block; concatenating every string would leak it.
    answer = [b["text"] for e in events if e["type"] == "assistant"
              for b in e["message"]["content"] if b["type"] == "text"]
    assert answer == [expected["assistant_text"]]


def test_codex_first_meta_identity_and_event_duplicates(expected):
    events = lines("codex/rollout-v1/session.jsonl")
    metas = [e["payload"] for e in events if e["type"] == "session_meta"]
    assert metas[0]["id"] == "synthetic-child"
    assert metas[-1]["id"] == "synthetic-parent"
    messages = [e["payload"] for e in events if e["type"] == "response_item" and e["payload"]["type"] == "message"]
    mirrored = [e["payload"]["item"] for e in events if e["type"] == "event_msg"]
    assert mirrored[0]["id"] != messages[0]["id"]
    assert mirrored[0]["content"] == messages[0]["content"]
    assert mirrored[1]["id"] == messages[1]["id"]
    assert mirrored[1]["content"] == messages[1]["content"]
    assert len(messages) == 2
    call = next(e["payload"] for e in events if e["type"] == "response_item" and e["payload"]["type"] == "custom_tool_call")
    result = next(e["payload"] for e in events if e["type"] == "response_item" and e["payload"]["type"] == "custom_tool_call_output")
    assert call["call_id"] == result["call_id"]
    assert result["output"][0]["text"] == expected["tool_output"]


def test_agy_sqlite_recovers_full_text_and_has_independent_project_metadata(expected):
    with database("agy/trajectory-v0/conversation.sql") as connection:
        rows = connection.execute("SELECT idx,step_type,step_format,step_payload,metadata FROM steps ORDER BY idx").fetchall()
        assert [PROBE.agy_text_witness(kind, fmt, payload) for _, kind, fmt, payload, _ in rows[:2]] == [expected["user_text"], expected["assistant_text"]]
        for _, _, _, payload, metadata in rows:
            assert PROBE.wire_bytes(payload, 5) == [metadata]
        meta = connection.execute("SELECT data FROM trajectory_metadata_blob").fetchone()[0]
        assert PROBE.wire_bytes(meta, 7) == [b"file:///C:/fixture/project"]
        transcript = lines("agy/trajectory-v0/transcript.jsonl")
        assert transcript[1]["truncated_fields"] == ["content"]
        assert transcript[1]["content"] != expected["assistant_text"]
        command = PROBE.wire_bytes(rows[2][3], 28)[0]
        output = PROBE.wire_bytes(command, 21)[0]
        assert PROBE.wire_bytes(output, 1)[0].decode() == expected["tool_output"]
        assert any(n == 6 and w == 0 and v == 0 for n, w, v in PROBE.wire_fields(command))


def test_agy_call_link_and_timestamp_are_distinct_from_body():
    with database("agy/trajectory-v0/conversation.sql") as connection:
        planner, tool = connection.execute("SELECT step_payload,metadata FROM steps WHERE idx IN (1,2) ORDER BY idx").fetchall()
        planner_body = PROBE.wire_bytes(planner[0], 20)[0]
        call = PROBE.wire_bytes(planner_body, 7)[0]
        result_call = PROBE.wire_bytes(tool[1], 4)[0]
        assert PROBE.wire_bytes(call, 1) == PROBE.wire_bytes(result_call, 1)
        stamp = PROBE.wire_bytes(tool[1], 1)[0]
        assert PROBE.wire_fields(stamp) == [(1, 0, 1767225600)]


def test_agy_unknown_format_is_rejected_not_empty_success():
    with database("agy/trajectory-v0/conversation.sql") as connection:
        payload = connection.execute("SELECT step_payload FROM steps WHERE idx=0").fetchone()[0]
        with pytest.raises(ValueError, match="unsupported_step"):
            PROBE.agy_text_witness(14, 99, payload)
        with pytest.raises(ValueError, match="wrapper_type_mismatch"):
            PROBE.agy_text_witness(15, 0, payload)


@pytest.mark.parametrize("raw", [b"\x00", b"\x0a\x08x", b"\x80", b"\x0b", b"\x08" + b"\xff" * 10])
def test_protobuf_corruption_is_not_silently_accepted(raw):
    with pytest.raises(ValueError):
        PROBE.wire_fields(raw)


def test_cursor_workspace_binding_then_external_bubble_lookup(expected):
    metadata = json.loads((FIXTURES / "cursor/storage-v1/workspace.json").read_text(encoding="utf-8"))
    assert metadata["folder"] == "file:///C:/fixture/project"
    with database("cursor/storage-v1/global.sql") as connection:
        cid, wid = connection.execute("SELECT composerId,workspaceId FROM composerHeaders").fetchone()
        assert wid == "synthetic-workspace"
        composer = json.loads(connection.execute("SELECT value FROM cursorDiskKV WHERE key=?", ("composerData:" + cid,)).fetchone()[0])
        bubbles = []
        for header in composer["fullConversationHeadersOnly"]:
            key = "bubbleId:" + cid + ":" + header["bubbleId"]
            bubbles.append(json.loads(connection.execute("SELECT value FROM cursorDiskKV WHERE key=?", (key,)).fetchone()[0]))
        assert [b["type"] for b in bubbles] == [1, 2, 2]
        assert [b["text"] for b in bubbles[:2]] == [expected["user_text"], expected["assistant_text"]]
        assert bubbles[2]["toolFormerData"]["result"] == expected["tool_output"]
        assert "createdAt" not in bubbles[1]  # session time is not a message timestamp.


def test_cursor_legacy_inline_binding_and_null_tombstone(expected):
    with database("cursor/storage-v1/workspace.sql") as workspace:
        state = json.loads(workspace.execute("SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()[0])
    cid = state["allComposers"][0]["composerId"]
    with database("cursor/storage-v1/global.sql") as connection:
        data = json.loads(connection.execute("SELECT value FROM cursorDiskKV WHERE key=?", ("composerData:" + cid,)).fetchone()[0])
        assert [v["text"] for v in data["conversation"]] == [expected["user_text"], expected["assistant_text"]]
        assert connection.execute("SELECT value FROM cursorDiskKV WHERE key='bubbleId:synthetic-deleted:synthetic-null'").fetchone() == (None,)
        assert connection.execute("SELECT value FROM cursorDiskKV WHERE key='bubbleId:synthetic-missing:synthetic-missing'").fetchone() is None


def test_fixture_source_is_fully_synthetic():
    # Scan SQL literals AND decoded binary payloads so privacy checks cover BLOBs.
    raw = b"\n".join(p.read_bytes() for p in FIXTURES.rglob("*") if p.is_file())
    def check_paths(data):
        for path in re.findall(rb"(?:file:///|[A-Za-z]:[/\\])[A-Za-z0-9_./\\:-]+", data):
            assert path.startswith((b"C:/fixture/", b"file:///C:/fixture/")), path
    check_paths(raw)
    with database("agy/trajectory-v0/conversation.sql") as connection:
        for (payload,) in connection.execute("SELECT step_payload FROM steps"):
            check_paths(payload)


def test_saved_observations_cover_all_native_sources():
    result = json.loads((ROOT / "docs/research/session-recall/t0-observations.json").read_text(encoding="utf-8"))
    assert result["contains_conversation_text"] is False
    sources = result["sources"]
    for agent in ("claude_code", "codex"):
        assert sources[agent]["counts"]["sample_files"] >= 1
        assert sources[agent]["counts"]["records"] > 0
    for variant in ("agy_legacy", "agy_ide", "agy_cli"):
        assert sources[variant]["database_counts"]["historical_db_samples"] >= 1
        assert sources[variant]["database_counts"]["with_workspace_uri"] >= 1
    assert sources["cursor"]["counts"]["bubble_json_rows"] > 0
    assert sources["cursor"]["counts"]["header_workspace_dir_match"] > 0
