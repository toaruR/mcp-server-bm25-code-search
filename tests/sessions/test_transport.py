import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from bm25_search.sessions.config import Config
from bm25_search.sessions.identity import project_root
from test_runtime import setup_project


@pytest.mark.parametrize("client", ["claude_code", "codex", "agy", "cursor"])
def test_interactive_stdio_sync_search_read_for_each_client_configuration(tmp_path, client):
    root, _ = setup_project(tmp_path, ("codex",))
    repository = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(repository / "bm25_search/mcp_server.py"), "--stdio", "--root", str(root), "--sessions", "--client-agent", client]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=root)
    replies = queue.Queue()
    def reader():
        for line in process.stdout:
            replies.put(json.loads(line))
    threading.Thread(target=reader, daemon=True).start()
    request_id = 0
    def request(method, params=None):
        nonlocal request_id
        request_id += 1
        process.stdin.write(json.dumps(dict(jsonrpc="2.0", id=request_id, method=method, params=params or {})) + "\n")
        process.stdin.flush()
        response = replies.get(timeout=15)
        assert response["id"] == request_id
        assert "error" not in response, response
        return response["result"]
    def call(name, args):
        result = request("tools/call", dict(name=name, arguments=args))
        assert result["isError"] is False, result
        return result["structuredContent"]
    try:
        request("initialize", dict(protocolVersion="2024-11-05", capabilities={}, clientInfo=dict(name="synthetic-protocol-client", version="1")))
        assert len(request("tools/list")["tools"]) == 4
        started = call("session_sync", {})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = call("session_sync", dict(action="status", run_id=started["run_id"]))
            if state["status"] not in ("queued", "running"):
                break
            time.sleep(.01)
        assert state["status"] == "succeeded"
        found = call("session_search", dict(query="generation"))["results"][0]
        assert call("session_read", dict(session_id=found["session_id"], anchor=found["anchor"]))["messages"]
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_actual_linked_worktree_default_root_resolution(tmp_path, monkeypatch):
    main, worktree, hooks = tmp_path / "main", tmp_path / "linked", tmp_path / "hooks"
    main.mkdir()
    hooks.mkdir()
    def git(*arguments):
        return subprocess.run(["git", "-c", "core.hooksPath=" + str(hooks), "-C", str(main), *arguments], capture_output=True, text=True, check=True)
    git("init", "-q")
    git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "synthetic")
    git("worktree", "add", "--detach", str(worktree), "HEAD")
    child = worktree / "nested"
    child.mkdir()
    monkeypatch.chdir(child)
    assert Config().root == worktree.resolve()
    assert project_root(main) == main.resolve()
    assert (worktree / ".git").is_file()
    assert Config(worktree).root != Config(main).root


def test_background_sync_finishes_while_client_is_silent(tmp_path):
    # Windows: a git child inheriting the blocked stdin pipe stalled sync until
    # the next request; real clients stay silent between calls.
    root, inputs = setup_project(tmp_path, ("codex",))
    session = inputs / "codex" / "session.jsonl"
    other = tmp_path / "other-project"
    other.mkdir()
    for index in range(5):
        text = session.read_text(encoding="utf-8").replace(root.as_posix(), (other / str(index)).as_posix())
        (other / str(index)).mkdir()
        (inputs / "codex" / f"other-{index}.jsonl").write_text(text, encoding="utf-8")
    repository = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(repository / "bm25_search/mcp_server.py"), "--stdio", "--root", str(root), "--sessions"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=root)
    try:
        process.stdin.write(json.dumps(dict(jsonrpc="2.0", id=1, method="tools/call", params=dict(name="session_sync", arguments={}))) + "\n")
        process.stdin.flush()
        run_id = json.loads(process.stdout.readline())["result"]["structuredContent"]["run_id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with sqlite3.connect(str(root / ".bm25_sessions.db")) as connection:
                state = connection.execute("SELECT state FROM sync_runs WHERE run_id=?", (run_id,)).fetchone()[0]
            if state != "running":
                break
            time.sleep(.2)
        assert state == "succeeded"
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
