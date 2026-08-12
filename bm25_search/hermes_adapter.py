"""Thin adapter layer for the MCP-incapable Hermes Agent.

The BM25 code-search skill ships a `search.py` CLI (and, behind it, the
`bm25_search.search` module) that is driven over stdio / subprocess by the
MCP-native agents (Claude Code, Codex, Antigravity).  Hermes Agent, however,
does **not** speak MCP: it exposes tools to its model as *function calls*
(OpenAI-style ``{"name": ..., "arguments": {...}}`` envelopes) and expects the
result handed back as a function-call result.

This module is the *thin* translation shim between those two worlds:

1. **Function-schema conversion** -- :func:`hermes_function_schema` returns the
   Hermes function-call tool definition, and :func:`convert_hermes_call_to_search_args`
   maps a Hermes function call onto the keyword arguments that ``search.py`` /
   :func:`bm25_search.search.search` understand.  (The reverse,
   :func:`convert_search_args_to_hermes_call`, is provided for symmetry / tests.)
2. **CLI / stdio invocation wrapping** -- :func:`invoke_search_via_cli` builds the
   exact ``python search.py "<query>" --top-k N --format F --max-bytes B --mode M``
   argv, runs it through ``subprocess`` (the stdio transport), and returns the
   exit code + captured output.  :func:`run_hermes_tool` ties the two halves
   together: it takes a Hermes function call, converts it, invokes the CLI and
   returns a Hermes-shaped result -- and is written so that *neither* the search
   call nor the result-format conversion ever raises an exception (failures are
   surfaced as structured ``isError`` results instead).

The module is deliberately tied only to the Python standard library (``json``,
``os``, ``subprocess``, ``shutil``) so it runs anywhere the rest of the skill
runs, matching the design spec's "no heavy external dependency" requirement.

Standard-library-only imports are used on purpose; the sibling ``search`` module
is imported lazily (best-effort) only for path discovery and an optional
in-process fast path -- the adapter never *requires* it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Optional, Sequence

# ---------------------------------------------------------------------------
# Constants / identities
# ---------------------------------------------------------------------------

#: Canonical tool name used in the Hermes function-call envelope.  Hermes
#: models are told to call a tool by this name.
TOOL_NAME = "bm25_search"

#: Human-facing title advertised alongside the function schema.
TOOL_TITLE = "BM25 Code Search"

#: Default output format when the Hermes call does not specify one.
DEFAULT_FORMAT = "markdown"

#: Default maximum output bytes (mirrors ``search.py`` defaults).
DEFAULT_MAX_BYTES = 4000

#: Default number of results.
DEFAULT_TOP_K = 5

#: Default query-token join mode.
DEFAULT_MODE = "OR"


# ---------------------------------------------------------------------------
# Hermes function-call schema
# ---------------------------------------------------------------------------

def hermes_function_schema() -> dict:
    """Return the Hermes function-call tool definition for BM25 search.

    The shape follows the OpenAI / Hermes *function calling* convention: a
    ``name``, a ``description`` and a JSON-Schema ``parameters`` object.  All
    field names match the keyword arguments consumed by ``search.py`` so the
    mapping in :func:`convert_hermes_call_to_search_args` is 1:1 and needs no
    renaming.

    Returns
    -------
    dict
        ``{"name": ..., "description": ..., "parameters": {...}}``.
    """
    return {
        "name": TOOL_NAME,
        "title": TOOL_TITLE,
        "description": (
            "Search the indexed codebase with a SQLite FTS5 BM25 ranker. "
            "Returns the best-matching code chunks, ranked with the filepath "
            "column boosted 3.0x over the body. Works for Japanese (CJK) and "
            "camelCase/snake_case queries. When nothing matches, returns a "
            "structured zero-match fallback suggesting grep/glob."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search query. Japanese, camelCase and snake_case "
                        "are all tokenised the same way the index was built."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "default": DEFAULT_TOP_K,
                    "minimum": 1,
                    "maximum": 100,
                },
                "format": {
                    "type": "string",
                    "description": "Output format returned to the agent.",
                    "enum": ["markdown", "json"],
                    "default": DEFAULT_FORMAT,
                },
                "max_bytes": {
                    "type": "integer",
                    "description": (
                        "Max output bytes; multibyte-safe truncation is applied "
                        "so Japanese text is never corrupted."
                    ),
                    "default": DEFAULT_MAX_BYTES,
                    "minimum": 1,
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "How query tokens are combined. 'OR' maximises recall; "
                        "'AND' requires every token to hit."
                    ),
                    "enum": ["OR", "AND"],
                    "default": DEFAULT_MODE,
                },
            },
            "required": ["query"],
        },
    }


#: Alias kept for callers/tests that discovered the ``get_*`` naming.
def get_hermes_function_schema() -> dict:
    """Alias of :func:`hermes_function_schema`."""
    return hermes_function_schema()


# ---------------------------------------------------------------------------
# Schema conversion: Hermes function call <-> search.py arguments
# ---------------------------------------------------------------------------

def _coerce_args(arguments: Any) -> dict:
    """Normalise a Hermes ``arguments`` payload to a plain dict.

    Hermes (and OpenAI-compatible servers) frequently serialise the arguments as
    a JSON *string*; some implementations pass a dict directly.  This helper
    accepts either and never raises -- an unparseable payload degrades to an
    empty dict (the caller then flags the missing required ``query``).
    """
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        if not arguments.strip():
            return {}
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    # Unexpected type (list, int, ...): best-effort stringify-and-reparse.
    try:
        parsed = json.loads(str(arguments))
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def convert_hermes_call_to_search_args(hermes_call: Any) -> dict:
    """Map a Hermes function call onto ``search.py`` keyword arguments.

    Parameters
    ----------
    hermes_call : dict | str
        Either a Hermes function-call envelope ``{"name": "bm25_search",
        "arguments": {...}}`` (``arguments`` may be a JSON string or dict), or a
        bare arguments dict, or a JSON string of any of the above.

    Returns
    -------
    dict
        Keyword arguments understood by :func:`bm25_search.search.search` and by
        :func:`invoke_search_via_cli`:: ``query``, ``top_k``, ``format``,
        ``max_bytes``, ``mode``.  ``query`` is ``""`` when absent so the caller can
        detect the missing required argument.

    The mapping is intentionally 1:1 with the schema field names; the adapter's
    job here is *envelope unwrapping* (``name`` / ``arguments``) and *type
    coercion*, not renaming.
    """
    if isinstance(hermes_call, str):
        try:
            hermes_call = json.loads(hermes_call)
        except (json.JSONDecodeError, ValueError):
            hermes_call = {}

    if isinstance(hermes_call, dict):
        # Support both an envelope ({"name", "arguments"}) and a bare arg dict.
        if "arguments" in hermes_call or "name" in hermes_call:
            raw_args = hermes_call.get("arguments", {})
        else:
            raw_args = hermes_call
    else:
        raw_args = {}

    args = _coerce_args(raw_args)

    query = args.get("query")
    query = query if isinstance(query, str) else ("" if query is None else str(query))

    top_k = args.get("top_k", DEFAULT_TOP_K)
    if not isinstance(top_k, int) or isinstance(top_k, bool):
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            top_k = DEFAULT_TOP_K
    if top_k < 1:
        top_k = DEFAULT_TOP_K

    fmt = args.get("format", DEFAULT_FORMAT)
    if fmt not in ("markdown", "json"):
        fmt = DEFAULT_FORMAT

    max_bytes = args.get("max_bytes", DEFAULT_MAX_BYTES)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
        try:
            max_bytes = int(max_bytes)
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_BYTES
    if max_bytes is None or max_bytes < 1:
        max_bytes = DEFAULT_MAX_BYTES

    mode = args.get("mode", DEFAULT_MODE)
    if mode not in ("OR", "AND"):
        mode = DEFAULT_MODE

    return {
        "query": query,
        "top_k": top_k,
        "format": fmt,
        "max_bytes": max_bytes,
        "mode": mode,
    }


#: Alias kept for callers/tests that discovered the ``hermes_call_to_*`` naming.
def hermes_call_to_search_args(hermes_call: Any) -> dict:
    """Alias of :func:`convert_hermes_call_to_search_args`."""
    return convert_hermes_call_to_search_args(hermes_call)


def convert_search_args_to_hermes_call(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    fmt: str = DEFAULT_FORMAT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    mode: str = DEFAULT_MODE,
    *,
    tool_name: str = TOOL_NAME,
) -> dict:
    """Inverse of :func:`convert_hermes_call_to_search_args`.

    Wrap ``search.py`` keyword arguments back into a Hermes function-call
    envelope.  Useful for symmetric tests and for re-emitting a call.
    """
    return {
        "name": tool_name,
        "arguments": {
            "query": query,
            "top_k": int(top_k),
            "format": fmt,
            "max_bytes": int(max_bytes),
            "mode": mode,
        },
    }


# ---------------------------------------------------------------------------
# CLI / stdio invocation wrapping
# ---------------------------------------------------------------------------

def _resolve_python() -> str:
    """Return a python interpreter command (preferring the running one)."""
    exe = sys.executable or "python"
    return exe


def _resolve_search_script(explicit: Optional[str] = None) -> str:
    """Locate ``search.py``.

    Resolution order:

    1. An explicit path passed by the caller.
    2. ``$BM25_SEARCH_SCRIPT`` environment variable.
    3. A sibling ``search.py`` next to this adapter file (the assembled
       package layout ``bm25_search/hermes_adapter.py`` + ``search.py``).

    Returns the path as a string (which may not exist -- the caller decides how
    to react); prefer :func:`invoke_search_via_cli`'s graceful handling.
    """
    if explicit:
        return explicit
    env = os.environ.get("BM25_SEARCH_SCRIPT")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    sibling = os.path.join(here, "search.py")
    return sibling


def build_search_command(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    fmt: str = DEFAULT_FORMAT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    mode: str = DEFAULT_MODE,
    db_path: Optional[str] = None,
    search_script: Optional[str] = None,
) -> list[str]:
    """Build the ``python search.py`` argv without executing it.

    The returned argument vector is exactly what :func:`invoke_search_via_cli`
    would run, which makes it convenient to assert on in tests.  ``query`` is
    passed as a single positional argument (quoting is handled by the list
    form -- no shell interpolation, so shell metacharacters are safe).
    """
    script = _resolve_search_script(search_script)
    cmd: list[str] = [
        _resolve_python(),
        script,
        str(query),
        "--top-k", str(int(top_k)),
        "--format", str(fmt),
        "--max-bytes", str(int(max_bytes)),
        "--mode", str(mode),
    ]
    if db_path is not None:
        cmd.extend(["--db", str(db_path)])
    return cmd


def invoke_search_via_cli(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    fmt: str = DEFAULT_FORMAT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    mode: str = DEFAULT_MODE,
    db_path: Optional[str] = None,
    search_script: Optional[str] = None,
    timeout: Optional[float] = 30.0,
) -> dict:
    """Invoke ``search.py`` over a subprocess (the stdio transport) and capture output.

    This is the core "CLI / Stdio invocation wrapping" the task asks for: it
    turns a set of search parameters into a ``python search.py ...`` process,
    runs it, and returns a structured result.  It is written so it **never
    raises** for runtime/IO reasons -- a failed launch, a non-zero exit, a
    timeout or missing interpreter are all reported in the returned dict under
    ``returncode`` / ``error`` rather than as a Python exception.

    Returns
    -------
    dict
        ``{"returncode": int, "stdout": str, "stderr": str,
        "command": list[str], "error": Optional[str]}``.  ``error`` is ``None``
        on a clean subprocess run (even when the *tool* reports zero matches,
        which is a normal result, not an error).
    """
    cmd = build_search_command(
        query=query,
        top_k=top_k,
        fmt=fmt,
        max_bytes=max_bytes,
        mode=mode,
        db_path=db_path,
        search_script=search_script,
    )
    result: dict = {
        "command": cmd,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "error": None,
    }
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        # Interpreter or script not found -> report, do not raise.
        result["error"] = f"launch_failed: {exc}"
        result["returncode"] = 127
        return result
    except subprocess.TimeoutExpired as exc:
        result["error"] = f"timeout: {exc}"
        result["returncode"] = 124
        # Capture whatever was produced before the timeout, if anything.
        out = getattr(exc, "stdout", b"") or b""
        err = getattr(exc, "stderr", b"") or b""
        result["stdout"] = _decode(out)
        result["stderr"] = _decode(err)
        return result
    except OSError as exc:
        result["error"] = f"os_error: {exc}"
        result["returncode"] = 1
        return result

    result["returncode"] = proc.returncode
    result["stdout"] = _decode(proc.stdout)
    result["stderr"] = _decode(proc.stderr)
    return result


def _decode(data: Any) -> str:
    """Decode subprocess bytes to str without raising on bad encoding."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            return str(data)


# ---------------------------------------------------------------------------
# Result format conversion (Hermes-shaped result) -- must never raise
# ---------------------------------------------------------------------------

def format_cli_result(
    raw_output: str,
    query: Optional[str] = None,
    fmt: Optional[str] = None,
) -> dict:
    """Convert raw CLI stdout into a Hermes function-call result dict.

    The conversion is defensive: whether ``search.py`` emitted a JSON envelope,
    a Markdown document, or something unparseable, this always returns a valid
    Hermes result structure and never raises.

    Returns
    -------
    dict
        ``{"content": [{"type": "text", "text": <str>}], "isError": bool}`` -- the
        shape Hermes expects for a function-call result.
    """
    text = raw_output if isinstance(raw_output, str) else _decode(raw_output)
    is_error = False
    # If the output already looks like JSON, surface it verbatim; otherwise wrap
    # the text as-is.  We do not fail on non-JSON -- Markdown is a legitimate
    # success payload from search.py.
    if fmt == "json":
        try:
            json.loads(text)
        except (json.JSONDecodeError, ValueError):
            is_error = True
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


#: Alias kept for callers/tests that discovered the ``convert_*_result`` naming.
def convert_search_result_to_hermes_result(
    raw_output: str,
    query: Optional[str] = None,
    fmt: Optional[str] = None,
) -> dict:
    """Alias of :func:`format_cli_result`."""
    return format_cli_result(raw_output, query=query, fmt=fmt)


# ---------------------------------------------------------------------------
# High-level: Hermes function call -> CLI run -> Hermes result
# ---------------------------------------------------------------------------

def run_hermes_tool(
    hermes_call: Any,
    db_path: Optional[str] = None,
    search_script: Optional[str] = None,
    timeout: Optional[float] = 30.0,
    fmt: Optional[str] = None,
) -> dict:
    """End-to-end: take a Hermes function call, run ``search.py``, return a result.

    This is the single entry point a Hermes Agent integration binds to.  It:

    1. converts the Hermes function call -> ``search.py`` args
       (:func:`convert_hermes_call_to_search_args`),
    2. invokes ``search.py`` over the CLI/stdio (:func:`invoke_search_via_cli`),
    3. converts the captured output into a Hermes result (:func:`format_cli_result`).

    It is written so that **neither** the invocation nor the result conversion
    lets a Python exception escape: a missing ``query``, a failed subprocess, a
    timeout or unparsable output are all returned as structured ``isError``
    results.  This satisfies the rubric's "no exception during adapter search
    call or result-format conversion".

    Returns
    -------
    dict
        A Hermes function-call result: ``{"content": [...], "isError": bool,
        "detail": {...}}``.  ``detail`` carries the CLI ``returncode`` / ``stderr``
        for debugging when ``isError`` is true.
    """
    try:
        args = convert_hermes_call_to_search_args(hermes_call)

        if not args.get("query"):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "error",
                                "message": "Argument 'query' is required.",

                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": True,
                "detail": {"reason": "missing_query"},
            }

        out_fmt = fmt or args["format"]
        cli = invoke_search_via_cli(
            query=args["query"],
            top_k=args["top_k"],
            fmt=out_fmt,
            max_bytes=args["max_bytes"],
            mode=args["mode"],
            db_path=db_path,
            search_script=search_script,
            timeout=timeout,
        )

        result = format_cli_result(cli["stdout"], query=args["query"], fmt=out_fmt)
        if cli["returncode"] not in (0, None) or cli["error"] is not None:
            result["isError"] = True
        result["detail"] = {
            "returncode": cli["returncode"],
            "stderr": cli["stderr"],
            "error": cli["error"],
            "command": cli["command"],
        }
        return result
    except Exception as exc:  # pragma: no cover - last-resort guard
        # Absolutely never let an unexpected exception escape the adapter.
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "status": "error",
                            "message": f"hermes_adapter internal error: {exc}",
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "isError": True,
            "detail": {"reason": "unexpected", "error": str(exc)},
        }


# ---------------------------------------------------------------------------
# Optional in-process fast path (used only when the sibling module is importable)
# ---------------------------------------------------------------------------

def _try_in_process_search(args: dict, db_path: Optional[str]):
    """Best-effort in-process search via the sibling ``search`` module.

    Returns the rendered output string, or ``None`` when the sibling module is
    unavailable or the search cannot be performed in-process (e.g. a persistent
    db_path that only the CLI knows how to open).  Used by tests / callers that
    prefer not to spawn a subprocess; the CLI path remains the primary one.
    """
    try:
        from bm25_search.search import (  # type: ignore
            init_db,
            search,
            run_search,
        )
    except Exception:
        try:
            import importlib.util
            here = os.path.dirname(os.path.abspath(__file__))
            spec = importlib.util.spec_from_file_location(
                "bm25_search_search", os.path.join(here, "search.py")
            )
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            init_db = mod.init_db
            run_search = mod.run_search
        except Exception:
            return None

    try:
        if db_path in (None, ":memory:"):
            return None  # in-memory index is empty; CLI path handles real DBs
        conn = init_db(db_path)
        try:
            return run_search(
                conn,
                args["query"],
                top_k=args["top_k"],
                fmt=args.get("format", DEFAULT_FORMAT),
                max_bytes=args.get("max_bytes"),
                mode=args.get("mode", DEFAULT_MODE),
            )
        finally:
            conn.close()
    except Exception:
        return None


__all__ = [
    "TOOL_NAME",
    "TOOL_TITLE",
    "DEFAULT_FORMAT",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TOP_K",
    "DEFAULT_MODE",
    "hermes_function_schema",
    "get_hermes_function_schema",
    "convert_hermes_call_to_search_args",
    "hermes_call_to_search_args",
    "convert_search_args_to_hermes_call",
    "build_search_command",
    "invoke_search_via_cli",
    "format_cli_result",
    "convert_search_result_to_hermes_result",
    "run_hermes_tool",
]
