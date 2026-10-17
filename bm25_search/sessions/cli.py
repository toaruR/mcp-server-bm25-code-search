from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .config import Config
from .db import Database
from .models import AGENTS, SessionError
from .service import Service
from .sync import apply_policy, reconcile_chunks


def parser():
    command = argparse.ArgumentParser(description="Project-local session recall; no additional LLM required.")
    command.add_argument("--root", default=".")
    command.add_argument("--client-agent", choices=AGENTS)
    commands = command.add_subparsers(dest="command", required=True)
    source = commands.add_parser("source").add_subparsers(dest="action", required=True)
    add = source.add_parser("add")
    add.add_argument("source_id")
    add.add_argument("--agent", choices=AGENTS, required=True)
    add.add_argument("--path", action="append", required=True)
    add.add_argument("--variant", default="")
    source.add_parser("list")
    for action in ("disable", "enable"):
        source.add_parser(action).add_argument("source_id")
    alias = commands.add_parser("project").add_subparsers(dest="action", required=True)
    alias.add_parser("alias").add_argument("path")
    alias.add_parser("relocate")
    sync = commands.add_parser("sync")
    sync.add_argument("--agent", choices=("all", "self", *AGENTS), default="all")
    sync.add_argument("--source-id")
    sync.add_argument("--verify", action="store_true")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--agent", choices=("all", "self", *AGENTS), default="all")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--since")
    search.add_argument("--until")
    search.add_argument("--max-bytes", type=int, default=16384)
    search.add_argument("--include-current", action="store_true")
    read = commands.add_parser("read")
    read.add_argument("session_id")
    read.add_argument("--anchor")
    read.add_argument("--cursor")
    read.add_argument("--before", type=int, default=3)
    read.add_argument("--after", type=int, default=3)
    read.add_argument("--max-bytes", type=int, default=16384)
    commands.add_parser("status").add_argument("--json", action="store_true")
    forget = commands.add_parser("forget")
    forget.add_argument("--session", required=True)
    forget.add_argument("--apply", action="store_true")
    restore = commands.add_parser("allow-reimport")
    restore.add_argument("--session", required=True)
    commands.add_parser("rebuild-index")
    commands.add_parser("backup").add_argument("destination")
    imp = commands.add_parser("import")
    imp.add_argument("path")
    imp.add_argument("--format", choices=AGENTS, required=True)
    imp.add_argument("--source-id", required=True)
    knowledge = commands.add_parser("knowledge").add_subparsers(dest="action", required=True)
    knowledge_search = knowledge.add_parser("search")
    knowledge_search.add_argument("query")
    knowledge_search.add_argument("--top-k", type=int, default=5)
    knowledge_search.add_argument("--max-bytes", type=int, default=16384)
    knowledge_read = knowledge.add_parser("read")
    knowledge_read.add_argument("knowledge_id")
    knowledge_read.add_argument("--max-bytes", type=int, default=16384)
    knowledge.add_parser("record").add_argument("--file", required=True)
    return command


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        config = Config(args.root)
        if args.command == "project" and args.action == "relocate":
            with Database(config.root, relocate=True) as db:
                if db.previous_root not in config.data.setdefault("project_aliases", []):
                    config.data["project_aliases"].append(db.previous_root)
                    config.save()
                result = dict(status="ok", project_id=db.project_uid)
        elif args.command == "source":
            if args.action == "add":
                config.add_source(args.source_id, args.agent, args.path, args.variant)
            elif args.action in ("disable", "enable"):
                source = next((s for s in config.data.get("sources", []) if s["source_id"] == args.source_id), None)
                if source is None:
                    raise SessionError("source_unavailable")
                source["enabled"] = args.action == "enable"
                config.save()
            result = dict(status="ok", sources=config.data.get("sources", []))
        elif args.command == "project":
            config.data.setdefault("project_aliases", []).append(args.path)
            config.save()
            result = dict(status="ok")
        else:
            service = Service(args.root, args.client_agent, knowledge=args.command == "knowledge")
            if args.command == "knowledge":
                if args.action == "record":
                    arguments = json.loads(Path(args.file).read_text(encoding="utf-8"))
                elif args.action == "search":
                    arguments = dict(query=args.query, top_k=args.top_k, max_bytes=args.max_bytes)
                else:
                    arguments = dict(knowledge_id=args.knowledge_id, max_bytes=args.max_bytes)
                result = service.call("knowledge_" + args.action, arguments)
            elif args.command == "sync":
                result = service.sync(args.agent, args.source_id, args.verify)
            elif args.command in ("search", "read"):
                fields = ("query", "agent", "top_k", "since", "until", "max_bytes", "include_current") if args.command == "search" else ("session_id", "anchor", "cursor", "before", "after", "max_bytes")
                result = service.call("session_" + args.command, {field: getattr(args, field) for field in fields})
            elif args.command == "status":
                result = service.status()
            elif args.command == "import":
                source = next((s for s in config.sources if s.source_id == args.source_id), None)
                if not source or source.agent != args.format or str(Path(args.path).resolve()) not in source.roots:
                    raise SessionError("not_found_or_not_allowed")
                result = service.sync(source_id=source.source_id, verify=True)
            else:
                with Database(config.root) as db:
                    if args.command == "forget":
                        result = dict(status="ok", **db.forget(args.session, args.apply))
                    elif args.command == "allow-reimport":
                        with db.transaction():
                            db.connection.execute("DELETE FROM tombstones WHERE kind='session' AND uid=?", (args.session,))
                            db.connection.execute("DELETE FROM artifacts WHERE session_uid IS NULL")
                        result = dict(status="ok")
                    elif args.command == "backup":
                        db.backup(args.destination)
                        result = dict(status="ok")
                    elif args.command == "rebuild-index":
                        with db.transaction():
                            db.connection.execute("DELETE FROM chunks")
                            for session in db.connection.execute("SELECT session_uid,title FROM sessions").fetchall():
                                rows = [dict(row, uid=row["message_uid"], locator=json.loads(row["locator"])) for row in db.connection.execute("SELECT * FROM messages WHERE session_uid=? ORDER BY ordinal", (session[0],))]
                                reconcile_chunks(db, session[0], rows, session[1], config)
                        result = dict(status="ok")
        print(json.dumps(result, ensure_ascii=False))
        return {"succeeded": 0, "partial": 2, "failed": 1, "busy": 3, "interrupted": 3, "already_running": 3}.get(result.get("status"), 0)
    except (SessionError, OSError, sqlite3.Error, ValueError, TypeError) as error:
        code = error.code if isinstance(error, SessionError) else ("invalid_arguments" if isinstance(error, (ValueError, TypeError)) else "storage_error")
        print(json.dumps(dict(status="error", code=code)), file=sys.stderr)
        return 3 if code == "busy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
