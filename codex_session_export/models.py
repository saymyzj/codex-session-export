from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AssetRef:
    source: str
    label: str = "image"
    exported_name: str | None = None


@dataclass
class Event:
    index: int
    timestamp: str | None
    role: str
    kind: str
    title: str
    text: str = ""
    details: str = ""
    raw: dict[str, Any] | None = None
    call_id: str | None = None
    tool_name: str | None = None
    command: str | None = None
    cwd: str | None = None
    exit_code: int | None = None
    files: list[str] = field(default_factory=list)
    assets: list[AssetRef] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)
    importance: int = 1
    collapsed: bool = True


@dataclass
class SessionMeta:
    source_path: Path
    source_sha256: str
    session_id: str | None = None
    originator: str | None = None
    cwd: str | None = None
    started_at: str | None = None
    title: str = "AI Agent 工作流使用记录"


@dataclass
class Report:
    meta: SessionMeta
    events: list[Event]
    stats: dict[str, int]
    redaction_counts: dict[str, int]
    parse_warnings: list[str] = field(default_factory=list)

