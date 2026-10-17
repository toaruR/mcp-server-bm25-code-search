from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

AGENTS = ("claude_code", "codex", "agy", "cursor")


class SessionError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


@dataclass
class Message:
    native_id: str
    role: str
    body: str
    occurred_at: Optional[str] = None
    kind: str = "message"
    call_id: Optional[str] = None
    locator: dict = field(default_factory=dict)
    truncated: bool = False
    inherited: bool = False


@dataclass
class Session:
    native_id: str
    projects: list
    messages: list
    title: str = ""
    diagnostics: list = field(default_factory=list)
    parent_id: Optional[str] = None
    completeness: str = "complete"
    metadata: dict = field(default_factory=dict)


@dataclass
class Artifact:
    key: str
    paths: list
    data: dict = field(default_factory=dict)


@dataclass
class Source:
    source_id: str
    agent: str
    roots: list
    enabled: bool = True
    variant: str = ""


def diagnostic(code, **details):
    return dict(code=code, **details)
