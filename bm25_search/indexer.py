"""Indexer for the BM25 code-search skill.

Implements the four responsibilities from the design spec (sections 4.3 / 4.5):

1. **File collection + extension filtering** -- ``git ls-files --cached
   --others --exclude-standard`` is used so ``.gitignore`` semantics
   (negation ``!``, nested ``.gitignore``, compound wildcards) are handled by
   git itself, i.e. 100% equivalent to the official rules.  The result is then
   narrowed to the target extension list.
2. **Fixed chunking** -- 80-line blocks with a 20-line overlap (stride 60);
   the trailing remainder becomes a final, shorter chunk.
3. **Incremental update** -- ``repo_state.last_commit_hash`` is compared to the
   current ``HEAD``; on a branch switch ``git diff --name-status <old> HEAD``
   drives deletions (``D``) and re-indexing (``A``/``M``/``R``).
4. **Worktree warm start** -- a parent worktree's ``.bm25_index.db`` is copied
   into a fresh worktree and then diff-synced, avoiding a full rebuild.

Only the Python standard library is used (``sqlite3`` + ``subprocess``).

The module is import-safe on its own: ``bm25_search.db`` /
``bm25_search.tokenizer`` are used when present (after integration), otherwise
equivalent local fallbacks are used, so ``indexer.py`` works standalone.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Chunking parameters (design spec 4.3.2): 80-line blocks, 20-line overlap.
CHUNK_SIZE = 80
CHUNK_OVERLAP = 20
#: Distance between two consecutive chunk starts (80 - 20 = 60).
CHUNK_STRIDE = CHUNK_SIZE - CHUNK_OVERLAP

#: Worktree-local index file (kept out of git; see spec 4.5.1/4.5.2).
DB_FILENAME = ".bm25_index.db"

#: Target extensions (verbatim from design spec 4.3.1).
TARGET_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro",
    ".html", ".htm", ".hbs", ".ejs", ".twig",
    ".php", ".phtml", ".blade.php",
    ".css", ".scss", ".sass", ".less", ".styl",
    ".py", ".go", ".java", ".rs", ".c", ".cpp", ".h",
    ".md", ".json", ".yaml", ".toml", ".sql",
})

# Aliases so callers/tests can use whichever name they expect.
DEFAULT_EXTENSIONS = TARGET_EXTENSIONS
SUPPORTED_EXTENSIONS = TARGET_EXTENSIONS
INDEX_EXTENSIONS = TARGET_EXTENSIONS

LAST_COMMIT_KEY = "last_commit_hash"


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def _run_git(args: Sequence[str], root: str | os.PathLike) -> subprocess.CompletedProcess:
    """Run ``git <args>`` inside *root* (never through a shell)."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        shell=False,
    )


def is_git_repo(root: str | os.PathLike) -> bool:
    """True when *root* is inside a git working tree."""
    try:
        proc = _run_git(["rev-parse", "--is-inside-work-tree"], root)
    except (OSError, FileNotFoundError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def get_head_commit(root: str | os.PathLike) -> Optional[str]:
    """Current ``HEAD`` hash, or ``None`` when unavailable (e.g. no commits)."""
    try:
        proc = _run_git(["rev-parse", "HEAD"], root)
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# Alias (spec wording: "Git HEAD tracking")
get_current_head = get_head_commit


def matches_extension(path: str | os.PathLike,
                      extensions: Optional[Iterable[str]] = None) -> bool:
    """True when *path*'s filename ends with one of the target extensions.

    Suffix matching (rather than ``os.path.splitext``) is used so compound
    extensions such as ``.blade.php`` are honoured.  Matching is
    case-insensitive.
    """
    exts = TARGET_EXTENSIONS if extensions is None else extensions
    name = os.path.basename(str(path)).lower()
    for ext in exts:
        e = ext.lower()
        if not e.startswith("."):
            e = "." + e
        if name.endswith(e) and len(name) > len(e):
            return True
    return False


def filter_by_extension(paths: Iterable[str],
                        extensions: Optional[Iterable[str]] = None) -> list[str]:
    """Keep only the paths whose extension is in the target list."""
    return [p for p in paths if matches_extension(p, extensions)]


def git_ls_files(root: str | os.PathLike) -> list[str]:
    """Raw ``git ls-files --cached --others --exclude-standard`` output.

    Tracked *and* untracked-but-not-ignored files are returned as
    worktree-root-relative POSIX paths.  ``.gitignore`` handling is delegated
    entirely to git, so nested/negated patterns behave exactly as git does.
    """
    proc = _run_git(
        ["ls-files", "--cached", "--others", "--exclude-standard", "-z"], root
    )
    if proc.returncode != 0:
        return []
    out = [p for p in proc.stdout.split("\0") if p]
    # `--cached` also lists index entries whose file was deleted on disk; those
    # must not be indexed.  De-duplicate while preserving order.
    seen: set[str] = set()
    files: list[str] = []
    root_p = Path(root)
    for rel in out:
        if rel in seen:
            continue
        seen.add(rel)
        if (root_p / rel).is_file():
            files.append(rel)
    return files


def _walk_files(root: str | os.PathLike) -> list[str]:
    """Fallback collection for a non-git directory (no .gitignore semantics)."""
    root_p = Path(root)
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_p):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "__pycache__", "node_modules"}]
        for fn in filenames:
            rel = (Path(dirpath) / fn).relative_to(root_p).as_posix()
            files.append(rel)
    return sorted(files)


def collect_files(root: str | os.PathLike = ".",
                  extensions: Optional[Iterable[str]] = None) -> list[str]:
    """Collect indexable files under *root* (relative POSIX paths, sorted).

    ``git ls-files --cached --others --exclude-standard`` supplies the
    candidate set so ignored files are excluded by git itself; the extension
    filter is applied afterwards.  Falls back to a plain directory walk when
    *root* is not a git repository.
    """
    root = str(root)
    if is_git_repo(root):
        candidates = git_ls_files(root)
    else:
        candidates = _walk_files(root)
    return sorted(filter_by_extension(candidates, extensions))


# Aliases
collect_target_files = collect_files
collect_indexable_files = collect_files


# ---------------------------------------------------------------------------
# Chunking: 80-line blocks / 20-line overlap
# ---------------------------------------------------------------------------

#: Default forward look-ahead (in lines) for :func:`chunk_lines`'s boundary
#: snapping (design-spec gap-closing plan, item 2-A).
DEFAULT_SNAP_LOOKAHEAD = 10


def _snap_end_to_blank_line(lines: Sequence[str], ideal_end: int, total: int,
                            lookahead: int) -> int:
    """Extend *ideal_end* forward to just past the nearest blank line.

    Scans ``lines[ideal_end : ideal_end + lookahead]`` only -- never
    backward -- for the first blank line and, if found, returns its index+1
    so the blank line becomes the chunk's last line.  Falls back to
    *ideal_end* unchanged when no blank line is found in range.  Forward-only
    snapping guarantees the chunk still covers ``[start, ideal_end)`` in
    full, so no coverage gap can appear between consecutive chunks.
    """
    limit = min(ideal_end + lookahead, total)
    for i in range(ideal_end, limit):
        if lines[i].strip() == "":
            return i + 1
    return ideal_end


def chunk_lines(lines: Sequence[str],
                chunk_size: int = CHUNK_SIZE,
                overlap: int = CHUNK_OVERLAP,
                snap_boundaries: bool = True,
                snap_lookahead: int = DEFAULT_SNAP_LOOKAHEAD) -> list[dict]:
    """Split *lines* into overlapping fixed blocks.

    Returns a list of dicts::

        {"start_line": 1, "end_line": 80, "text": "...", "lines": [...]}

    ``start_line`` / ``end_line`` are **1-based and inclusive**, matching the
    ``chunks`` table in the schema.  Consecutive chunks advance by
    ``chunk_size - overlap`` lines (60 by default), so each chunk repeats the
    last ``overlap`` lines of its predecessor.  A trailing remainder shorter
    than *chunk_size* is emitted as a final chunk; a chunk that would be fully
    contained in the previous one is not emitted (no duplicates).

    When *snap_boundaries* is true (default), a non-final chunk's end is
    extended forward -- up to *snap_lookahead* lines -- to the nearest blank
    line, approximating a function/class boundary without any per-language
    parsing (design-spec gap-closing plan, item 2-A).  Most languages in the
    target extension list separate top-level definitions with a blank line,
    so this is a language-agnostic, standard-library-only heuristic; content
    with no nearby blank line (dense config formats, minified code) falls
    back to the exact fixed-length cut unchanged.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk_size")

    total = len(lines)
    if total == 0:
        return []

    stride = chunk_size - overlap
    chunks: list[dict] = []
    start = 0
    while start < total:
        end = min(start + chunk_size, total)
        if snap_boundaries and end < total:
            end = _snap_end_to_blank_line(lines, end, total, snap_lookahead)
        block = list(lines[start:end])
        chunks.append({
            "start_line": start + 1,
            "end_line": end,
            "text": "".join(block) if block and block[0].endswith("\n")
                    else "\n".join(block),
            "lines": block,
        })
        if end >= total:
            break
        start += stride
    return chunks


def chunk_text(text: str,
               chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Chunk a raw string; newlines are preserved inside each chunk's text."""
    if text == "":
        return []
    lines = text.splitlines()
    out = chunk_lines(lines, chunk_size, overlap)
    for c in out:
        c["text"] = "\n".join(c["lines"])
        c["raw_snippet"] = c["text"]
    return out


def chunk_file(path: str | os.PathLike,
               chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Read a file (binary-safe, errors replaced) and chunk it 80/20."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return chunk_text(text, chunk_size, overlap)


# Aliases
make_chunks = chunk_lines
chunk = chunk_text


# ---------------------------------------------------------------------------
# tokenizer / db integration (with standalone fallbacks)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on integration order
    from bm25_search.tokenizer import pre_tokenize as _pre_tokenize  # type: ignore
except Exception:  # pragma: no cover
    try:
        from .tokenizer import pre_tokenize as _pre_tokenize  # type: ignore
    except Exception:
        _pre_tokenize = None  # type: ignore

if _pre_tokenize is None:  # pragma: no cover - fallback only
    import re as _re

    _CAMEL_RE = _re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
    _CJK_RE = _re.compile(r"[\u3040-\u30ff\u3400-\u9fff]+")
    _WORD_RE = _re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u3040-\u30ff\u3400-\u9fff]+")

    def _pre_tokenize(raw_text: str) -> str:  # type: ignore[misc]
        tokens: list[str] = []
        for m in _WORD_RE.finditer(raw_text):
            w = m.group(0)
            if _CJK_RE.fullmatch(w):
                tokens.extend([w[i:i + 2] for i in range(len(w) - 1)] or [w])
            else:
                tokens.append(w.lower())
                if "_" in w or _re.search(r"[a-z][A-Z]", w):
                    for piece in w.split("_"):
                        tokens.extend(s.lower()
                                      for s in _CAMEL_RE.findall(piece) if s)
        return " ".join(tokens)


def pre_tokenize(raw_text: str) -> str:
    """Pre-tokenize text for the FTS ``index_text`` column."""
    return _pre_tokenize(raw_text)


try:  # pragma: no cover
    from bm25_search.db import init_db as _init_db  # type: ignore
except Exception:  # pragma: no cover
    try:
        from .db import init_db as _init_db  # type: ignore
    except Exception:
        _init_db = None  # type: ignore

#: Local copy of the v2 schema, used when ``bm25_search.db`` is unavailable so
#: the indexer can be exercised standalone.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath   TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    raw_snippet TEXT NOT NULL,
    index_text  TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    filepath, index_text,
    content='chunks', content_rowid='chunk_id',
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
CREATE TABLE IF NOT EXISTS file_metadata (
    filepath  TEXT PRIMARY KEY,
    mtime     REAL NOT NULL,
    file_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repo_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_db(conn_or_path):
    """Create the v2 schema, delegating to ``bm25_search.db`` when available."""
    if _init_db is not None:
        return _init_db(conn_or_path)
    conn = (sqlite3.connect(conn_or_path)
            if not isinstance(conn_or_path, sqlite3.Connection) else conn_or_path)
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn


def file_hash(path: str | os.PathLike) -> str:
    """SHA-256 of the file's bytes (used for change detection)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# repo_state helpers (HEAD tracking)
# ---------------------------------------------------------------------------

def set_repo_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO repo_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_repo_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM repo_state WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row is not None else None


def get_last_commit_hash(conn: sqlite3.Connection) -> Optional[str]:
    """Previously indexed ``HEAD`` (``repo_state.last_commit_hash``)."""
    return get_repo_state(conn, LAST_COMMIT_KEY)


def set_last_commit_hash(conn: sqlite3.Connection, commit: str) -> None:
    set_repo_state(conn, LAST_COMMIT_KEY, commit)


# ---------------------------------------------------------------------------
# git diff --name-status parsing
# ---------------------------------------------------------------------------

def parse_name_status(output: str) -> dict[str, list[str]]:
    """Parse ``git diff --name-status`` into ``{"deleted": [...], "changed": [...]}``.

    * ``D``      -> deleted (purge the file's chunks)
    * ``A``/``M``/``T`` -> changed (re-index)
    * ``R``      -> rename: the *old* path is deleted, the *new* path re-indexed
    * ``C``      -> copy: the source still exists, so only the *new* path is
                    re-indexed (deleting the source here would drop a live file)
    """
    deleted: list[str] = []
    changed: list[str] = []
    for raw in output.splitlines():
        line = raw.strip("\0").strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.split()
            if len(parts) < 2:
                continue
        status = parts[0].strip()
        code = status[0].upper() if status else ""
        if code == "D":
            deleted.append(parts[1])
        elif code == "R":
            deleted.append(parts[1])
            if len(parts) >= 3:
                changed.append(parts[2])
        elif code == "C":
            changed.append(parts[-1])
        elif code in ("A", "M", "T"):
            changed.append(parts[1])
        else:
            changed.append(parts[-1])
    return {"deleted": deleted, "changed": changed}


def git_diff_name_status(root: str | os.PathLike,
                         old_hash: str,
                         new_hash: str = "HEAD") -> dict[str, list[str]]:
    """``git diff --name-status <old> <new>`` reduced to deleted/changed lists."""
    proc = _run_git(["diff", "--name-status", old_hash, new_hash], root)
    if proc.returncode != 0:
        return {"deleted": [], "changed": []}
    return parse_name_status(proc.stdout)


# Alias
diff_files = git_diff_name_status


# ---------------------------------------------------------------------------
# chunk-level DB operations
# ---------------------------------------------------------------------------

def delete_file_chunks(conn: sqlite3.Connection, filepath: str) -> int:
    """Purge every chunk (and FTS row, via trigger) belonging to *filepath*."""
    cur = conn.execute("DELETE FROM chunks WHERE filepath = ?", (filepath,))
    conn.execute("DELETE FROM file_metadata WHERE filepath = ?", (filepath,))
    conn.commit()
    return cur.rowcount


# Aliases
remove_file = delete_file_chunks
purge_file = delete_file_chunks


def index_file(conn: sqlite3.Connection,
               root: str | os.PathLike,
               filepath: str,
               chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> int:
    """(Re-)index a single file: delete its old chunks, then insert new ones.

    *filepath* is relative to *root* and stored verbatim (relative paths keep
    the index worktree-portable, spec 4.5.3).  Returns the number of chunks
    inserted; a missing file is purged and yields 0.
    """
    abs_path = Path(root) / filepath
    delete_file_chunks(conn, filepath)
    if not abs_path.is_file():
        return 0

    text = abs_path.read_text(encoding="utf-8", errors="replace")
    chunks = chunk_text(text, chunk_size, overlap)
    path_tokens = pre_tokenize(filepath)
    rows = [
        (filepath, c["start_line"], c["end_line"], c["text"],
         f"{path_tokens} {pre_tokenize(c['text'])}".strip())
        for c in chunks
    ]
    if rows:
        conn.executemany(
            "INSERT INTO chunks (filepath, start_line, end_line, raw_snippet, "
            "index_text) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    try:
        st = abs_path.stat()
        conn.execute(
            "INSERT INTO file_metadata (filepath, mtime, file_hash) "
            "VALUES (?, ?, ?) ON CONFLICT(filepath) DO UPDATE SET "
            "mtime=excluded.mtime, file_hash=excluded.file_hash",
            (filepath, st.st_mtime, file_hash(abs_path)),
        )
    except OSError:  # pragma: no cover
        pass
    conn.commit()
    return len(rows)


def indexed_files(conn: sqlite3.Connection) -> set[str]:
    """Every filepath currently present in the index."""
    return {r[0] for r in conn.execute("SELECT DISTINCT filepath FROM chunks")}


def count_chunks(conn: sqlite3.Connection,
                 filepath: Optional[str] = None) -> int:
    """Number of chunks in the index, optionally scoped to one file."""
    if filepath is None:
        row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE filepath = ?", (filepath,)
        ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Warm start: copy a parent worktree's index
# ---------------------------------------------------------------------------

def db_path_for(root: str | os.PathLike) -> Path:
    """Worktree-local DB path (``<root>/.bm25_index.db``, spec 4.5.2)."""
    return Path(root) / DB_FILENAME


def copy_index_from_parent(parent: str | os.PathLike,
                           target: str | os.PathLike,
                           overwrite: bool = False) -> bool:
    """Copy ``.bm25_index.db`` from *parent* into *target* (warm start).

    Either directory may be given as the worktree root or as a direct path to
    the DB file.  Returns ``True`` when a copy was made, ``False`` when the
    source is missing or the target already exists (and *overwrite* is False).
    """
    src = Path(parent)
    if src.is_dir():
        src = db_path_for(src)
    dst = Path(target)
    if dst.is_dir() or not dst.suffix:
        dst = db_path_for(dst)

    if not src.is_file():
        return False
    if dst.exists() and not overwrite:
        return False
    if src.resolve() == dst.resolve():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


# Aliases
warm_start = copy_index_from_parent
copy_parent_index = copy_index_from_parent


def warmstart_from_parent(parent: str | os.PathLike,
                          target: str | os.PathLike,
                          overwrite: bool = False,
                          sync: bool = True) -> dict:
    """Warm-start *target* from *parent*, then diff-sync against its ``HEAD``.

    Returns a report dict with ``copied`` plus the sync counters (see
    :func:`sync_index`), so the caller can tell a warm start from a cold build.
    """
    copied = copy_index_from_parent(parent, target, overwrite=overwrite)
    report: dict = {"copied": copied, "db_path": str(db_path_for(target))}
    if sync:
        report.update(sync_index(target))
    report["copied"] = copied
    report["warm_start"] = copied
    return report


# ---------------------------------------------------------------------------
# Build / sync entry points
# ---------------------------------------------------------------------------

def build_index(root: str | os.PathLike = ".",
                db_path: Optional[str | os.PathLike] = None,
                conn: Optional[sqlite3.Connection] = None,
                extensions: Optional[Iterable[str]] = None,
                chunk_size: int = CHUNK_SIZE,
                overlap: int = CHUNK_OVERLAP) -> dict:
    """Full build: index every collected file and record the current ``HEAD``."""
    own = conn is None
    if conn is None:
        conn = init_db(str(db_path or db_path_for(root)))
    else:
        init_db(conn)

    files = collect_files(root, extensions)
    total = 0
    for rel in files:
        total += index_file(conn, root, rel, chunk_size, overlap)

    head = get_head_commit(root)
    if head:
        set_last_commit_hash(conn, head)
    conn.commit()

    report = {
        "files": len(files),
        "indexed": len(files),
        "chunks": total,
        "head": head,
        "full_build": True,
    }
    if own:
        report["conn"] = conn
    return report


def sync_index(root: str | os.PathLike = ".",
               db_path: Optional[str | os.PathLike] = None,
               conn: Optional[sqlite3.Connection] = None,
               extensions: Optional[Iterable[str]] = None,
               chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> dict:
    """Incremental update driven by ``git diff`` + ``HEAD`` tracking (spec 4.5.4).

    * When the stored ``last_commit_hash`` differs from the current ``HEAD``,
      ``git diff --name-status <old> HEAD`` is used: ``D`` paths are purged and
      ``A``/``M``/``R`` paths are re-indexed (extension filter still applies).
    * Files that git no longer reports (deleted or newly ignored) are purged,
      and files absent from the index are added, so a stale copied DB
      (warm start) converges to the current tree.
    * An empty index, or a missing ``last_commit_hash``, falls back to a full
      build.
    """
    own = conn is None
    if conn is None:
        conn = init_db(str(db_path or db_path_for(root)))
    else:
        init_db(conn)

    head = get_head_commit(root)
    last = get_last_commit_hash(conn)
    known = indexed_files(conn)

    # Nothing indexed yet -> full build.
    if not known:
        rep = build_index(root, conn=conn, extensions=extensions,
                          chunk_size=chunk_size, overlap=overlap)
        rep.setdefault("deleted", 0)
        rep.setdefault("updated", rep.get("indexed", 0))
        if own:
            rep["conn"] = conn
        return rep

    current = set(collect_files(root, extensions))
    deleted: set[str] = set()
    changed: set[str] = set()

    # 1) branch switch / new commits -> git diff
    if head and last and head != last:
        diff = git_diff_name_status(root, last, head)
        for p in diff["deleted"]:
            if matches_extension(p, extensions):
                deleted.add(p)
        for p in diff["changed"]:
            if matches_extension(p, extensions):
                changed.add(p)

    # 2) reconcile against what git currently reports (covers uncommitted
    #    edits, ignored-now files, and a warm-started DB from another branch)
    deleted |= (known - current)
    changed |= (current - known)
    changed -= deleted
    deleted -= current

    # 3) content-level change detection for files we already know about
    for rel in sorted(current & known):
        if rel in changed:
            continue
        row = conn.execute(
            "SELECT mtime, file_hash FROM file_metadata WHERE filepath = ?",
            (rel,),
        ).fetchone()
        abs_p = Path(root) / rel
        if row is None:
            changed.add(rel)
            continue
        try:
            st = abs_p.stat()
        except OSError:  # pragma: no cover
            deleted.add(rel)
            continue
        if abs(st.st_mtime - float(row[0])) > 1e-6 and file_hash(abs_p) != row[1]:
            changed.add(rel)

    for rel in sorted(deleted):
        delete_file_chunks(conn, rel)

    chunks = 0
    for rel in sorted(changed):
        chunks += index_file(conn, root, rel, chunk_size, overlap)

    if head:
        set_last_commit_hash(conn, head)
    conn.commit()

    report = {
        "deleted": len(deleted),
        "updated": len(changed),
        "indexed": len(changed),
        "chunks": chunks,
        "deleted_files": sorted(deleted),
        "updated_files": sorted(changed),
        "head": head,
        "previous_head": last,
        "branch_switched": bool(head and last and head != last),
        "full_build": False,
    }
    if own:
        report["conn"] = conn
    return report


# Aliases
incremental_update = sync_index
update_index = sync_index


# ---------------------------------------------------------------------------
# Object-oriented surface
# ---------------------------------------------------------------------------

class Indexer:
    """Object wrapper binding a worktree root to its local index DB.

    ``Indexer(root)`` uses the worktree-local ``.bm25_index.db``; pass
    ``db_path=":memory:"`` (or any path) to override, or ``conn=`` to reuse an
    existing connection.
    """

    def __init__(self,
                 root: str | os.PathLike = ".",
                 db_path: Optional[str | os.PathLike] = None,
                 conn: Optional[sqlite3.Connection] = None,
                 extensions: Optional[Iterable[str]] = None,
                 chunk_size: int = CHUNK_SIZE,
                 overlap: int = CHUNK_OVERLAP) -> None:
        self.root = str(root)
        self.extensions = frozenset(extensions) if extensions else TARGET_EXTENSIONS
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.db_path = str(db_path) if db_path else str(db_path_for(self.root))
        if conn is not None:
            self.conn = conn
            init_db(self.conn)
        else:
            self.conn = init_db(self.db_path)

    # -- collection / chunking ------------------------------------------
    def collect_files(self) -> list[str]:
        return collect_files(self.root, self.extensions)

    def chunk_file(self, filepath: str) -> list[dict]:
        return chunk_file(Path(self.root) / filepath,
                          self.chunk_size, self.overlap)

    # -- build / sync ---------------------------------------------------
    def build(self) -> dict:
        return build_index(self.root, conn=self.conn,
                           extensions=self.extensions,
                           chunk_size=self.chunk_size, overlap=self.overlap)

    build_index = build
    full_build = build

    def sync(self) -> dict:
        return sync_index(self.root, conn=self.conn,
                          extensions=self.extensions,
                          chunk_size=self.chunk_size, overlap=self.overlap)

    incremental_update = sync
    update = sync

    # -- single file ----------------------------------------------------
    def index_file(self, filepath: str) -> int:
        return index_file(self.conn, self.root, filepath,
                          self.chunk_size, self.overlap)

    def delete_file(self, filepath: str) -> int:
        return delete_file_chunks(self.conn, filepath)

    # -- state / introspection ------------------------------------------
    def head(self) -> Optional[str]:
        return get_head_commit(self.root)

    def last_commit_hash(self) -> Optional[str]:
        return get_last_commit_hash(self.conn)

    def indexed_files(self) -> set[str]:
        return indexed_files(self.conn)

    def count_chunks(self, filepath: Optional[str] = None) -> int:
        return count_chunks(self.conn, filepath)

    # -- warm start -----------------------------------------------------
    def warm_start_from(self, parent: str | os.PathLike,
                        overwrite: bool = True) -> bool:
        """Replace this index with the parent's copy, then reopen + sync."""
        self.conn.close()
        copied = copy_index_from_parent(parent, self.db_path, overwrite=overwrite)
        self.conn = init_db(self.db_path)
        if copied:
            self.sync()
        return copied

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "Indexer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = [
    # constants
    "CHUNK_SIZE", "CHUNK_OVERLAP", "CHUNK_STRIDE", "DB_FILENAME",
    "DEFAULT_SNAP_LOOKAHEAD",
    "TARGET_EXTENSIONS", "DEFAULT_EXTENSIONS", "SUPPORTED_EXTENSIONS",
    "INDEX_EXTENSIONS", "LAST_COMMIT_KEY",
    # git / collection
    "is_git_repo", "get_head_commit", "get_current_head", "git_ls_files",
    "collect_files", "collect_target_files", "collect_indexable_files",
    "matches_extension", "filter_by_extension",
    # chunking
    "chunk_lines", "chunk_text", "chunk_file", "make_chunks",
    # tokenize / db
    "pre_tokenize", "init_db", "file_hash",
    # state
    "set_repo_state", "get_repo_state", "get_last_commit_hash",
    "set_last_commit_hash",
    # diff / incremental
    "parse_name_status", "git_diff_name_status", "diff_files",
    "delete_file_chunks", "remove_file", "purge_file", "index_file",
    "indexed_files", "count_chunks",
    "build_index", "sync_index", "incremental_update", "update_index",
    # warm start
    "db_path_for", "copy_index_from_parent", "warm_start",
    "copy_parent_index", "warmstart_from_parent",
    # class
    "Indexer",
]



