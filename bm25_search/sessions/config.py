from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .identity import project_root
from .models import AGENTS, SessionError, Source

CONFIG_NAME = ".bm25_sessions.json"
DB_NAME = ".bm25_sessions.db"
DEFAULTS = {"config_version": 1, "sources": [], "project_aliases": [], "exclude_paths": [],
            "tool_output": {"store_limit_chars": 16000, "index_limit_chars": 2000},
            "redaction": {"rules": []}, "max_artifact_bytes": 64 * 1024 * 1024}


def default_sources():
    """Fixed per-product storage locations that exist on this machine.

    Paths are never taken from tool arguments; sync still keeps only records
    whose workspace matches the project root.
    """
    home = Path.home()
    if os.name == "nt" and os.environ.get("APPDATA"):
        cursor = Path(os.environ["APPDATA"]) / "Cursor" / "User"
    elif sys.platform == "darwin":
        cursor = home / "Library" / "Application Support" / "Cursor" / "User"
    else:
        cursor = home / ".config" / "Cursor" / "User"
    claude = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else home / ".claude"
    codex = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else home / ".codex"
    candidates = [("claude-local", "claude_code", claude / "projects", ""),
                  ("codex-local", "codex", codex / "sessions", ""),
                  ("agy-cli", "agy", home / ".gemini" / "antigravity-cli", "cli"),
                  ("agy-ide", "agy", home / ".gemini" / "antigravity-ide", "ide"),
                  ("agy-legacy", "agy", home / ".gemini" / "antigravity", "legacy"),
                  ("cursor-local", "cursor", cursor, "")]
    return [Source(source_id, agent, [str(path.expanduser().resolve())], True, variant)
            for source_id, agent, path, variant in candidates if path.is_dir()]


class Config:
    def __init__(self, root=".", auto_sources=False):
        # auto_sources: use default_sources() while no source is registered.
        self.auto_sources = auto_sources
        self.root = project_root(root) if str(root) == "." else Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise SessionError("project_required")
        self.path = self.root / CONFIG_NAME
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else json.loads(json.dumps(DEFAULTS))
        except (OSError, ValueError):
            raise SessionError("invalid_config") from None
        if not isinstance(self.data, dict) or type(self.data.get("config_version")) is not int or self.data.get("config_version") != 1:
            raise SessionError("invalid_config")
        self.validate()

    def validate(self):
        if not isinstance(self.data.get("redaction", {}), dict) or not isinstance(self.data.get("tool_output", {}), dict):
            raise SessionError("invalid_config")
        seen = set()
        sources = self.data.get("sources", [])
        if not isinstance(sources, list):
            raise SessionError("invalid_config")
        for source in sources:
            if (not isinstance(source, dict) or source.get("agent") not in AGENTS
                    or not isinstance(source.get("source_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", source["source_id"])
                    or source["source_id"] in seen
                    or not isinstance(source.get("roots"), list) or not source["roots"]
                    or not all(isinstance(root, str) and root for root in source["roots"])
                    or not isinstance(source.get("enabled", True), bool)
                    or not isinstance(source.get("variant", ""), str)):
                raise SessionError("invalid_config")
            expected_adapter = {"claude_code": "claude_code_jsonl_v2", "codex": "codex_rollout_v1", "agy": "agy_trajectory_v0", "cursor": "cursor_storage_v1"}[source["agent"]]
            if source.get("adapter", expected_adapter) != expected_adapter:
                raise SessionError("unsupported_format")
            seen.add(source["source_id"])
        if self.auto_sources and not sources:
            seen.update(s.source_id for s in default_sources())
        for key in ("project_aliases", "exclude_paths"):
            if not isinstance(self.data.get(key, []), list) or not all(isinstance(v, str) for v in self.data.get(key, [])):
                raise SessionError("invalid_config")
        allowed = self.data.get("allowed_source_ids")
        if allowed is not None and (not isinstance(allowed, list) or not all(isinstance(v, str) and v in seen for v in allowed)):
            raise SessionError("invalid_config")
        rules = self.data.get("redaction", {}).get("rules", [])
        if not isinstance(rules, list):
            raise SessionError("invalid_config")
        try:
            for rule in rules:
                re.compile(rule["pattern"])
                if not isinstance(rule.get("replacement", "[REDACTED]"), str):
                    raise ValueError()
        except (KeyError, TypeError, ValueError, re.error):
            raise SessionError("invalid_config") from None
        for key, default in (("store_limit_chars", 16000), ("index_limit_chars", 2000)):
            value = self.data.get("tool_output", {}).get(key, default)
            if type(value) is not int or not 256 <= value <= 1024 * 1024:
                raise SessionError("invalid_config")
        maximum = self.data.get("max_artifact_bytes", DEFAULTS["max_artifact_bytes"])
        if type(maximum) is not int or not 1024 <= maximum <= 1024 * 1024 * 1024:
            raise SessionError("invalid_config")

    def save(self):
        self.validate()
        fd, name = tempfile.mkstemp(prefix=".bm25_sessions-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @property
    def sources(self):
        if self.auto_sources and not self.data.get("sources"):
            return default_sources()
        return [Source(s["source_id"], s["agent"], [str(Path(p).expanduser().resolve()) for p in s["roots"]],
                       s.get("enabled", True), s.get("variant", "")) for s in self.data.get("sources", [])]

    def add_source(self, source_id, agent, roots, variant=""):
        if any(s.source_id == source_id for s in self.sources):
            raise SessionError("source_exists")
        self.data.setdefault("sources", []).append(dict(source_id=source_id, agent=agent,
                                                       roots=[str(Path(p).expanduser().resolve()) for p in roots], enabled=True, variant=variant))
        self.save()

    def replace(self, text):
        for rule in self.data.get("redaction", {}).get("rules", []):
            text = re.sub(rule["pattern"], rule.get("replacement", "[REDACTED]"), text)
        return text

    def policy_hash(self):
        from .identity import digest
        return digest(self.data.get("redaction", {}), self.data.get("tool_output", {}), 1)
