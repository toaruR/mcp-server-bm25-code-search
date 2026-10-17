from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .base import is_recall, readonly, timestamp
from ..identity import path_text
from ..models import Artifact, Message, Session


def workspace_projects(path):
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("unsupported_workspace")
    if isinstance(metadata.get("folder"), str):
        return [metadata["folder"]], []
    value = metadata.get("workspace", metadata.get("configuration"))
    if not isinstance(value, str):
        return [], []
    definition = Path(path_text(value))
    if not definition.is_file():
        return [], [definition]
    data = json.loads(definition.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("unsupported_workspace")
    projects = []
    for folder in data.get("folders", []):
        if not isinstance(folder, dict):
            continue
        if isinstance(folder.get("uri"), str):
            projects.append(folder["uri"])
        elif isinstance(folder.get("path"), str):
            projects.append(str((definition.parent / folder["path"]).resolve()))
    return projects, [definition]


def discover(source):
    for root in source.roots:
        root = Path(root)
        global_db = root / "globalStorage/state.vscdb"
        if not global_db.exists():
            continue
        with readonly(global_db) as connection:
            header_map = {}
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "composerHeaders" in tables:
                for row in connection.execute("SELECT composerId,workspaceId FROM composerHeaders"):
                    if isinstance(row[0], str) and isinstance(row[1], str):
                        header_map.setdefault(row[1], set()).add(row[0])
            composer_ids = {r[0].split(":", 1)[1] for r in connection.execute("SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%' AND value IS NOT NULL")}
        artifacts = {}
        for directory in sorted((root / "workspaceStorage").glob("*")):
            meta, workspace_db = directory / "workspace.json", directory / "state.vscdb"
            if not meta.is_file():
                continue
            try:
                projects, extra_paths = workspace_projects(meta)
                ids = set(header_map.get(directory.name, set()))
                if workspace_db.is_file():
                    with readonly(workspace_db) as connection:
                        row = connection.execute("SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()
                        if row and row[0]:
                            data = json.loads(row[0])
                            if not isinstance(data, dict):
                                raise ValueError("unsupported_workspace")
                            for header in data.get("allComposers", []):
                                if isinstance(header, dict) and isinstance(header.get("composerId"), str):
                                    ids.add(header["composerId"])
                for cid in sorted(ids):
                    dependencies = [global_db, Path(str(global_db) + "-wal"), workspace_db, Path(str(workspace_db) + "-wal"), meta, *extra_paths]
                    if cid in artifacts:
                        artifacts[cid].data["projects"] = list(dict.fromkeys(artifacts[cid].data["projects"] + projects))
                        artifacts[cid].paths = list(dict.fromkeys(artifacts[cid].paths + dependencies))
                        artifacts[cid].data["bindings"].append(dict(meta=meta, workspace_db=workspace_db, workspace_id=directory.name))
                    else:
                        artifacts[cid] = Artifact(str(global_db.resolve()) + "#" + cid, dependencies,
                                                  dict(db=global_db, projects=projects, composer_id=cid,
                                                       bindings=[dict(meta=meta, workspace_db=workspace_db, workspace_id=directory.name)]))
            except (ValueError, OSError, sqlite3.Error):
                yield Artifact(str(meta.resolve()), [meta], dict(invalid=True))
        yield from artifacts.values()
        for cid in sorted(composer_ids - artifacts.keys()):
            yield Artifact(str(global_db.resolve()) + "#" + cid, [global_db, Path(str(global_db) + "-wal")],
                           dict(db=global_db, projects=[], composer_id=cid, bindings=[]))


def read(artifact, belongs=None):
    if artifact.data.get("invalid"):
        return Session("", [], [], diagnostics=[dict(code="invalid_workspace_metadata")], completeness="partial")
    cid = artifact.data["composer_id"]
    projects = []
    diagnostics, messages = [], []
    with readonly(artifact.data["db"]) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        current_ids = {r[0] for r in connection.execute("SELECT workspaceId FROM composerHeaders WHERE composerId=?", (cid,))} if "composerHeaders" in tables else set()
        for binding in artifact.data["bindings"]:
            associated = binding["workspace_id"] in current_ids
            if not current_ids and binding["workspace_db"].is_file():
                with readonly(binding["workspace_db"]) as workspace:
                    row = workspace.execute("SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()
                    state = json.loads(row[0]) if row and row[0] else {}
                    if not isinstance(state, dict):
                        raise ValueError("unsupported_workspace")
                    associated = any(isinstance(head, dict) and head.get("composerId") == cid for head in state.get("allComposers", []))
            if associated:
                fresh, _ = workspace_projects(binding["meta"])
                projects.extend(fresh)
        projects = list(dict.fromkeys(projects))
        if belongs and not belongs(projects):
            return Session(cid, projects, [])
        row = connection.execute("SELECT value FROM cursorDiskKV WHERE key=?", ("composerData:" + cid,)).fetchone()
        if not row or not row[0]:
            return Session(cid, projects, [], diagnostics=[dict(code="missing_composer")], completeness="partial")
        composer = json.loads(row[0])
        if not isinstance(composer, dict):
            return Session(cid, projects, [], diagnostics=[dict(code="unsupported_composer")], completeness="partial")
        bubbles = []
        headers = composer.get("fullConversationHeadersOnly", [])
        if not isinstance(headers, list) or not isinstance(composer.get("conversation", []), list):
            return Session(cid, projects, [], diagnostics=[dict(code="unsupported_composer")], completeness="partial")
        if headers:
            for header in headers:
                if not isinstance(header, dict) or not isinstance(header.get("bubbleId"), str):
                    diagnostics.append(dict(code="invalid_bubble_ref"))
                    continue
                key = "bubbleId:" + cid + ":" + header["bubbleId"]
                row = connection.execute("SELECT value FROM cursorDiskKV WHERE key=?", (key,)).fetchone()
                if not row or not row[0]:
                    diagnostics.append(dict(code="missing_bubble"))
                else:
                    bubbles.append((key, json.loads(row[0])))
        else:
            bubbles = [("composerData:" + cid, value) for value in composer.get("conversation", [])]
        for index, (key, bubble) in enumerate(bubbles):
            if not isinstance(bubble, dict) or bubble.get("type") not in (1, 2):
                diagnostics.append(dict(code="unknown_bubble"))
                continue
            if bubble.get("isThought"):
                continue
            bid = str(bubble.get("bubbleId") or "ordinal:" + str(index))
            created = timestamp(bubble.get("createdAt"))
            locator = dict(table="cursorDiskKV", key=key, bubble_id=bid)
            text = bubble.get("text", "")
            if isinstance(text, str) and text:
                messages.append(Message(bid + ":text", "user" if bubble["type"] == 1 else "assistant", text, created, locator=locator))
            tool = bubble.get("toolFormerData")
            if isinstance(tool, dict):
                call_id, name = tool.get("toolCallId"), str(tool.get("name", ""))
                args = tool.get("rawArgs", tool.get("params", ""))
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                messages.append(Message(bid + ":call", "tool", name + "\n" + args, created, "retrieved_context" if is_recall(name) else "tool_call", call_id, locator))
                if "result" in tool:
                    result = tool["result"]
                    if not isinstance(result, str):
                        result = json.dumps(result, ensure_ascii=False)
                    messages.append(Message(bid + ":result", "tool", result, created,
                                            "retrieved_context" if is_recall(name) else "tool_result", call_id, locator))
    missing_time = sum(message.occurred_at is None for message in messages)
    if missing_time:
        diagnostics.append(dict(code="unknown_event_timestamps", count=missing_time))
    return Session(cid, projects, messages, str(composer.get("name", "")), diagnostics,
                   completeness="partial" if diagnostics else "complete", metadata=dict(adapter="cursor_storage_v1"))
