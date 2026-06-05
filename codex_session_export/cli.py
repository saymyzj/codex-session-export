from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .gui import run_gui
from .parser import parse_session
from .redact import Redactor
from .render import write_batch_index, write_report
from .utils import SESSION_ROOT, find_session_files, list_session_summaries, resolve_session_ids, session_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-session-export")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="列出本地 Codex JSONL 会话。")
    list_parser.add_argument("--root", type=Path, default=SESSION_ROOT)
    list_parser.add_argument("--limit", type=int, default=20)

    gui_parser = sub.add_parser("gui", help="启动本地 GUI 控制台，用于扫描、选择和导出会话。")
    gui_parser.add_argument("--root", type=Path, default=SESSION_ROOT)
    gui_parser.add_argument("--out", type=Path, default=Path.cwd() / "exports")
    gui_parser.add_argument("--host", default="127.0.0.1")
    gui_parser.add_argument("--port", type=int, default=8765)
    gui_parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器。")

    export_parser = sub.add_parser("export", help="导出一个或多个会话为 HTML/Markdown 报告。")
    export_parser.add_argument("session", type=Path, nargs="?", help="直接指定 rollout-*.jsonl 路径")
    export_parser.add_argument("--latest", action="store_true", help="导出最新会话")
    export_parser.add_argument("--id", dest="ids", action="append", default=[], help="按会话 ID 或短 ID 选择，可重复传入")
    export_parser.add_argument("--ids-file", type=Path, default=None, help="从文本文件读取会话 ID，每行一个")
    export_parser.add_argument("--all", action="store_true", help="导出扫描范围内的全部会话")
    export_parser.add_argument("--limit", type=int, default=None, help="与 --all 连用，仅导出最新 N 个会话")
    export_parser.add_argument("--root", type=Path, default=SESSION_ROOT)
    export_parser.add_argument("--out", type=Path, default=None)
    export_parser.add_argument("--course", default=None)
    export_parser.add_argument("--assignment", default=None)
    export_parser.add_argument("--review-note", default=None)
    export_parser.add_argument("--redaction", choices=["none", "basic", "strict"], default="basic")
    export_parser.add_argument("--max-output-lines", type=int, default=80)

    args = parser.parse_args(argv)
    if args.command == "list":
        return _list(args.root, args.limit)
    if args.command == "gui":
        run_gui(host=args.host, port=args.port, open_browser=not args.no_browser, root=args.root, out=args.out)
        return 0
    if args.command == "export":
        return _export(args)
    return 2


def _list(root: Path, limit: int) -> int:
    sessions = list_session_summaries(root)
    if not sessions:
        print(f"未在 {root} 找到会话。", file=sys.stderr)
        return 1
    for idx, summary in enumerate(sessions[:limit], 1):
        started = summary.started_at or "unknown"
        sid = summary.session_id or summary.short_id
        print(f"{idx:>2}. {summary.short_id}  {started}  {summary.originator or 'Codex'}  {summary.line_count} lines")
        print(f"    id: {sid}")
        print(f"    path: {summary.path}")
        if summary.cwd:
            print(f"    cwd: {summary.cwd}")
    return 0


def _export(args: argparse.Namespace) -> int:
    sessions = _resolve_sessions(args)
    if not sessions:
        print("未选择任何会话。可以传路径、`--latest`、`--id`、`--ids-file` 或 `--all`。", file=sys.stderr)
        return 2
    reports = []

    if len(sessions) == 1:
        session = sessions[0]
        out_dir = args.out or Path("exports") / session.stem
        redactor = Redactor(mode=args.redaction, workspace=Path.cwd())
        report = parse_session(session, redactor=redactor, max_output_lines=args.max_output_lines)
        if args.assignment:
            report.meta.title = args.assignment
        write_report(
            report,
            out_dir,
            redactor=redactor,
            course=args.course,
            assignment=args.assignment,
            review_note=args.review_note,
        )
        print(f"Wrote {out_dir / 'report.html'}")
        print(f"Wrote {out_dir / 'report.md'}")
        print(f"Wrote {out_dir / 'raw' / 'manifest.json'}")
        return 0

    batch_root = args.out or Path("exports") / "batch-export"
    batch_root.mkdir(parents=True, exist_ok=True)
    for session in sessions:
        redactor = Redactor(mode=args.redaction, workspace=Path.cwd())
        report = parse_session(session, redactor=redactor, max_output_lines=args.max_output_lines)
        if args.assignment:
            report.meta.title = args.assignment
        reports.append(report)
        child_dir = batch_root / _session_dirname(session)
        write_report(
            report,
            child_dir,
            redactor=redactor,
            course=args.course,
            assignment=args.assignment,
            review_note=args.review_note,
        )
    write_batch_index(reports, batch_root, course=args.course, assignment=args.assignment)
    print(f"Wrote batch index {batch_root / 'index.html'}")
    print(f"Exported {len(reports)} sessions into {batch_root}")
    return 0


def _resolve_sessions(args: argparse.Namespace) -> list[Path]:
    if args.session is not None:
        return [args.session.expanduser()]

    ids = list(args.ids or [])
    if args.ids_file:
        ids.extend(
            line.strip()
            for line in args.ids_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    if ids:
        return resolve_session_ids(ids, root=args.root)

    if args.latest:
        sessions = find_session_files(args.root)
        return [sessions[0]] if sessions else []

    if args.all:
        sessions = find_session_files(args.root)
        return sessions[: args.limit] if args.limit else sessions

    return []


def _session_dirname(session: Path) -> str:
    summary = session_summary(session)
    raw = summary.session_id or session.stem
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw)
    return safe[:48] or session.stem


if __name__ == "__main__":
    raise SystemExit(main())
