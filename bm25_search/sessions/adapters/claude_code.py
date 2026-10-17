import json

from .base import discover_jsonl as discover, is_recall, jsonl, text_blocks, timestamp
from ..models import Message, Session


def read(artifact, belongs=None):
    records, diagnostics = jsonl(artifact.paths[0])
    candidates = list({e["cwd"] for _, e in records if e.get("type") in ("user", "assistant") and isinstance(e.get("cwd"), str)})
    if belongs and not belongs(candidates):
        return Session("", candidates, [])
    messages, projects, native, calls = [], set(), None, {}
    agent_ids, sidechain = set(), False
    parent = None
    for line, event in records:
        kind = event.get("type")
        if kind not in ("user", "assistant"):
            continue
        if native is None and isinstance(event.get("sessionId"), str):
            native = event["sessionId"]
        if event.get("sessionId") != native:
            diagnostics.append(dict(code="mixed_session_ids", line=line))
            continue
        sidechain = sidechain or event.get("isSidechain") is True
        if isinstance(event.get("agentId"), str) and event["agentId"]:
            agent_ids.add(event["agentId"])
        if isinstance(event.get("cwd"), str):
            projects.add(event["cwd"])
        content = event.get("message", {}).get("content")
        uid = event.get("uuid") or "line:" + str(line)
        stamp = timestamp(event.get("timestamp"))
        base = dict(line=line, uuid=str(uid))
        if isinstance(content, str):
            messages.append(Message(str(uid), kind, content, stamp, locator=base))
            continue
        if not isinstance(content, list):
            diagnostics.append(dict(code="unknown_content", line=line))
            continue
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                diagnostics.append(dict(code="unknown_block", line=line))
                continue
            btype = block.get("type")
            locator = dict(base, block=index)
            bid = str(uid) + ":" + str(index)
            if btype == "text" and isinstance(block.get("text"), str):
                messages.append(Message(bid, kind, block["text"], stamp, locator=locator))
            elif btype == "tool_use":
                cid = str(block.get("id", bid))
                name = str(block.get("name", ""))
                calls[cid] = name
                body = name + "\n" + json.dumps(block.get("input", {}), ensure_ascii=False)
                messages.append(Message(bid, "tool", body, stamp, "retrieved_context" if is_recall(name) else "tool_call", cid, locator))
            elif btype == "tool_result":
                cid = str(block.get("tool_use_id", ""))
                raw = block.get("content", "")
                body = text_blocks(raw)
                if not isinstance(raw, (str, list)):
                    diagnostics.append(dict(code="unknown_tool_output", line=line))
                messages.append(Message(bid, "tool", body, stamp, "retrieved_context" if is_recall(calls.get(cid)) else "tool_result", cid or None, locator))
            elif btype not in ("thinking", "redacted_thinking", "image"):
                diagnostics.append(dict(code="unknown_block", line=line, block=index))
    if not native:
        return Session("", list(projects), [], diagnostics=diagnostics + [dict(code="unsupported_format")], completeness="partial")
    if sidechain:
        if len(agent_ids) != 1:
            return Session("", list(projects), [], diagnostics=diagnostics + [dict(code="sidechain_identity_missing")], completeness="partial")
        parent, native = native, native + "::agent:" + next(iter(agent_ids))
    return Session(native, list(projects), messages, diagnostics=diagnostics, parent_id=parent,
                   completeness="partial" if diagnostics else "complete",
                   metadata=dict(adapter="claude_code_jsonl_v2", writer_versions=sorted({str(e["version"])[:80] for _, e in records if isinstance(e.get("version"), str)})[:8]))
