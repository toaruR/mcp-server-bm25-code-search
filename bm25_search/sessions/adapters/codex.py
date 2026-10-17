import json

from .base import discover_jsonl as discover, is_recall, jsonl, text_blocks, timestamp
from ..models import Message, Session


def read(artifact, belongs=None):
    records, diagnostics = jsonl(artifact.paths[0])
    meta = next((e.get("payload") for _, e in records if e.get("type") == "session_meta" and isinstance(e.get("payload"), dict)), {})
    native = meta.get("id") or meta.get("session_id") or ""
    if not isinstance(native, str) or len(native) > 512:
        native = ""
    projects = [meta["cwd"]] if isinstance(meta.get("cwd"), str) else []
    if belongs and not belongs(projects):
        return Session(str(native), projects, [])
    messages, calls = [], {}
    parent = next((e.get("payload", {}).get("id") for _, e in records if e.get("type") == "session_meta" and isinstance(e.get("payload"), dict) and e["payload"].get("id") != native), None)
    started = timestamp(meta.get("timestamp"))
    for line, event in records:
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            diagnostics.append(dict(code="unknown_payload", line=line))
            continue
        kind = payload.get("type")
        uid = str(payload.get("id") or "line:" + str(line))
        locator = dict(line=line, item_id=uid)
        stamp = timestamp(event.get("timestamp"))
        inherited = bool(parent and started and stamp and stamp < started)
        if kind == "message" and payload.get("role") in ("user", "assistant"):
            content = payload.get("content")
            text = text_blocks(content)
            if text:
                messages.append(Message(uid, payload["role"], text, stamp, locator=locator,
                                        inherited=inherited))
            if isinstance(content, list) and any(not isinstance(b, dict) or b.get("type") not in ("text", "input_text", "output_text", "image", "input_image") for b in content):
                diagnostics.append(dict(code="unknown_content_block", line=line))
        elif kind in ("custom_tool_call", "function_call"):
            cid = str(payload.get("call_id") or uid)
            name = str(payload.get("name", ""))
            calls[cid] = name
            arguments = payload.get("input", payload.get("arguments", ""))
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            messages.append(Message(uid, "tool", name + "\n" + arguments, stamp, "retrieved_context" if is_recall(name) else "tool_call", cid, locator, inherited=inherited))
        elif kind in ("custom_tool_call_output", "function_call_output"):
            cid = str(payload.get("call_id") or "")
            output = payload.get("output", "")
            body = text_blocks(output)
            if not isinstance(output, (str, list)):
                diagnostics.append(dict(code="unknown_tool_output", line=line))
            messages.append(Message(uid, "tool", body, stamp, "retrieved_context" if is_recall(calls.get(cid)) else "tool_result", cid or None, locator, inherited=inherited))
        elif kind not in ("message", "reasoning", "compaction"):
            diagnostics.append(dict(code="unknown_item", line=line))
    if not native or not messages:
        diagnostics.append(dict(code="unsupported_format"))
    return Session(str(native), projects, messages, diagnostics=diagnostics,
                   parent_id=meta.get("parent_thread_id") or parent, completeness="partial" if diagnostics else "complete",
                   metadata=dict(adapter="codex_rollout_v1", writer_version=str(meta.get("cli_version", ""))[:80]))
