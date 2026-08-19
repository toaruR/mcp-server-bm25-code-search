import json
import pytest
from pathlib import Path
import sys

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bm25_search.mcp_server import (
    MCPServer,
    process_request,
    init_db,
    insert_chunk,
    TOOLS,
)


def test_mcp_discover_and_tools_list():
    server = MCPServer()

    # Test server/discover
    disc_resp = server.discover(request_id=1)
    assert disc_resp is not None
    assert disc_resp["id"] == 1
    res = disc_resp["result"]
    assert res["resultType"] == "complete"
    assert "2026-07-28" in res["protocolVersions"]
    assert res["serverInfo"]["name"] == "bm25-code-search"

    # Test tools/list
    tools_resp = server.list_tools(request_id=2)
    assert tools_resp is not None
    assert tools_resp["id"] == 2
    tools = tools_resp["result"]["tools"]
    assert len(tools) >= 1
    # Verify tools are sorted deterministically
    names = [t["name"] for t in tools]
    assert names == sorted(names)


def test_mcp_search_rpc_execution(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    insert_chunk(conn, "src/mcp_server.py", 1, 20, "def handle_discover(): pass")
    conn.close()

    server = MCPServer()
    resp = server.call_tool(
        name="search",
        arguments={"query": "handle_discover", "db_path": str(db_path)},
        request_id=3,
    )
    assert resp is not None
    assert resp["id"] == 3
    result = resp["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is False
    assert len(result["content"]) >= 1
    assert "src/mcp_server.py" in result["content"][0]["text"]


def test_mcp_output_result_type():
    server = MCPServer()
    resp = server.discover(request_id=100)
    assert resp["result"]["resultType"] == "complete"


def test_mcp_search_with_queries_fanout(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    insert_chunk(conn, "src/auth.py", 1, 10,
                 "def authenticate_user(): validate_session_token()")
    insert_chunk(conn, "src/other_auth.py", 1, 10,
                 "def authenticate_user(): pass")
    conn.close()

    server = MCPServer()
    resp = server.call_tool(
        name="search",
        arguments={
            "query": "authenticate_user",
            "queries": ["session_token"],
            "db_path": str(db_path),
        },
        request_id=4,
    )
    result = resp["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["queries_used"] == ["authenticate_user", "session_token"]
    assert structured["results"][0]["filepath"] == "src/auth.py"
    assert structured["results"][0]["matched_queries"] == [
        "authenticate_user", "session_token",
    ]
    assert "confidence" in structured


def test_mcp_search_response_includes_confidence(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    insert_chunk(conn, "src/mcp_server.py", 1, 20, "def handle_discover(): pass")
    conn.close()

    server = MCPServer()
    resp = server.call_tool(
        name="search",
        arguments={"query": "handle_discover", "db_path": str(db_path)},
        request_id=5,
    )
    structured = resp["result"]["structuredContent"]
    assert "confidence" in structured
    assert "queries_used" not in structured


def test_mcp_search_invalid_queries_type(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    insert_chunk(conn, "src/mcp_server.py", 1, 20, "def handle_discover(): pass")
    conn.close()

    server = MCPServer()
    resp = server.call_tool(
        name="search",
        arguments={"query": "handle_discover", "queries": "not_a_list", "db_path": str(db_path)},
        request_id=6,
    )
    assert "error" in resp
    assert resp["error"]["code"] == -32602


def test_mcp_search_includes_low_confidence_hint_when_sparse(tmp_path):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    insert_chunk(conn, "src/mcp_server.py", 1, 20, "def handle_discover(): pass")
    conn.close()

    server = MCPServer()
    resp = server.call_tool(
        name="search",
        arguments={"query": "handle_discover", "top_k": 10, "db_path": str(db_path)},
        request_id=7,
    )
    structured = resp["result"]["structuredContent"]
    assert "low_confidence_hint" in structured
    assert structured["low_confidence_hint"] is not None


def test_mcp_initialize():
    server = MCPServer()
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }
    resp = server.handle_request(init_req)
    assert resp is not None
    assert resp["id"] == 1
    assert "result" in resp
    res = resp["result"]
    assert res["protocolVersion"] == "2024-11-05"
    assert res["serverInfo"]["name"] == "bm25-code-search"
    assert "capabilities" in res

