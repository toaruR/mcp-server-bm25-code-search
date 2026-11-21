"""Explicit local-source smoke test. Persist only aggregate results, not snippets."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from bm25_search.sessions.adapters import adapter_for
from bm25_search.sessions.config import Config
from bm25_search.sessions.identity import historical_root
from bm25_search.sessions.models import Source, SessionError
from bm25_search.sessions.service import Service
from bm25_search.sessions.tokenizer import tokenize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.local or args.workdir.exists():
        parser.error("--local required and workdir must be new")
    args.workdir.mkdir(parents=True)
    home = Path.home()
    cases = [("claude_code", "", home / ".claude/projects"), ("codex", "", home / ".codex/sessions"),
             ("agy", "legacy", home / ".gemini/antigravity"), ("agy", "ide", home / ".gemini/antigravity-ide"),
             ("agy", "cli", home / ".gemini/antigravity-cli"), ("cursor", "", Path(os.environ["APPDATA"]) / "Cursor/User")]
    results = []
    for agent, variant, native_root in cases:
        label = agent + ("-" + variant if variant else "")
        result = dict(agent=agent, variant=variant, status="unavailable", native_host_client_tested=False)
        try:
            selected, project = None, None
            if agent in ("claude_code", "codex"):
                candidates = sorted(native_root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns, reverse=True)[:10]
                source = Source(label, agent, [], variant=variant)
                artifacts = []
                for path in candidates:
                    source.roots = [str(path)]
                    artifacts.extend(adapter_for(source).discover(source))
            elif agent == "agy":
                candidates = [p for p in (native_root / "conversations").glob("*.db") if not Path(str(p) + "-wal").exists()]
                candidates = sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:10]
                source = Source(label, agent, [], variant=variant)
                artifacts = []
                for path in candidates:
                    source.roots = [str(path)]
                    artifacts.extend(adapter_for(source).discover(source))
            else:
                source = Source(label, agent, [str(native_root)], variant=variant)
                artifacts = adapter_for(source).discover(source)
            for artifact in artifacts:
                try:
                    parsed = adapter_for(source).read(artifact)
                    roots = {historical_root(value) for value in parsed.projects}
                    if len(roots) == 1 and parsed.messages:
                        selected, project = artifact, next(iter(roots))
                        break
                except (OSError, ValueError, SessionError):
                    continue
            if selected is None:
                result["status"] = "no_single_project_sample"
            else:
                root = args.workdir / label
                root.mkdir()
                config = Config(root)
                config.data["project_aliases"] = [project]
                config.data["max_artifact_bytes"] = 1024 * 1024 * 1024
                registered_root = native_root if agent == "cursor" else selected.paths[0]
                config.add_source(label, agent, [str(registered_root)], variant)
                service = Service(root, agent)
                imported = service.sync(verify=True)
                information = service.status()
                query = None
                from bm25_search.sessions.db import Database
                with Database(root) as db:
                    for row in db.connection.execute("SELECT body FROM messages WHERE kind!='retrieved_context' AND inherited=0"):
                        words = tokenize(row[0]).split()
                        query = next((w for w in words if 2 <= len(w) <= 32), None)
                        if query:
                            break
                found = service.call("session_search", dict(query=query, top_k=5)) if query else dict(results=[])
                count = 0
                if found["results"]:
                    first = found["results"][0]
                    read = service.call("session_read", dict(session_id=first["session_id"], anchor=first["anchor"]))
                    count = len(read["messages"])
                result.update(status="passed" if count else "failed", sync_status=imported["status"], sessions=information["sessions"],
                              messages=information["messages"], chunks=information["chunks"], search_hits=len(found["results"]), read_messages=count,
                              diagnostics=[d["code"] for source in imported.get("sources", []) for d in source.get("diagnostics", [])][:10])
        except Exception as error:
            result.update(status="failed", error_code=error.code if isinstance(error, SessionError) else type(error).__name__)
        results.append(result)
        print(label + ": " + result["status"], flush=True)
    args.output.write_text(json.dumps(dict(scope="real native data through production adapters/service, not host-client UI E2E", contains_conversation_text=False, cases=results), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
