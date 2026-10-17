from pathlib import Path
import json

from . import wire
from .base import is_recall, jsonl, readonly, timestamp
from ..models import Artifact, Message, Session

TOOL_TYPES = frozenset(("GENERIC", "RUN_COMMAND", "VIEW_FILE", "LIST_DIRECTORY", "MCP_TOOL", "GREP_SEARCH",
                        "CODE_ACTION", "READ_URL_CONTENT", "ASK_QUESTION", "ERROR_MESSAGE"))
IGNORED = frozenset(("SYSTEM_MESSAGE", "CHECKPOINT", "CONVERSATION_HISTORY", "KNOWLEDGE_ARTIFACTS"))


def discover(source):
    for root in source.roots:
        root = Path(root)
        directory = root / "conversations"
        if not directory.is_dir():
            directory = root if root.is_dir() else root.parent
        candidates = [root] if root.is_file() else sorted(directory.glob("*.db"))
        for path in candidates:
            app = directory.parent if directory.name == "conversations" else root
            transcript = app / "brain" / path.stem / ".system_generated/logs/transcript.jsonl"
            yield Artifact(str(path.resolve()), [path, Path(str(path) + "-wal"), transcript], dict(db=path, transcript=transcript))


def read(artifact, belongs=None):
    messages, diagnostics, projects = [], [], []
    with readonly(artifact.data["db"]) as connection:
        identity = connection.execute("SELECT cascade_id FROM trajectory_meta LIMIT 1").fetchone()
        native = str(identity[0]) if identity and identity[0] else artifact.data["db"].stem
        metadata = connection.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
        if metadata:
            projects.extend(wire.strings(metadata[0], 7))
            for workspace in wire.blobs(metadata[0], 1):
                projects.extend(wire.strings(workspace, 1))
        projects = list(dict.fromkeys(projects))
        if belongs and not belongs(projects):
            return Session(native, projects, [])
        transcript = {}
        if artifact.data["transcript"].exists():
            rows, bad = jsonl(artifact.data["transcript"])
            diagnostics.extend(bad)
            transcript = {event["step_index"]: event for _, event in rows if type(event.get("step_index")) is int}
        for row in connection.execute("SELECT idx,step_type,step_format,step_payload,metadata FROM steps ORDER BY idx"):
            index, step_type, fmt, payload, md = row
            event = transcript.get(index, {})
            locator = dict(table="steps", index=index, field="step_payload")
            try:
                if fmt != 0 or wire.integer(payload, 1) != step_type or wire.blobs(payload, 5) != [md]:
                    raise ValueError("unsupported_step_format")
                created = wire.stamp(md) or timestamp(event.get("created_at"))
                native_call = wire.blobs(md, 4)
                cid = wire.string(native_call[0], 1) if native_call else None
                name = wire.string(native_call[0], 2) if native_call else ""
                if step_type in (14, 15):
                    field = 19 if step_type == 14 else 20
                    bodies = wire.blobs(payload, field)
                    if len(bodies) != 1:
                        raise ValueError("missing_step_body")
                    body = bodies[0]
                    text = (wire.string(body, 2) or wire.string(body, 1)) if step_type == 14 else (wire.string(body, 8) or wire.string(body, 1))
                    if step_type == 14 and wire.blobs(body, 3):
                        diagnostics.append(dict(code="input_items_not_decoded", index=index))
                    if text:
                        messages.append(Message(str(index) + ":text", "user" if step_type == 14 else "assistant", text, created,
                                                locator=dict(locator, field="step_payload." + str(field))))
                    if step_type == 15:
                        calls = wire.blobs(body, 7)
                        supplemental = event.get("tool_calls", [])
                        if not isinstance(supplemental, list):
                            supplemental = []
                        for number, call in enumerate(calls):
                            call_id = wire.string(call, 1) or None
                            call_name = wire.string(call, 2)
                            extra = supplemental[number] if number < len(supplemental) and isinstance(supplemental[number], dict) else {}
                            arguments = ""
                            if extra.get("name") == call_name:
                                arguments = json.dumps(extra.get("args", {}), ensure_ascii=False)
                            else:
                                diagnostics.append(dict(code="tool_arguments_unavailable", index=index))
                            messages.append(Message(str(index) + ":call:" + str(number), "tool", call_name + "\n" + arguments,
                                                    created, "retrieved_context" if is_recall(call_name) else "tool_call", call_id, dict(locator, call=number), bool(event.get("truncated_fields"))))
                elif step_type == 21:
                    bodies = wire.blobs(payload, 28)
                    if len(bodies) != 1:
                        raise ValueError("missing_command")
                    command = bodies[0]
                    outputs = wire.blobs(command, 21) or wire.blobs(command, 26)
                    output, truncated = "", False
                    if outputs:
                        output = wire.string(outputs[0], 1)
                        if not output:
                            output = wire.string(outputs[0], 2)
                            truncated = bool(output)
                    else:
                        output = wire.string(command, 4) + wire.string(command, 5)
                    code = wire.integer(command, 6)
                    if code is not None and code >= 1 << 63:
                        code -= 1 << 64
                    text = wire.string(command, 23) or wire.string(command, 1)
                    text += "\nexit_code=" + str(code) + "\n" + output
                    messages.append(Message(str(index) + ":result", "tool", text, created, "retrieved_context" if is_recall(name) else "tool_result", cid, dict(locator, exit_code=code), truncated))
                elif event.get("type") in TOOL_TYPES:
                    text = event.get("content", "")
                    if not isinstance(text, str):
                        raise ValueError("unknown_tool_content")
                    truncated = bool(event.get("truncated_fields"))
                    messages.append(Message(str(index) + ":result", "tool", text, created,
                                            "retrieved_context" if is_recall(name) else "tool_result", cid, locator, truncated))
                    diagnostics.append(dict(code="transcript_tool_fallback", index=index, truncated=truncated))
                elif event.get("type") not in IGNORED:
                    diagnostics.append(dict(code="unknown_step", index=index, step_type=step_type))
            except (ValueError, OverflowError, OSError):
                diagnostics.append(dict(code="unsupported_step_format", index=index))
    return Session(native, projects, messages, diagnostics=diagnostics, completeness="partial" if diagnostics else "complete",
                   metadata=dict(adapter="agy_trajectory_v0", step_format=0))
