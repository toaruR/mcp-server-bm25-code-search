import json
import pytest
from pathlib import Path
import sys

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bm25_search.hermes_adapter import (
    hermes_function_schema,
    convert_hermes_call_to_search_args,
    convert_search_args_to_hermes_call,
    build_search_command,
    invoke_search_via_cli,
    format_cli_result,
    run_hermes_tool,
    TOOL_NAME,
)


def test_hermes_function_schema_conversion():
    schema = hermes_function_schema()
    assert schema["name"] == TOOL_NAME
    assert "parameters" in schema
    assert "query" in schema["parameters"]["properties"]

    # Test converting a Hermes function call to search args
    call = {
        "name": "bm25_search",
        "arguments": {"query": "test_symbol", "top_k": 10, "format": "json"},
    }
    args = convert_hermes_call_to_search_args(call)
    assert args["query"] == "test_symbol"
    assert args["top_k"] == 10
    assert args["format"] == "json"

    # Test roundtrip / inverse conversion
    inverse = convert_search_args_to_hermes_call("test_symbol", top_k=10, fmt="json")
    assert inverse["name"] == TOOL_NAME
    assert inverse["arguments"]["query"] == "test_symbol"


def test_hermes_cli_invocation_wrapping(tmp_path):
    # Test building command
    cmd = build_search_command(query="my_query", top_k=5, fmt="markdown")
    assert "my_query" in cmd
    assert "--top-k" in cmd
    assert "5" in cmd

    # Test run_hermes_tool with missing query (must not raise exception)
    res_bad = run_hermes_tool({"name": "bm25_search", "arguments": {}})
    assert res_bad["isError"] is True
    assert "content" in res_bad

    # Test formatting result defensively
    formatted = format_cli_result("some text output", query="q", fmt="markdown")
    assert formatted["isError"] is False
    assert formatted["content"][0]["text"] == "some text output"
