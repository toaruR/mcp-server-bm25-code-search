"""Tests for automatic index sync, default DB paths, and packaging entry points."""

import os
import shutil
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

