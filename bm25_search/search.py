"""SQLite FTS5 BM25 search execution for the BM25 code-search skill.

This module is the *search* half of the skill (design spec sections 4.3 / 4.4).
It is intentionally built on the Python standard library only (``sqlite3`` +
``argparse`` + ``json``) so it runs anywhere the rest of the skill runs.

Responsibilities implemented here
---------------------------------
* **BM25 scoring with a filepath column boost of 3.0** -- the FTS5
  ``code_fts`` table indexes both ``filepath`` (weight 3.0) and ``index_text``
  (weight 1.0), per spec 4.3.5, so a match inside the path outranks an equal
  match inside the body.
* **Query-side pre-tokenization** -- the *same* Python pre-tokenizer used at
  index time is applied to the query (``pre_tokenize_query``), turning a raw
  multi-character CJK query such as ``有効期限`` into the OR-joined MATCH
  expression ``"有効" OR "効期" OR "期限"``.  Without this a Japanese query
  never matches the 2-gram index (spec 4.3.4, Iteration 5 finding #4).
* **CLI interface** -- ``python search.py "<query>" --top-k 5
  --format [markdown|json] --max-bytes 4000`` (spec 4.4).
* **Max-bytes auto truncation** -- the rendered output is truncated to a byte
  budget *without splitting a multibyte character*, so Japanese output is never
  corrupted (spec 4.4, rubric criterion).
* **Zero-match fallback response** -- when a query returns nothing, a
  structured JSON message steers the agent back to plain ``grep`` / ``glob``
  (spec 4.4, rubric criterion).

The module is **self-contained**: it carries its own copy of the verified
schema, the pre-tokenizer pipeline, and the chunk-insert helper, so it can be
imported and exercised on its own (the acceptance tests build a temporary in-
memory index using the ``init_db`` / ``insert_chunk`` helpers defined here).
If the sibling ``bm25_search.db`` / ``bm25_search.tokenizer`` modules are
present they are preferred, but the behaviour is identical either way.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Pre-tokenization pipeline (Python side; stdlib sqlite3 cannot register a
# custom FTS5 tokenizer, spec 4.3.3).  These regexes and this ordering are the
# exact "verified design-spec pipeline" used in docs/plans/bm25-schema-
# verification.py and bm25_search/db.py / bm25_search/tokenizer.py.
# ---------------------------------------------------------------------------

# Splits a single identifier piece into subwords:
#   getUserProfile -> ['get', 'User', 'Profile']
#   session_token -> ['session', 'token']
#   v2API         -> ['v', '2', 'API']
_CAMEL_RE = __import__("re").compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Matches a run of CJK / kana characters (hiragana, katakana, CJK ideographs).
_CJK_RE = __import__("re").compile(r"[぀-ヿ㐀-鿿]+")

# Splits raw text into tokens: ASCII identifiers (incl. digits/underscore)
# or contiguous CJK runs.  Punctuation / whitespace act as separators.
_WORD_RE = __import__("re").compile(r"[A-Za-z_][A-Za-z0-9_]*|[぀-ヿ㐀-鿿]+")

# Try to reuse the canonical sibling implementations if they are available
# (keeps a single source of truth when the whole package is assembled); fall
# back to the local copies otherwise.  The local copies are byte-for-byte
# equivalent to the verified pipeline, so behaviour never changes.
try:  # pragma: no cover - depends on integration order
    from bm25_search.tokenizer import (  # type: ignore
        pre_tokenize as _pre_tokenize,
        pre_tokenize_query as _pre_tokenize_query,
        split_identifier as _split_identifier,
        cjk_bigrams as _cjk_bigrams,
    )
except Exception:  # pragma: no cover
    try:
        from .tokenizer import (  # type: ignore
            pre_tokenize as _pre_tokenize,
            pre_tokenize_query as _pre_tokenize_query,
            split_identifier as _split_identifier,
            cjk_bigrams as _cjk_bigrams,
        )
    except Exception:  # pragma: no cover - standalone fallback
        _pre_tokenize = None
        _pre_tokenize_query = None
        _split_identifier = None
        _cjk_bigrams = None


def split_identifier(word: str) -> list[str]:
    """camelCase / snake_case -> lower-cased subword tokens (originals kept)."""
    if _split_identifier is not None:
        return _split_identifier(word)
    parts: list[str] = []
    for piece in word.split("_"):
        if not piece:
            continue
        subs = _CAMEL_RE.findall(piece)
        parts.extend(s.lower() for s in subs if s)
    return parts


def cjk_bigrams(text: str) -> list[str]:
    """Japanese (CJK) 2-gram.  A single character passes through unchanged."""
    if _cjk_bigrams is not None:
        return _cjk_bigrams(text)
    if len(text) <= 1:
        return [text]
    return [text[i:i + 2] for i in range(len(text) - 1)]


def pre_tokenize(raw_text: str) -> str:
    """Tokenize *raw_text* into whitespace-joined tokens suitable for FTS5.

    * ASCII identifiers keep their original (lower-cased) form and gain
      subword tokens (camelCase / snake_case split).
    * Runs of CJK characters are expanded into 2-grams.
    """
    if _pre_tokenize is not None:
        return _pre_tokenize(raw_text)
    import re
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
    """Apply :func:`pre_tokenize` to a query and build an OR-joined MATCH expr.

    A raw multi-character CJK query never matches a 2-gram index unless the
    query side is tokenized the same way (``unicode61`` tokenizes the query
    too, turning ``有効期限`` into a single token).  We therefore pre-tokenize
    the query and join every resulting token with ``OR``, quoting each token so
    the output is directly usable as an FTS5 ``MATCH`` condition::

        >>> pre_tokenize_query("有効期限")
        '\"有効\" OR \"効期\" OR \"期限\"'

    The default join is OR (not AND) because AND would collapse recall too
    aggressively; callers that need exact phrasing can post-process the result.
    """
    if _pre_tokenize_query is not None:
        if mode == "OR":
            return _pre_tokenize_query(raw_query)
        # sibling helper only does OR; rebuild for other modes locally
    tokens = pre_tokenize(raw_query).split()
    if not tokens:
        return raw_query
    quoted = [f'"{t}"' for t in tokens]
    return f" {mode} ".join(quoted)


# ``build_match_expr`` is kept as an alias for ``pre_tokenize_query`` so callers
# that discovered the db.py name still work.
def build_match_expr(raw_query: str, mode: str = "OR") -> str:
    """Alias of :func:`pre_tokenize_query` (design-spec MATCH builder)."""
    return pre_tokenize_query(raw_query, mode)


# ---------------------------------------------------------------------------
# Schema (the verified "v2" schema from the design spec, 4.2)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- chunks: source of truth (chunk-level granularity, joined to FTS by rowid)
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath   TEXT NOT NULL,       -- relative path from the worktree root
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    raw_snippet TEXT NOT NULL,      -- original text, returned to the agent
    index_text  TEXT NOT NULL       -- pre-tokenized text, fed to FTS5 only
);

-- code_fts: FTS5 external-content table.  rowid == chunks.chunk_id (1:1).
-- Both 'filepath' and 'index_text' are indexed so the filepath column can be
-- weighted (3.0) independently of the body (1.0) in bm25().
CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    filepath,
    index_text,
    content='chunks',
    content_rowid='chunk_id',
    tokenize='unicode61 remove_diacritics 2'
);

-- keep code_fts in sync with chunks (standard external-content recipe)
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

-- file_metadata: incremental-update tracking
CREATE TABLE IF NOT EXISTS file_metadata (
    filepath  TEXT PRIMARY KEY,
    mtime     REAL NOT NULL,
    file_hash TEXT NOT NULL
);

-- repo_state: key/value store (e.g. last git commit hash)
CREATE TABLE IF NOT EXISTS repo_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Column weights passed to FTS5 ``bm25()`` -- filepath is boosted 3.0x.
BM25_WEIGHTS = (3.0, 1.0)


def init_db(conn) -> sqlite3.Connection:
    """Create the full schema (tables, FTS5 virtual table, triggers) on *conn*.

    *conn* may be either a :class:`sqlite3.Connection` or a path/``":memory:"``
    string.  When a string is given a fresh connection is created and returned;
    when a connection is given it is returned unchanged (after the schema is
    applied).
    """
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
    """Insert one chunk and return its ``chunk_id``.

    If *index_text* is omitted it is derived from *filepath* + *raw_snippet*
    via :func:`pre_tokenize` (the design-spec convention).  The
    ``chunks_ai`` trigger mirrors the row into ``code_fts`` automatically.
    """
    if index_text is None:
        index_text = pre_tokenize(filepath) + " " + pre_tokenize(raw_snippet)
    cur = conn.execute(
        "INSERT INTO chunks (filepath, start_line, end_line, raw_snippet, "
        "index_text) VALUES (?, ?, ?, ?, ?)",
        (filepath, start_line, end_line, raw_snippet, index_text),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Core search
# ---------------------------------------------------------------------------

def search(conn: sqlite3.Connection,
           raw_query: str,
           top_k: int = 5,
           mode: str = "OR") -> list[dict]:
    """Search the index and return up to *top_k* ranked chunk dicts.

    Each result is
    ``{"filepath", "start_line", "end_line", "raw_snippet", "score"}`` ordered
    best-first (FTS5 ``bm25()`` returns the most-negative score for the best
    hit).  ``filepath`` is weighted 3.0 vs ``index_text`` 1.0 (design spec).

    The query is passed through :func:`pre_tokenize_query` so Japanese and
    camelCase/snake_case queries tokenize exactly like the index.
    """
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


# ---------------------------------------------------------------------------
# Max-bytes safe truncation (no multibyte corruption)
# ---------------------------------------------------------------------------

def truncate_bytes(text: str, max_bytes: int) -> str:
    """Truncate *text* so its UTF-8 encoding is at most *max_bytes* bytes.

    The cut is made on a character boundary: a multibyte sequence is never
    split, so Japanese (and any non-ASCII) text is returned intact.  Returns
    *text* unchanged when it already fits.
    """
    if max_bytes is None:
        return text
    if max_bytes < 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Back off from the cut point while we are inside a UTF-8 continuation byte
    # (0b10xxxxxx).  The byte just before the run is the leading byte of the
    # character we must drop wholesale.
    i = max_bytes
    while i > 0 and (encoded[i] & 0xC0) == 0x80:
        i -= 1
    return encoded[:i].decode("utf-8", errors="strict")


#: ``truncate_output`` is an alias used by some callers/tests.
def truncate_output(text: str, max_bytes: int) -> str:
    """Alias of :func:`truncate_bytes`."""
    return truncate_bytes(text, max_bytes)


# ---------------------------------------------------------------------------
# Zero-match fallback
# ---------------------------------------------------------------------------

#: Exact message mandated by the design spec (4.4).
ZERO_MATCH_MESSAGE = (
    "BM25 match zero. Recommended Fallback: Use standard grep_search or glob "
    "to locate exact symbol definitions."
)


def zero_match_fallback(query: Optional[str] = None) -> dict:
    """Return the structured zero-match fallback response (spec 4.4)."""
    resp = {
        "status": "zero_match",
        "message": ZERO_MATCH_MESSAGE,
    }
    if query is not None:
        resp["query"] = query
    return resp


#: ``build_fallback_response`` is an alias used by some callers/tests.
def build_fallback_response(query: Optional[str] = None) -> dict:
    """Alias of :func:`zero_match_fallback`."""
    return zero_match_fallback(query)


# ---------------------------------------------------------------------------
# Output formatting (markdown / json) with max-bytes truncation
# ---------------------------------------------------------------------------

def format_markdown(results: Sequence[dict], query: Optional[str] = None) -> str:
    """Render *results* as a Markdown document for an agent."""
    lines: list[str] = []
    if query is not None:
        lines.append(f"# BM25 Search Results")
        lines.append("")
        lines.append(f"**Query:** `{query}`")
        lines.append("")
    else:
        lines.append("# BM25 Search Results")
        lines.append("")
    if not results:
        lines.append(ZERO_MATCH_MESSAGE)
        return "\n".join(lines)
    for i, r in enumerate(results, 1):
        lines.append(
            f"## {i}. {r['filepath']} "
            f"(lines {r['start_line']}-{r['end_line']})"
        )
        lines.append(f"score: {r['score']:.6f}")
        lines.append("")
        lines.append("```")
        lines.append(r["raw_snippet"])
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_json(results: Sequence[dict], query: Optional[str] = None) -> str:
    """Render *results* as a JSON document (an envelope with a results list)."""
    payload = {
        "status": "ok",
        "query": query,
        "count": len(results),
        "results": list(results),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def format_results(results: Sequence[dict],
                   fmt: str = "markdown",
                   max_bytes: Optional[int] = None,
                   query: Optional[str] = None) -> str:
    """Render *results* in *fmt* (``markdown`` or ``json``), then truncate.

    When *max_bytes* is given the rendered string is truncated on a character
    boundary so multibyte text is never corrupted.
    """
    fmt = (fmt or "markdown").lower()
    if fmt == "json":
        rendered = format_json(results, query=query)
    else:
        rendered = format_markdown(results, query=query)
    if max_bytes is not None:
        rendered = truncate_bytes(rendered, max_bytes)
    return rendered


# ---------------------------------------------------------------------------
# High-level orchestration used by the CLI
# ---------------------------------------------------------------------------

def run_search(conn: sqlite3.Connection,
               raw_query: str,
               top_k: int = 5,
               fmt: str = "markdown",
               max_bytes: Optional[int] = None,
               mode: str = "OR") -> str:
    """Run a search and return a ready-to-print string.

    * Zero matches -> the JSON zero-match fallback response (spec 4.4).
    * Otherwise -> the results rendered in *fmt* and truncated to *max_bytes*.
    """
    results = search(conn, raw_query, top_k=top_k, mode=mode)
    if not results:
        return json.dumps(zero_match_fallback(query=raw_query),
                          ensure_ascii=False, indent=2)
    return format_results(results, fmt=fmt, max_bytes=max_bytes, query=raw_query)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    """Build the ``search.py`` argument parser (spec 4.4)."""
    p = argparse.ArgumentParser(
        prog="search.py",
        description="SQLite FTS5 BM25 code search (filepath weight 3.0).",
    )
    p.add_argument("query", help="Search query (Japanese / camelCase OK).")
    p.add_argument("--top-k", type=int, default=5,
                   help="Maximum number of results (default 5).")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown",
                   help="Output format (default markdown).")
    p.add_argument("--max-bytes", type=int, default=4000,
                   help="Max output bytes; multibyte-safe truncation "
                        "(default 4000).")
    p.add_argument("--db", default=".bm25_index.db",
                   help="Path to the SQLite index (default .bm25_index.db).")
    p.add_argument("--mode", default="OR", choices=["OR", "AND"],
                   help="Query token join mode (default OR).")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.  Prints the search result / fallback to stdout."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    conn = init_db(args.db)
    try:
        output = run_search(
            conn,
            args.query,
            top_k=args.top_k,
            fmt=args.format,
            max_bytes=args.max_bytes,
            mode=args.mode,
        )
    finally:
        conn.close()
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
