"""Tests for automatic index sync, default DB paths, and packaging entry points."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest

from bm25_search import mcp_server


@pytest.fixture
def temp_project():
    """Create a temporary project directory with sample code files."""
    tmpdir = tempfile.mkdtemp()
    try:
        project_path = Path(tmpdir)
        (project_path / "src").mkdir()
        (project_path / "src" / "auth.py").write_text(
            "def authenticate_user(username, password):\n"
            "    # Validate user credentials and return session token\n"
            "    token = generate_session_token(username)\n"
            "    return token\n",
            encoding="utf-8",
        )
        (project_path / "src" / "utils.js").write_text(
            "function getUserProfile(userId) {\n"
            "  // Fetch user profile from database\n"
            "  return { id: userId, name: 'Alice' };\n"
            "}\n",
            encoding="utf-8",
        )
        yield project_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_auto_sync_and_default_db(temp_project):
    """Test that search tool automatically syncs and creates .bm25_index.db in target root."""
    mcp_server.SERVER_CONFIG["root"] = str(temp_project)
    mcp_server.SERVER_CONFIG["db_path"] = None
    mcp_server.SERVER_CONFIG["auto_sync"] = True

    # Call search tool via process_request
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search",
            "arguments": {
                "query": "authenticate_user",
                "top_k": 5,
            },
        },
    }

    response = mcp_server.process_request(request)
    assert response is not None
    assert "result" in response
    assert response["result"]["resultType"] == "complete"

    structured = response["result"]["structuredContent"]
    assert structured.get("status") == "ok"
    assert structured.get("count", 0) >= 1
    assert "auth.py" in structured["results"][0]["filepath"]

    # Verify that .bm25_index.db was automatically created in temp_project
    db_file = temp_project / ".bm25_index.db"
    assert db_file.is_file()


def test_cli_main_root_option(temp_project, monkeypatch):
    """Test CLI main parsing --root argument."""
    # Reset config
    mcp_server.SERVER_CONFIG["root"] = "."
    mcp_server.SERVER_CONFIG["db_path"] = None
    mcp_server.SERVER_CONFIG["auto_sync"] = True

    # Mock run_stdio to avoid blocking on stdin
    monkeypatch.setattr(mcp_server, "run_stdio", lambda: None)

    exit_code = mcp_server.main(["--root", str(temp_project)])
    assert exit_code == 0
    assert mcp_server.SERVER_CONFIG["root"] == os.path.abspath(str(temp_project))

    # Search call triggers auto sync on demand
    response = mcp_server.process_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "authenticate_user"}},
    })
    assert response is not None
    assert (temp_project / ".bm25_index.db").is_file()


def test_cli_main_db_option(temp_project, monkeypatch):
    """Test CLI main parsing --db argument and auto-inferring project root."""
    mcp_server.SERVER_CONFIG["root"] = "."
    mcp_server.SERVER_CONFIG["db_path"] = None
    mcp_server.SERVER_CONFIG["auto_sync"] = True

    monkeypatch.setattr(mcp_server, "run_stdio", lambda: None)

    custom_db = temp_project / "custom_index.db"
    exit_code = mcp_server.main(["--db", str(custom_db)])
    assert exit_code == 0
    assert mcp_server.SERVER_CONFIG["db_path"] == os.path.abspath(str(custom_db))
    assert mcp_server.SERVER_CONFIG["root"] == os.path.abspath(str(temp_project))

    response = mcp_server.process_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "authenticate_user"}},
    })
    assert response is not None
    assert custom_db.is_file()
    assert response["result"]["structuredContent"]["status"] == "ok"
    assert "auth.py" in response["result"]["structuredContent"]["results"][0]["filepath"]



# ---------------------------------------------------------------------------
# Branch switch integration tests (git checkout -> next search auto-syncs)
# ---------------------------------------------------------------------------

from bm25_search.indexer import (
    db_path_for,
    get_last_commit_hash,
    indexed_files,
    init_db,
    set_last_commit_hash,
)


def _git(repo, *args):
    """Run git inside *repo* with a fixed identity; return stdout."""
    proc = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def _write(repo, rel, text):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _search_files(query):
    """Call the MCP search tool and return the set of matching file paths."""
    response = mcp_server.process_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search",
                   "arguments": {"query": query, "top_k": 20}},
    })
    structured = response["result"]["structuredContent"]
    return {r["filepath"] for r in structured.get("results", [])}


def _indexed(repo):
    conn = init_db(str(db_path_for(repo)))
    try:
        return indexed_files(conn), get_last_commit_hash(conn)
    finally:
        conn.close()


@pytest.fixture
def git_project(monkeypatch):
    """A git repo with ``main`` and ``feature`` branches.

    ``feature`` relative to ``main``:
      * deletes ``src/legacy.py``
      * modifies ``src/shared.py``
      * renames ``src/old_name.py`` -> ``src/new_name.py``
      * adds ``src/feature.py``
    """
    tmpdir = tempfile.mkdtemp()
    repo = Path(tmpdir)
    try:
        _git(repo, "init", "-q")
        _git(repo, "checkout", "-q", "-b", "main")
        _write(repo, ".gitignore", ".bm25_index.db*\n")
        _write(repo, "src/legacy.py", "def legacy_handler():\n    return 'mainonlylegacy'\n")
        _write(repo, "src/shared.py", "def shared_util():\n    return 'mainversionshared'\n")
        _write(repo, "src/old_name.py", "def renamed_target():\n    return 'renamecontent'\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "main")

        _git(repo, "checkout", "-q", "-b", "feature")
        _git(repo, "rm", "-q", "src/legacy.py")
        _write(repo, "src/shared.py", "def shared_util():\n    return 'featureversionshared'\n")
        _git(repo, "mv", "src/old_name.py", "src/new_name.py")
        _write(repo, "src/feature.py", "def feature_entry():\n    return 'featureonlyaddition'\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "feature")
        _git(repo, "checkout", "-q", "main")

        monkeypatch.setitem(mcp_server.SERVER_CONFIG, "root", str(repo))
        monkeypatch.setitem(mcp_server.SERVER_CONFIG, "db_path", None)
        monkeypatch.setitem(mcp_server.SERVER_CONFIG, "auto_sync", True)
        yield repo
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _assert_main_state(repo):
    # Searches run first: the first one triggers the auto sync.
    assert _search_files("mainonlylegacy") == {"src/legacy.py"}
    assert _search_files("mainversionshared") == {"src/shared.py"}
    assert _search_files("featureversionshared") == set()
    assert _search_files("featureonlyaddition") == set()
    assert _search_files("renamecontent") == {"src/old_name.py"}
    files, head = _indexed(repo)
    assert files == {"src/legacy.py", "src/shared.py", "src/old_name.py"}
    assert head == _git(repo, "rev-parse", "HEAD")


def _assert_feature_state(repo):
    # Searches run first: the first one triggers the auto sync.
    assert _search_files("mainonlylegacy") == set()
    assert _search_files("mainversionshared") == set()
    assert _search_files("featureversionshared") == {"src/shared.py"}
    assert _search_files("featureonlyaddition") == {"src/feature.py"}
    assert _search_files("renamecontent") == {"src/new_name.py"}
    files, head = _indexed(repo)
    assert files == {"src/shared.py", "src/new_name.py", "src/feature.py"}
    assert head == _git(repo, "rev-parse", "HEAD")


def test_branch_switch_syncs_index_on_next_search(git_project):
    """Checkout to another branch is reflected (D/M/R/A) on the next search."""
    # First search on main performs the full build.
    assert _search_files("mainonlylegacy") == {"src/legacy.py"}
    _assert_main_state(git_project)

    _git(git_project, "checkout", "-q", "feature")
    _assert_feature_state(git_project)


def test_branch_switch_round_trip(git_project):
    """Switching back and forth keeps the index in step with each branch."""
    _assert_main_state(git_project)
    _git(git_project, "checkout", "-q", "feature")
    _assert_feature_state(git_project)
    _git(git_project, "checkout", "-q", "main")
    _assert_main_state(git_project)
    _git(git_project, "checkout", "-q", "feature")
    _assert_feature_state(git_project)


def test_branch_switch_with_unreachable_previous_head(git_project):
    """If the stored HEAD can no longer be diffed, the tree is still reconciled."""
    _assert_main_state(git_project)

    # Simulate a previous HEAD that no longer exists (rebased away / gc'd).
    conn = init_db(str(db_path_for(git_project)))
    try:
        set_last_commit_hash(conn, "0" * 40)
        conn.commit()
    finally:
        conn.close()

    _git(git_project, "checkout", "-q", "feature")
    _assert_feature_state(git_project)


def test_branch_switch_uses_git_diff_when_mtime_is_unchanged(git_project):
    """A file changed by checkout is re-indexed even if its mtime looks unchanged.

    The mtime/hash check alone would skip such a file; only the
    ``git diff <old HEAD> <new HEAD>`` step catches it.
    """
    _assert_main_state(git_project)
    shared = git_project / "src" / "shared.py"
    old_stat = shared.stat()

    _git(git_project, "checkout", "-q", "feature")
    os.utime(shared, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    assert _search_files("featureversionshared") == {"src/shared.py"}
    assert _search_files("mainversionshared") == set()


def test_branch_switch_carries_uncommitted_changes(git_project):
    """Uncommitted edits and untracked files survive a checkout into the index."""
    _assert_main_state(git_project)

    # An untracked file created before checkout, and an edit made after it.
    _write(git_project, "src/scratch.py", "def scratch():\n    return 'untrackedscratch'\n")
    _git(git_project, "checkout", "-q", "feature")
    _write(git_project, "src/feature.py",
           "def feature_entry():\n    return 'uncommittedfeatureedit'\n")

    assert _search_files("untrackedscratch") == {"src/scratch.py"}
    assert _search_files("uncommittedfeatureedit") == {"src/feature.py"}
    assert _search_files("featureonlyaddition") == set()
    assert _search_files("featureversionshared") == {"src/shared.py"}
