"""MCP server (Model Context Protocol, 2026-07-28 spec) for BM25 code search.

This module implements an **fully stateless** MCP server exposing a single
``search`` tool backed by a SQLite FTS5 BM25 index.  It is built on the Python
standard library only (``sqlite3`` + ``json`` + ``re``) so it can run anywhere.

Conformance with the 2026-07-28 specification
--------------------------------------------
* **Stateless RPC core** -- the ``initialize`` / ``initialized`` handshake and
  the ``Mcp-Session-Id`` session are gone.  Every JSON-RPC request is fully
  self-describing; the protocol version and client identity ride in ``_meta``
  and no server-side session state is kept.  Any request may land on any server
  instance behind a plain round-robin load balancer (SEP-2567 / SEP-2575).
* **``server/discover`` support** -- every 2026-07-28 server MUST implement
  this RPC.  It advertises the supported protocol versions, server
  capabilities and server identity (SEP-2575).  It is also used by clients as a
  STDIO backwards-compatibility probe.
* **``resultType: "complete"`` output** -- every successful result carries a
  required ``resultType`` field set to ``"complete"`` (``"input_required"`` is
  reserved for Multi Round-Trip interim results; this server is fully
  stateless and never needs it).  Results from earlier-protocol servers are
  likewise treated as ``complete`` (SEP-2322).
* **Deterministic tool ordering** -- tool definitions returned by
  ``tools/list`` are sorted by name so the JSON payload (and therefore the
  prompt cache key on the client side) is byte-stable across calls, which keeps
  prompt-caching efficient when the tool catalogue is embedded in the system
  prompt.
* **Stdio JSON-RPC transport** -- the server reads newline-delimited JSON-RPC
  requests from ``stdin`` and writes JSON-RPC responses to ``stdout``.  A bare
  ``Content-Type: application/json`` HTTP body, or a JSON-RPC ``batch``, or a
  notification (no ``id``) are all handled; notifications are silently
  acknowledged (no response emitted) per JSON-RPC 2.0.

The BM25 search pipeline (tokenisation, schema, query) is embedded locally so
the server is fully self-contained; when the sibling ``bm25_search.search``
module is importable it is preferred (single source of truth), otherwise the
local copy -- byte-for-byte equivalent to the verified design-spec pipeline --
is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover
    from bm25_search.indexer import sync_index, db_path_for  # type: ignore
except Exception:  # pragma: no cover
    try:
        from .indexer import sync_index, db_path_for  # type: ignore
    except Exception:  # pragma: no cover
        sync_index = None  # type: ignore
        db_path_for = None  # type: ignore


# ---------------------------------------------------------------------------
# Protocol constants (2026-07-28 spec)
# ---------------------------------------------------------------------------

#: The single protocol version this server implements.
PROTOCOL_VERSION = "2026-07-28"

#: Server identity advertised by ``server/discover``.
SERVER_INFO = {
    "name": "bm25-code-search",
    "version": "1.0.0",
}

# ---------------------------------------------------------------------------
# BM25 search pipeline (self-contained copy of the verified design-spec pipeline)
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]+")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[぀-ヿ㐀-鿿]+")

try:  # pragma: no cover - depends on integration order
    from bm25_search.search import search as _sibling_search  # type: ignore
    _HAS_SIBLING = True
except Exception:  # pragma: no cover - standalone fallback
    _sibling_search = None
    _HAS_SIBLING = False


def split_identifier(word: str) -> list[str]:
    """camelCase / snake_case -> lower-cased subword tokens."""
    parts: list[str] = []
    for piece in word.split("_"):
        if not piece:
            continue
        subs = _CAMEL_RE.findall(piece)
        parts.extend(s.lower() for s in subs if s)
    return parts


def cjk_bigrams(text: str) -> list[str]:
    """Japanese (CJK) 2-gram; a single character passes through unchanged."""
    if len(text) <= 1:
        return [text]
    return [text[i:i + 2] for i in range(len(text) - 1)]


def pre_tokenize(raw_text: str) -> str:
    """Tokenise *raw_text* into whitespace-joined tokens for FTS5."""
    tokens: list[str] = []
    for m in _WORD_RE.finditer(raw_text):
        w = m.group(0)
        if _CJK_RE.fullmatch(w):
            tokens.extend(cjk_bigrams(w))
        else:
            tokens.append(w.lower())
            if "_" in w or re.search(r"[a-z][A-Z]", w):
                tokens.extend(split_identifier(w))
    return " ".join(tokens)


def pre_tokenize_query(raw_query: str, mode: str = "OR") -> str:
    """Pre-tokenise *raw_query* and build an OR/AND-joined quoted MATCH expr."""
    tokens = pre_tokenize(raw_query).split()
    if not tokens:
        return raw_query
    quoted = [f'"{t}"' for t in tokens]
    return f" {mode} ".join(quoted)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath   TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    raw_snippet TEXT NOT NULL,
    index_text  TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    filepath,
    index_text,
    content='chunks',
    content_rowid='chunk_id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO code_fts(rowid, filepath, index_text)
    VALUES (new.chunk_id, new.filepath, new.index_text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO code_fts(code_fts, rowid, filepath, index_text)
    VALUES ('delete', old.chunk_id, old.filepath, old.index_text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO code_fts(code_fts, rowid, filepath, index_text)
    VALUES ('delete', old.chunk_id, old.filepath, old.index_text);
    INSERT INTO code_fts(rowid, filepath, index_text)
    VALUES (new.chunk_id, new.filepath, new.index_text);
END;
"""

#: Column weights passed to FTS5 bm25() -- filepath is boosted 3.0x.
BM25_WEIGHTS = (3.0, 1.0)


def init_db(conn) -> sqlite3.Connection:
    """Create the full schema on *conn* (path string or connection)."""
    if isinstance(conn, str):
        conn = sqlite3.connect(conn)
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError(
            f"init_db expects a sqlite3.Connection or a path string, "
            f"got {type(conn).__name__}"
        )
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def insert_chunk(conn: sqlite3.Connection,
                 filepath: str,
                 start_line: int,
                 end_line: int,
                 raw_snippet: str,
                 index_text: Optional[str] = None) -> int:
    """Insert one chunk and return its ``chunk_id``."""
    if index_text is None:
        index_text = pre_tokenize(filepath) + " " + pre_tokenize(raw_snippet)
    cur = conn.execute(
        "INSERT INTO chunks (filepath, start_line, end_line, raw_snippet, "
        "index_text) VALUES (?, ?, ?, ?, ?)",
        (filepath, start_line, end_line, raw_snippet, index_text),
    )
    conn.commit()
    return int(cur.lastrowid)


def search(conn: sqlite3.Connection,
           raw_query: str,
           top_k: int = 5,
           mode: str = "OR") -> list[dict]:
    """Search the index and return up to *top_k* ranked chunk dicts."""
    query = pre_tokenize_query(raw_query, mode)
    rows = conn.execute(
        """
        SELECT c.filepath, c.start_line, c.end_line, c.raw_snippet,
               bm25(code_fts, ?, ?) AS score
        FROM code_fts
        JOIN chunks c ON c.chunk_id = code_fts.rowid
        WHERE code_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (BM25_WEIGHTS[0], BM25_WEIGHTS[1], query, top_k),
    ).fetchall()
    return [
        {
            "filepath": r[0],
            "start_line": r[1],
            "end_line": r[2],
            "raw_snippet": r[3],
            "score": r[4],
        }
        for r in rows
    ]


ZERO_MATCH_MESSAGE = (
    "BM25 match zero. Recommended Fallback: Use standard grep_search or glob "
    "to locate exact symbol definitions."
)


def zero_match_fallback(query: Optional[str] = None) -> dict:
    """Return the structured zero-match fallback response."""
    resp = {
        "status": "zero_match",
        "message": ZERO_MATCH_MESSAGE,
    }
    if query is not None:
        resp["query"] = query
    return resp


def format_json(results: list[dict], query: Optional[str] = None) -> str:
    """Render *results* as a JSON document (envelope with a results list)."""
    payload = {
        "status": "ok",
        "query": query,
        "count": len(results),
        "results": list(results),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

def _tool_search_definition() -> dict:
    """The ``search`` tool definition (JSON Schema 2020-12 input schema)."""
    return {
        "name": "search",
        "title": "BM25 Code Search",
        "description": (
            "Search the indexed codebase with a SQLite FTS5 BM25 ranker. "
            "Returns the best-matching code chunks, ranked with the filepath "
            "column boosted 3.0x over the body.  Works for Japanese (CJK) and "
            "camelCase/snake_case queries.  When nothing matches, returns a "
            "structured zero-match fallback suggesting grep/glob."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search query.  Japanese, camelCase and snake_case "
                        "are all tokenised the same way the index was built."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 100,
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "How query tokens are combined.  'OR' maximises recall; "
                        "'AND' requires every token to hit."
                    ),
                    "enum": ["OR", "AND"],
                    "default": "OR",
                },
            },
            "required": ["query"],
        },
    }


#: Canonical tool catalogue.  ``TOOLS`` is the *ordered-by-name* list; the
#: deterministic sort is applied once at module import so callers can rely on a
#: stable ordering.  (See :func:`list_tools` / the ``tools/list`` handler.)
TOOLS: list[dict] = sorted(
    [_tool_search_definition()],
    key=lambda t: t["name"],
)


# ---------------------------------------------------------------------------
# Core request handlers (each returns a JSON-RPC *result* payload)
# ---------------------------------------------------------------------------

def handle_initialize(request_id: Any, params: Optional[dict]) -> dict:
    """Handle ``initialize`` -- backwards compatibility handshake for MCP 2024-11-05 clients."""
    params = params or {}
    protocol_version = params.get("protocolVersion", "2024-11-05")
    return {
        "protocolVersion": protocol_version,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": dict(SERVER_INFO),
    }


def handle_ping(request_id: Any, params: Optional[dict]) -> dict:
    """Handle ``ping`` -- standard ping handler."""
    return {}


def handle_discover(request_id: Any, params: Optional[dict]) -> dict:
    """Handle ``server/discover`` -- advertise versions, capabilities, identity.

    Every 2026-07-28 server MUST implement this RPC (SEP-2575).  It returns the
    supported protocol versions, the server capabilities (the stateless set),
    the server identity, and an optional instruction note.
    """
    return {
        "resultType": "complete",
        "protocolVersions": [PROTOCOL_VERSION],
        "capabilities": {
            # Stateless core: no sessions, no notifications stream required.
            "stateless": True,
            "tools": {"listChanged": False},
            # The optional resource/prompt surfaces are not offered.
            "resources": {},
            "prompts": {},
            "logging": {},
            "elicitation": {},
        },
        "serverInfo": dict(SERVER_INFO),
        "instructions": (
            "Stateless BM25 code-search server (2026-07-28). "
            "Call tools/list to discover tools and tools/call to invoke them. "
            "No initialize handshake is required."
        ),
    }


def handle_tools_list(request_id: Any, params: Optional[dict]) -> dict:
    """Handle ``tools/list`` -- return the deterministically sorted catalogue.

    The tool definitions are sorted by ``name`` before being returned.  This is
    what keeps the prompt-cache key byte-stable across calls: when a client
    embeds the tool catalogue in the system prompt, a fixed ordering means the
    cached prefix is reused instead of being invalidated by reordering.
    """
    # Deterministic, stable ordering by name (idempotent; defends against any
    # accidental re-ordering of the module-level ``TOOLS`` list).
    ordered = sorted(TOOLS, key=lambda t: t.get("name", ""))
    return {
        "resultType": "complete",
        "tools": ordered,
    }


SERVER_CONFIG: dict[str, Any] = {
    "root": ".",
    "db_path": None,
    "auto_sync": True,
}


def handle_tools_call(request_id: Any, params: Optional[dict]) -> dict:
    """Handle ``tools/call`` -- run the requested tool and return its result."""
    params = params or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if name != "search":
        raise McpError(code=-32602, message=f"Unknown tool: {name!r}")

    raw_query = arguments.get("query")
    if not isinstance(raw_query, str) or not raw_query:
        raise McpError(code=-32602, message="Argument 'query' is required.")
    top_k = arguments.get("top_k", 5)
    mode = arguments.get("mode", "OR")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise McpError(code=-32602, message="'top_k' must be a positive integer.")
    if mode not in ("OR", "AND"):
        raise McpError(code=-32602, message="'mode' must be 'OR' or 'AND'.")

    db_path_arg = arguments.get("db_path")
    should_sync = False
    if db_path_arg:
        db_path = str(db_path_arg)
    elif SERVER_CONFIG["db_path"]:
        db_path = str(SERVER_CONFIG["db_path"])
        should_sync = SERVER_CONFIG.get("auto_sync", True)
    else:
        root_dir = SERVER_CONFIG["root"]
        if root_dir == ".":
            # auto detect root if cwd is not a repo
            if not (Path.cwd() / ".git").exists():
                fallback = Path(__file__).resolve().parent.parent
                if (fallback / ".git").exists():
                    root_dir = str(fallback)
                    SERVER_CONFIG["root"] = root_dir
        db_path = str(Path(root_dir) / ".bm25_index.db")
        should_sync = SERVER_CONFIG.get("auto_sync", True)

    if should_sync and db_path != ":memory:" and sync_index is not None:
        try:
            root_dir = SERVER_CONFIG["root"]
            sync_index(root=root_dir, db_path=db_path)
        except Exception:
            pass

    if db_path == ":memory:":
        raise McpError(
            code=-32602,
            message="Argument 'db_path' is required for a persistent index "
                    "(an in-memory index is empty).",
        )
    try:
        conn = sqlite3.connect(db_path)
        init_db(conn)
    except sqlite3.Error as exc:
        raise McpError(code=-32603, message=f"Index open failed: {exc}") from exc


    try:
        # Prefer the sibling implementation when present, for a single source
        # of truth; the local copy is byte-for-byte equivalent otherwise.
        if _HAS_SIBLING and _sibling_search is not None:
            results = _sibling_search(conn, raw_query, top_k=top_k, mode=mode)
        else:
            results = search(conn, raw_query, top_k=top_k, mode=mode)
    finally:
        conn.close()

    if not results:
        payload = zero_match_fallback(query=raw_query)
    else:
        payload = json.loads(format_json(results, query=raw_query))

    return {
        "resultType": "complete",
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ],
        "isError": False,
        "structuredContent": payload,
    }


def dispatch(method: str, request_id: Any, params: Optional[dict]) -> Optional[dict]:
    """Dispatch a JSON-RPC method to its handler.

    Returns the JSON-RPC *result* object, or raises :class:`McpError` on a
    protocol/application error.  Returns ``None`` for notifications (which must
    not produce a response).
    """
    if method == "initialize":
        return handle_initialize(request_id, params)
    if method in ("notifications/initialized", "initialized"):
        return {}
    if method == "ping":
        return handle_ping(request_id, params)
    if method == "server/discover":
        return handle_discover(request_id, params)
    if method == "tools/list":
        return handle_tools_list(request_id, params)
    if method == "tools/call":
        return handle_tools_call(request_id, params)
    # Unknown method -> JSON-RPC method-not-found error.
    raise McpError(code=-32601, message=f"Method not found: {method!r}")


# A default in-memory index path the server uses when none is provided.
DEFAULT_DB_PATH = ":memory:"


class McpError(Exception):
    """JSON-RPC / MCP application error carrying a numeric *code*."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def build_response(request_id: Any, result: Any) -> dict:
    """Build a JSON-RPC 2.0 *success* response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def build_error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    """Build a JSON-RPC 2.0 *error* response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


# ---------------------------------------------------------------------------
# Stdio JSON-RPC transport loop
# ---------------------------------------------------------------------------

def process_message(raw: str) -> Optional[list[dict]]:
    """Process one raw stdin message (object or JSON-RPC batch).

    Returns a list of JSON-RPC response objects to write back, or ``None`` when
    nothing should be emitted (e.g. all inputs were notifications).
    """
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Parse error -- no request id available, emit a bare error response.
        return [build_error(None, PARSE_ERROR, "Parse error")]

    # JSON-RPC batch
    if isinstance(message, list):
        responses: list[dict] = []
        for item in message:
            resp = process_message_single(item)
            if resp is not None:
                responses.append(resp)
        return responses if responses else None

    return process_message_single(message)


def process_message_single(message: dict) -> Optional[dict]:
    """Process a single JSON-RPC request/notification object."""
    if not isinstance(message, dict):
        return build_error(None, INVALID_REQUEST, "Invalid Request")

    method = message.get("method")
    request_id = message.get("id", None)
    params = message.get("params")

    # Notification (no id) -> process but emit no response.
    is_notification = "id" not in message
    if is_notification:
        try:
            dispatch(method, None, params)
        except McpError:
            pass  # notifications never produce responses
        return None

    if method is None:
        return build_error(
            request_id, INVALID_REQUEST, "Invalid Request: missing 'method'"
        )

    try:
        result = dispatch(method, request_id, params)
    except McpError as exc:
        return build_error(request_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # defensive: never crash the loop
        return build_error(
            request_id, INTERNAL_ERROR, f"Internal error: {exc}"
        )

    return build_response(request_id, result)


def process_request(message: dict) -> Optional[dict]:
    """Process a single decoded JSON-RPC request/notification object.

    This is the dict-in / dict-out entry point (the decoded form of
    :func:`process_message`).  It is handy for callers that already hold a
    parsed request, and for tests that want to inspect the response object
    directly without serialising to / from JSON.

    Returns the JSON-RPC response object, or ``None`` for a notification (which
    must not produce a response).
    """
    return process_message_single(message)


class MCPServer:
    """Stateless MCP 2026-07-28 server facade.

    Thin object wrapper over the module-level :func:`process_request` handler so
    callers that prefer an instance API (and the conventional
    ``server.handle_request(request)`` shape) can use one.  The server keeps no
    per-connection state -- every request is processed independently.
    """

    protocol_version = PROTOCOL_VERSION
    server_info = dict(SERVER_INFO)

    def handle_request(self, message: dict) -> Optional[dict]:
        """Handle one decoded JSON-RPC request; return the response (or None)."""
        return process_request(message)

    def discover(self, request_id: Any = None) -> dict:
        """Convenience wrapper for ``server/discover``."""
        return self.handle_request(
            {"jsonrpc": "2.0", "id": request_id, "method": "server/discover"}
        )

    def list_tools(self, request_id: Any = None) -> dict:
        """Convenience wrapper for ``tools/list``."""
        return self.handle_request(
            {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}
        )

    def call_tool(self, name: str, arguments: dict, request_id: Any = None) -> dict:
        """Convenience wrapper for ``tools/call``."""
        return self.handle_request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )


def run_stdio() -> None:
    """Read newline-delimited JSON-RPC requests from stdin; write to stdout."""
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        responses = process_message(line)
        if responses is None:
            continue
        # ``process_message`` returns a single dict for a single request and a
        # list for a batch; normalise to a list so we always iterate responses.
        if isinstance(responses, dict):
            responses = [responses]
        for resp in responses:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-server-bm25-code-search",
        description="Stateless MCP 2026-07-28 server for BM25 code search.",
    )
    parser.add_argument(
        "--root", "-r", default=".",
        help="Root directory of the project to index and search (default: current working directory).",
    )
    parser.add_argument(
        "--db", default=None,
        help="Path to the SQLite FTS5 index (default: <root>/.bm25_index.db).",
    )
    parser.add_argument(
        "--no-auto-sync", action="store_true",
        help="Disable automatic index sync on tool invocation.",
    )
    parser.add_argument(
        "--stdio", action="store_true",
        help="Run the stdio JSON-RPC transport loop (default behaviour).",
    )
    args = parser.parse_args(argv)
    root_path = os.path.abspath(args.root)
    if args.root == "." and not (Path(root_path) / ".git").exists():
        fallback = Path(__file__).resolve().parent.parent
        if (fallback / ".git").exists():
            root_path = str(fallback)
    SERVER_CONFIG["root"] = root_path
    SERVER_CONFIG["db_path"] = args.db
    SERVER_CONFIG["auto_sync"] = not args.no_auto_sync

    run_stdio()
    return 0




if __name__ == "__main__":
    raise SystemExit(main())
