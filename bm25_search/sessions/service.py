from __future__ import annotations

import json
import sqlite3

from .config import Config
from .db import Database
from .models import AGENTS, SessionError
from . import search, sync


class Service:
    def __init__(self, root=".", client_agent=None, knowledge=False, auto_sources=False):
        if client_agent is not None and client_agent not in AGENTS:
            raise SessionError("invalid_arguments")
        self.config = Config(root, auto_sources)
        self.client_agent = client_agent
        self.knowledge = knowledge

    def agent(self, value):
        if value == "self":
            if self.client_agent is None:
                raise SessionError("client_agent_required")
            return self.client_agent
        if value not in ("all", *AGENTS):
            raise SessionError("invalid_arguments")
        return value

    def sources(self, agent="all", source_id=None):
        agent = self.agent(agent)
        allowed = search.allowed_sources(self.config)
        sources = [s for s in self.config.sources if s.enabled and (allowed is None or s.source_id in allowed)]
        if source_id is not None:
            source = next((s for s in sources if s.source_id == source_id), None)
            if source is None:
                raise SessionError("not_found_or_not_allowed")
            if agent != "all" and source.agent != agent:
                raise SessionError("invalid_arguments")
            return [source]
        return [s for s in sources if agent == "all" or s.agent == agent]

    def call(self, name, arguments):
        schemas = {
            "session_search": {"query", "queries", "agent", "top_k", "since", "until", "max_bytes", "exclude_session_ids", "include_current"},
            "session_read": {"session_id", "anchor", "before", "after", "cursor", "max_bytes"},
            "session_sync": {"action", "agent", "source_id", "run_id"},
        }
        if name.startswith("knowledge_") and self.knowledge:
            from .knowledge import call
            if name == "knowledge_record" and self.client_agent and isinstance(arguments, dict) and arguments.get("generated_by") != self.client_agent:
                raise SessionError("invalid_arguments")
            return call(self.config, name, arguments)
        if name not in schemas or not isinstance(arguments, dict) or set(arguments) - schemas[name]:
            raise SessionError("invalid_arguments")
        try:
            if name == "session_search":
                if "query" not in arguments:
                    raise SessionError("invalid_arguments")
                args = dict(arguments)
                args["agent"] = self.agent(args.get("agent", "all"))
                return search.search(self.config, client_agent=self.client_agent, **args)
            if name == "session_read":
                if "session_id" not in arguments:
                    raise SessionError("invalid_arguments")
                return search.read(self.config, **arguments)
            action = arguments.get("action", "start")
            if action == "start":
                if "run_id" in arguments:
                    raise SessionError("invalid_arguments")
                return sync.start(self.config, self.sources(arguments.get("agent", "all"), arguments.get("source_id")))
            if action == "status" and set(arguments).issubset({"action", "run_id"}) and isinstance(arguments.get("run_id"), str):
                with Database(self.config.root) as db:
                    row = db.connection.execute("SELECT source_ids FROM sync_runs WHERE run_id=?", (arguments["run_id"],)).fetchone()
                    allowed = search.allowed_sources(self.config)
                    if not row or allowed is not None and not set(json.loads(row[0])).issubset(set(allowed)):
                        raise SessionError("not_found_or_not_allowed")
                return sync.status(self.config, arguments["run_id"])
            raise SessionError("invalid_arguments")
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                raise SessionError("busy") from None
            raise SessionError("index_unavailable") from None

    def sync(self, agent="all", source_id=None, verify=False):
        try:
            return sync.start(self.config, self.sources(agent, source_id), wait=True, verify=verify)
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                raise SessionError("busy") from None
            raise SessionError("index_unavailable") from None

    def status(self):
        try:
            with Database(self.config.root) as db:
                allowed = search.allowed_sources(self.config)
                clause = "" if allowed is None else (" WHERE source_id IN (" + ",".join("?" for _ in allowed) + ")" if allowed else " WHERE 0")
                params = allowed or []
                ids = "SELECT session_uid FROM sessions" + clause
                return dict(status="ok", project_id=db.project_uid, freshness=db.freshness(allowed),
                            sessions=db.connection.execute("SELECT count(*) FROM sessions" + clause, params).fetchone()[0],
                            messages=db.connection.execute("SELECT count(*) FROM messages WHERE session_uid IN (" + ids + ")", params).fetchone()[0],
                            chunks=db.connection.execute("SELECT count(*) FROM chunks WHERE session_uid IN (" + ids + ")", params).fetchone()[0])
        except SessionError as error:
            if error.code != "index_unavailable":
                raise
            return dict(status="index_unavailable", sources=[dict(source_id=s.source_id, agent=s.agent, variant=s.variant, enabled=s.enabled) for s in self.config.sources])
