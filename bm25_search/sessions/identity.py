from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

from .models import SessionError


def digest(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def path_text(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SessionError("project_unresolved")
    if value.lower().startswith("file:"):
        parsed = urlsplit(value)
        value = ("//" + parsed.netloc if parsed.netloc else "") + unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        raise SessionError("project_unresolved")
    return value


def canonical(value):
    value = path_text(str(value))
    if re.match(r"^[A-Za-z]:[/\\]", value) or value.startswith("\\\\"):
        if os.name == "nt":
            return os.path.normcase(str(Path(value).resolve()))
        return ntpath.normcase(ntpath.normpath(value))
    return str(Path(value).expanduser().resolve())


def project_root(value="."):
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise SessionError("project_required")
    try:
        # stdin=DEVNULL: on Windows, inheriting an MCP server's stdin pipe while
        # the main thread blocks reading it stalls process creation until the
        # next request arrives, so background session sync hung between calls.
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=5)
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return path


@lru_cache(maxsize=512)
def historical_root(value):
    decoded = path_text(value)
    path = Path(decoded)
    if not path.is_absolute() and not re.match(r"^[A-Za-z]:[/\\]", decoded) and not decoded.startswith("\\\\"):
        raise SessionError("project_unresolved")
    # A Windows path on POSIX is not a local relative directory.
    if path.is_dir() and (os.name == "nt" or not re.match(r"^[A-Za-z]:", decoded)):
        return canonical(project_root(decoded))
    return canonical(decoded)


def matches_project(projects, root, aliases=()):
    candidates = {canonical(root), *(canonical(alias) for alias in aliases)}
    resolved = set()
    for value in projects:
        try:
            decoded = path_text(value)
            if not Path(decoded).is_absolute() and not re.match(r"^[A-Za-z]:[/\\]", decoded) and not decoded.startswith("\\\\"):
                return False
            direct = canonical(value)
            resolved.add(direct if direct in candidates else historical_root(value))
        except (SessionError, OSError, ValueError):
            return False
    return bool(resolved) and resolved.issubset(candidates)


def session_uid(source, native_id):
    return "s_" + digest(source.agent, source.source_id, native_id)


def message_uid(sid, native_id):
    return "m_" + digest(sid, native_id)
