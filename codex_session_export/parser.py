from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import AssetRef, Event, Report, SessionMeta
from .redact import Redactor
from .utils import first_line, iter_jsonl, parse_json_maybe, sha256_file, truncate_middle


READ_COMMANDS = ("cat ", "sed ", "rg ", "grep ", "find ", "ls ", "wc ", "nl ", "head ", "tail ")
TEST_WORDS = ("test", "pytest", "unittest", "npm run build", "npm run test", "pnpm test", "yarn test")
EDIT_TOOLS = {"apply_patch"}


def parse_session(path: Path, redactor: Redactor, max_output_lines: int = 80) -> Report:
    source_sha = sha256_file(path)
    meta = SessionMeta(source_path=path, source_sha256=source_sha)
    events: list[Event] = []
    pending_calls: dict[str, Event] = {}
    warnings: list[str] = []
    last_message_signature: tuple[str, str, tuple[str, ...]] | None = None
    last_message_ts: str | None = None

    def add_event(event: Event) -> Event:
        event.text = redactor.redact(event.text)
        event.details = redactor.redact(event.details)
        event.command = redactor.redact(event.command)
        event.cwd = redactor.redact(event.cwd)
        event.files = [redactor.redact(item) for item in event.files]
        events.append(event)
        return event

    for line_number, item, raw_line in iter_jsonl(path):
        timestamp = item.get("timestamp")
        item_type = item.get("type")
        payload = item.get("payload") or {}

        if item_type == "parse_error":
            warnings.append(f"Line {line_number}: invalid JSON")
            continue

        if item_type == "session_meta":
            _apply_session_meta(meta, payload)
            continue

        if item_type == "response_item":
            response_type = payload.get("type")
            if response_type == "message":
                role = payload.get("role", "assistant")
                text, assets = _extract_content(payload.get("content"))
                signature = _message_signature(role, text, assets)
                if (text or assets) and not _is_duplicate_message(signature, timestamp, last_message_signature, last_message_ts):
                    last_message_signature = signature
                    last_message_ts = timestamp
                    is_internal = _looks_internal_context(text)
                    kind = "system_context" if is_internal else ("prompt" if role == "user" else "answer")
                    title = "上下文/系统信息" if is_internal else ("用户 Prompt" if role == "user" else "AI 回复")
                    add_event(
                        Event(
                            index=len(events) + 1,
                            timestamp=timestamp,
                            role="system" if is_internal else role,
                            kind=kind,
                            title=title,
                            text=text,
                            raw=payload,
                            assets=assets,
                            importance=1 if is_internal else (5 if role == "user" else 3),
                            collapsed=True if is_internal else (False if role == "user" else len(text) > 1200),
                        )
                    )
                continue

            if response_type == "function_call":
                event = _event_from_function_call(len(events) + 1, timestamp, payload, redactor)
                add_event(event)
                if event.call_id:
                    pending_calls[event.call_id] = event
                continue

            if response_type == "function_call_output":
                call_id = payload.get("call_id")
                output = str(payload.get("output", ""))
                truncated, was_truncated = truncate_middle(output, max_lines=max_output_lines)
                if call_id in pending_calls:
                    event = pending_calls[call_id]
                    event.details = redactor.redact(truncated)
                    event.exit_code = _parse_exit_code(output)
                    if was_truncated:
                        event.tags.add("truncated")
                    event.title = _title_with_result(event)
                    event.importance = max(event.importance, 4 if event.exit_code not in (None, 0) else event.importance)
                    if event.exit_code not in (None, 0):
                        event.tags.add("failed")
                    continue
                add_event(
                    Event(
                        index=len(events) + 1,
                        timestamp=timestamp,
                        role="tool",
                        kind="tool_result",
                        title="工具结果",
                        details=truncated,
                        raw=payload,
                        call_id=call_id,
                        exit_code=_parse_exit_code(output),
                        tags={"truncated"} if was_truncated else set(),
                        importance=2,
                    )
                )
                continue

            if response_type in {"reasoning", "web_search_call", "function_call"}:
                continue

        if item_type == "event_msg":
            msg_type = payload.get("type")
            if msg_type in {"user_message", "agent_message"}:
                role = "user" if msg_type == "user_message" else "assistant"
                text = payload.get("message", "")
                assets = _assets_from_payload(payload)
                signature = _message_signature(role, text, assets)
                if (text or assets) and not _is_duplicate_message(signature, timestamp, last_message_signature, last_message_ts):
                    last_message_signature = signature
                    last_message_ts = timestamp
                    is_internal = _looks_internal_context(text)
                    add_event(
                        Event(
                            index=len(events) + 1,
                            timestamp=timestamp,
                            role="system" if is_internal else role,
                            kind="system_context" if is_internal else ("prompt" if role == "user" else "answer"),
                            title="上下文/系统信息" if is_internal else ("用户 Prompt" if role == "user" else "AI 回复"),
                            text=text,
                            raw=payload,
                            assets=assets,
                            importance=1 if is_internal else (5 if role == "user" else 3),
                            collapsed=True if is_internal else (False if role == "user" else len(text) > 1200),
                        )
                    )
                continue

    stats = compute_stats(events)
    if not meta.started_at and events:
        meta.started_at = events[0].timestamp
    return Report(meta=meta, events=events, stats=stats, redaction_counts=dict(redactor.counts), parse_warnings=warnings)


def compute_stats(events: list[Event]) -> dict[str, int]:
    stats = {
        "events": len(events),
        "user_prompts": 0,
        "assistant_replies": 0,
        "tool_calls": 0,
        "terminal_commands": 0,
        "file_reads": 0,
        "file_edits": 0,
        "validation_runs": 0,
        "failed_commands": 0,
        "images": 0,
    }
    for event in events:
        if event.kind == "prompt":
            stats["user_prompts"] += 1
        if event.kind == "answer":
            stats["assistant_replies"] += 1
        if event.role == "tool":
            stats["tool_calls"] += 1
        if event.kind == "terminal_command":
            stats["terminal_commands"] += 1
        if "file_read" in event.tags:
            stats["file_reads"] += 1
        if event.kind == "file_edit" or "file_edit" in event.tags:
            stats["file_edits"] += 1
        if "validation" in event.tags:
            stats["validation_runs"] += 1
        if event.exit_code not in (None, 0):
            stats["failed_commands"] += 1
        stats["images"] += len(event.assets)
    return stats


def _apply_session_meta(meta: SessionMeta, payload: dict[str, Any]) -> None:
    meta.session_id = payload.get("id") or meta.session_id
    meta.started_at = payload.get("timestamp") or meta.started_at
    meta.cwd = payload.get("cwd") or meta.cwd
    meta.originator = payload.get("originator") or meta.originator


def _event_from_function_call(index: int, timestamp: str | None, payload: dict[str, Any], redactor: Redactor) -> Event:
    name = payload.get("name") or "tool"
    args = parse_json_maybe(payload.get("arguments", {}))
    call_id = payload.get("call_id")
    details = json.dumps(args, ensure_ascii=False, indent=2) if not isinstance(args, str) else args

    if name in EDIT_TOOLS:
        files = _extract_patch_files(details)
        return Event(
            index=index,
            timestamp=timestamp,
            role="tool",
            kind="file_edit",
            title="文件修改",
            text=_summarize_files(files) if files else "应用补丁修改文件",
            details=details,
            raw=payload,
            call_id=call_id,
            tool_name=name,
            files=files,
            tags={"file_edit"},
            importance=5,
            collapsed=True,
        )

    if name == "exec_command":
        command = args.get("cmd") if isinstance(args, dict) else None
        cwd = args.get("workdir") if isinstance(args, dict) else None
        tags = _command_tags(command or "")
        kind = "terminal_command"
        title = "终端命令"
        text = command or "运行终端命令"
        importance = 4 if "validation" in tags else 3
        if "file_read" in tags:
            importance = 2
        return Event(
            index=index,
            timestamp=timestamp,
            role="tool",
            kind=kind,
            title=title,
            text=text,
            details=details,
            raw=payload,
            call_id=call_id,
            tool_name=name,
            command=command,
            cwd=cwd,
            files=_extract_paths_from_command(command or ""),
            tags=tags,
            importance=importance,
            collapsed=True,
        )

    title = _friendly_tool_name(name)
    return Event(
        index=index,
        timestamp=timestamp,
        role="tool",
        kind="tool_call",
        title=title,
        text=first_line(details, 180),
        details=details,
        raw=payload,
        call_id=call_id,
        tool_name=name,
        importance=2,
        collapsed=True,
    )


def _extract_content(content: Any) -> tuple[str, list[AssetRef]]:
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    text_parts: list[str] = []
    assets: list[AssetRef] = []
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            text_parts.append(str(part.get("text", "")))
        elif part_type in {"input_image", "image"}:
            src = part.get("image_url") or part.get("path") or part.get("file")
            if src:
                assets.append(AssetRef(source=str(src), label=part.get("alt", "image")))
    return "\n".join(x for x in text_parts if x), assets


def _assets_from_payload(payload: dict[str, Any]) -> list[AssetRef]:
    assets: list[AssetRef] = []
    for key in ("images", "local_images"):
        for item in payload.get(key) or []:
            if isinstance(item, str):
                assets.append(AssetRef(source=item))
            elif isinstance(item, dict):
                src = item.get("path") or item.get("url") or item.get("source")
                if src:
                    assets.append(AssetRef(source=str(src), label=item.get("name", "image")))
    return assets


def _command_tags(command: str) -> set[str]:
    stripped = command.strip()
    lower = stripped.lower()
    tags: set[str] = set()
    if lower.startswith(READ_COMMANDS) or lower.startswith("git status") or lower.startswith("git diff"):
        tags.add("file_read")
    if any(word in lower for word in TEST_WORDS):
        tags.add("validation")
    if re.search(r"\b(build|lint|typecheck|check)\b", lower):
        tags.add("validation")
    if re.search(r"\b(git add|git commit|git push)\b", lower):
        tags.add("git")
    return tags


def _extract_paths_from_command(command: str) -> list[str]:
    paths: list[str] = []
    for token in re.findall(r"(?:[A-Za-z0-9_.@~/-]+/[A-Za-z0-9_.@~/-]+|[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|json|md|html|css|yml|yaml|toml))", command):
        if token not in paths:
            paths.append(token)
    return paths[:12]


def _extract_patch_files(patch: str) -> list[str]:
    files: list[str] = []
    for line in patch.splitlines():
        match = re.match(r"\*\*\* (?:Add|Update|Delete) File: (.+)$", line)
        if match and match.group(1) not in files:
            files.append(match.group(1))
    return files


def _parse_exit_code(output: str) -> int | None:
    match = re.search(r"Process exited with code (-?\d+)", output)
    if match:
        return int(match.group(1))
    match = re.search(r"exit code[:=]\s*(-?\d+)", output, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _friendly_tool_name(name: str) -> str:
    mapping = {
        "write_stdin": "终端交互",
        "web.run": "网页检索",
        "update_plan": "计划更新",
    }
    return mapping.get(name, f"工具调用：{name}")


def _message_signature(role: str, text: str, assets: list[AssetRef]) -> tuple[str, str, tuple[str, ...]]:
    normalized_text = re.sub(r"\s+", " ", text).strip()
    text_sig = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    asset_sig = tuple(hashlib.sha256(asset.source.encode("utf-8")).hexdigest() for asset in assets[:4])
    return role, text_sig, asset_sig


def _is_duplicate_message(
    current: tuple[str, str, tuple[str, ...]],
    timestamp: str | None,
    last: tuple[str, str, tuple[str, ...]] | None,
    last_timestamp: str | None,
) -> bool:
    if last is None or current != last:
        return False
    if timestamp is None or last_timestamp is None:
        return True
    try:
        current_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        last_dt = datetime.fromisoformat(last_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return True
    return abs((current_dt - last_dt).total_seconds()) <= 5


def _looks_internal_context(text: str) -> bool:
    stripped = text.lstrip()
    markers = (
        "# AGENTS.md instructions",
        "<environment_context>",
        "<skill>",
        "<permissions instructions>",
        "<app-context>",
        "<collaboration_mode>",
        "<plugins_instructions>",
        "<skills_instructions>",
    )
    return any(marker in stripped[:4000] for marker in markers)


def _title_with_result(event: Event) -> str:
    if event.exit_code is None:
        return event.title
    status = "成功" if event.exit_code == 0 else f"失败，退出码 {event.exit_code}"
    return f"{event.title} · {status}"


def _summarize_files(files: list[str]) -> str:
    if not files:
        return ""
    if len(files) == 1:
        return f"修改 {files[0]}"
    return "修改 " + "、".join(files[:3]) + (f" 等 {len(files)} 个文件" if len(files) > 3 else "")
