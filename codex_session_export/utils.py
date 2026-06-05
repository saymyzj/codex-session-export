from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SESSION_ROOT = Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class SessionSummary:
    path: Path
    session_id: str | None
    short_id: str
    started_at: str | None
    originator: str | None
    cwd: str | None
    line_count: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any], str]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                yield line_number, json.loads(raw), raw
            except json.JSONDecodeError:
                yield line_number, {"type": "parse_error", "raw": raw}, raw


def truncate_middle(text: str, max_lines: int = 80, max_chars: int = 24000) -> tuple[str, bool]:
    if len(text) > max_chars:
        head = text[: max_chars // 2]
        tail = text[-max_chars // 2 :]
        text = head + "\n\n... [truncated by character limit] ...\n\n" + tail
        truncated_chars = True
    else:
        truncated_chars = False

    lines = text.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        keep = max(2, max_lines // 2)
        text = "\n".join(
            lines[:keep]
            + [f"... [truncated {len(lines) - keep * 2} lines] ..."]
            + lines[-keep:]
        )
        return text, True
    return text, truncated_chars


def first_line(text: str, limit: int = 120) -> str:
    line = " ".join(text.strip().split())
    if len(line) <= limit:
        return line
    return line[: limit - 1] + "…"


def parse_json_maybe(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def find_session_files(root: Path = SESSION_ROOT) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def session_summary(path: Path) -> SessionSummary:
    session_id = None
    started_at = None
    originator = None
    cwd = None
    line_count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_count, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") == "session_meta":
                    payload = item.get("payload") or {}
                    session_id = payload.get("id")
                    started_at = payload.get("timestamp")
                    originator = payload.get("originator")
                    cwd = payload.get("cwd")
                    break
    except OSError:
        pass
    total_lines = count_lines(path)
    fallback_id = path.stem.replace("rollout-", "")
    short_id = (session_id or fallback_id)[:12]
    return SessionSummary(
        path=path,
        session_id=session_id,
        short_id=short_id,
        started_at=started_at,
        originator=originator,
        cwd=cwd,
        line_count=total_lines,
    )


def list_session_summaries(root: Path = SESSION_ROOT) -> list[SessionSummary]:
    return [session_summary(path) for path in find_session_files(root)]


def resolve_session_ids(ids: list[str], root: Path = SESSION_ROOT) -> list[Path]:
    summaries = list_session_summaries(root)
    resolved: list[Path] = []
    for requested in ids:
        needle = requested.strip()
        if not needle:
            continue
        matches = [
            summary
            for summary in summaries
            if (summary.session_id and summary.session_id.startswith(needle))
            or summary.short_id.startswith(needle)
            or summary.path.stem.startswith(needle)
            or str(summary.path).endswith(needle)
        ]
        if not matches:
            raise ValueError(f"No session matched ID '{requested}'. Run `codex-session-export list` to see available IDs.")
        if len(matches) > 1:
            choices = ", ".join(match.session_id or match.short_id for match in matches[:6])
            raise ValueError(f"Session ID '{requested}' is ambiguous. Matches: {choices}")
        resolved.append(matches[0].path)
    return resolved


def count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def extract_code_block_language(command: str | None) -> str:
    if not command:
        return "text"
    if re.search(r"\b(python|pytest|pip)\b", command):
        return "bash"
    if re.search(r"\b(npm|pnpm|yarn|node)\b", command):
        return "bash"
    return "bash"
