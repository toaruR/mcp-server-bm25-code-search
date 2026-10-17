from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from ..identity import digest
from ..models import Artifact, SessionError


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except ValueError:
        return None


def jsonl(path):
    diagnostics, records = [], []
    with Path(path).open("rb") as stream:
        for line_number, raw in enumerate(stream, 1):
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError()
                records.append((line_number, event))
            except (ValueError, UnicodeError):
                diagnostics.append(dict(code="incomplete_tail" if not raw.endswith(b"\n") else "malformed_record", line=line_number))
    return records, diagnostics


def text_blocks(value, allowed=("text", "input_text", "output_text")):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(block.get("text", "") for block in value
                         if isinstance(block, dict) and block.get("type") in allowed and isinstance(block.get("text"), str))
    return ""


def discover_jsonl(source):
    seen = set()
    for root in source.roots:
        path = Path(root)
        candidates = [path] if path.is_file() else sorted(path.rglob("*.jsonl"))
        for candidate in candidates:
            key = str(candidate.resolve())
            if key not in seen:
                seen.add(key)
                yield Artifact(key, [candidate])


def fingerprint(paths, maximum=64 * 1024 * 1024, cache=None):
    parts = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            parts.append((str(path), None))
            continue
        stat = path.stat()
        if stat.st_size > maximum:
            raise SessionError("artifact_too_large")
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        hashed = cache.get(key) if cache is not None else None
        if hashed is None:
            content = hashlib.sha256()
            with path.open("rb") as stream:
                for data in iter(lambda: stream.read(1024 * 1024), b""):
                    content.update(data)
            hashed = content.hexdigest()
            if cache is not None:
                cache[key] = hashed
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SessionError("source_changed")
        parts.append((str(path), stat.st_size, stat.st_mtime_ns, hashed))
    return digest(parts)


@contextmanager
def readonly(path):
    """Native ro transaction. Stable no-WAL fallback never ignores a live WAL."""
    path = Path(path).resolve()
    uri = path.as_uri() + "?mode=ro"
    connection = None
    stable = None
    wal = Path(str(path) + "-wal")
    journal = Path(str(path) + "-journal")
    try:
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            if connection:
                connection.close()
                connection = None
            if wal.exists() or journal.exists():
                raise
            stable = fingerprint([path, wal], 1024 * 1024 * 1024)
            connection = sqlite3.connect(uri + "&immutable=1", uri=True, timeout=2)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SessionError("source_unavailable")
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        if connection:
            connection.close()
    if stable is not None and (wal.exists() or journal.exists() or fingerprint([path, wal], 1024 * 1024 * 1024) != stable):
        raise SessionError("source_changed")


def is_recall(name):
    return any(part in str(name).lower() for part in ("session_search", "session_read", "recall_search", "recall_export", "knowledge_search", "knowledge_read"))
