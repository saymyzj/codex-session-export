from __future__ import annotations

import base64
import binascii
import html
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .models import AssetRef, Event, Report
from .redact import Redactor
from .utils import extract_code_block_language


def write_report(
    report: Report,
    out_dir: Path,
    *,
    redactor: Redactor,
    course: str | None = None,
    assignment: str | None = None,
    review_note: str | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "assets").mkdir(exist_ok=True)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(exist_ok=True)

    _copy_assets(report.events, out_dir / "assets", redactor)
    (out_dir / "report.html").write_text(
        render_html(report, course=course, assignment=assignment, review_note=review_note),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(
        render_markdown(report, course=course, assignment=assignment, review_note=review_note),
        encoding="utf-8",
    )
    _write_redacted_jsonl(report.meta.source_path, raw_dir / "session.redacted.jsonl", redactor)
    manifest = _manifest(report, course=course, assignment=assignment)
    (raw_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def write_batch_index(reports: list[Report], out_dir: Path, *, course: str | None = None, assignment: str | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    rows = []
    total_stats: dict[str, int] = {}
    for report in reports:
        for key, value in report.stats.items():
            total_stats[key] = total_stats.get(key, 0) + value
        folder = _safe_report_dir_name(report)
        rows.append(
            f"""
            <tr>
              <td><code>{html.escape(report.meta.session_id or report.meta.source_path.stem)}</code></td>
              <td>{html.escape(report.meta.started_at or "")}</td>
              <td>{html.escape(report.meta.originator or "Codex")}</td>
              <td><code>{html.escape(report.meta.cwd or "")}</code></td>
              <td><a href="{html.escape(folder)}/report.html">打开报告</a></td>
            </tr>
            """
        )
    stats_html = "\n".join(
        f"<div><span>{html.escape(_stat_label(key))}</span><strong>{value}</strong></div>"
        for key, value in total_stats.items()
    )
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(assignment or "Codex 会话批量导出")}</title>
  <style>{_css()} table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}}a{{color:var(--accent);font-weight:700}}</style>
</head>
<body>
  <header class="top">
    <div>
      <p class="eyebrow">Codex Session Export</p>
      <h1>{html.escape(assignment or "Codex 会话批量导出")}</h1>
      <p class="sub">本页汇总多个 Codex 会话导出结果。每个子报告都保留独立的 HTML、Markdown、redacted JSONL 与 manifest。</p>
    </div>
    <div class="meta-grid">
      <div><span>课程</span><strong>{html.escape(course or "未填写")}</strong></div>
      <div><span>会话数</span><strong>{len(reports)}</strong></div>
      <div class="wide"><span>生成时间</span><strong>{html.escape(generated)}</strong></div>
    </div>
  </header>
  <main>
    <section class="panel">
      <h2>批量摘要</h2>
      <div class="stats">{stats_html}</div>
    </section>
    <section class="panel">
      <h2>会话列表</h2>
      <table>
        <thead><tr><th>会话 ID</th><th>开始时间</th><th>来源</th><th>工作目录</th><th>报告</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
  </main>
</body>
</html>
"""
    (out_dir / "index.html").write_text(html_text, encoding="utf-8")


def render_html(report: Report, *, course: str | None, assignment: str | None, review_note: str | None) -> str:
    title = assignment or report.meta.title
    summary_items = _summary_items(report)
    events_html = "\n".join(_render_event_html(event) for event in report.events)
    redactions = ", ".join(f"{k}: {v}" for k, v in sorted(report.redaction_counts.items())) or "无"
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{_css()}</style>
</head>
<body>
  <header class="top">
    <div>
      <p class="eyebrow">Codex Session Export</p>
      <h1>{html.escape(title)}</h1>
      <p class="sub">本页面由本地 Codex 会话 JSONL 生成，不是 Codex 官方页面截图。内容经过脱敏，完整证据保留在折叠详情和 raw 文件中。</p>
    </div>
    <div class="meta-grid">
      <div><span>课程</span><strong>{html.escape(course or "未填写")}</strong></div>
      <div><span>作业</span><strong>{html.escape(assignment or "未填写")}</strong></div>
      <div><span>工具</span><strong>{html.escape(report.meta.originator or "Codex")}</strong></div>
      <div><span>生成时间</span><strong>{html.escape(generated)}</strong></div>
      <div class="wide"><span>原始日志 SHA256</span><code>{html.escape(report.meta.source_sha256)}</code></div>
    </div>
  </header>

  <main>
    <section class="toolbar" aria-label="视图筛选">
      <button data-filter="default" class="active">默认视图</button>
      <button data-filter="all">完整视图</button>
      <button data-filter="prompt">提示词</button>
      <button data-filter="command">命令记录</button>
      <button data-filter="validation">验证记录</button>
      <button data-filter="edit">文件修改</button>
    </section>

    <section class="panel">
      <h2>一页式摘要</h2>
      <div class="stats">{summary_items}</div>
      <p class="note">{html.escape(review_note or "人工审核说明未填写。建议导出前补充：最终内容是否由本人检查、运行结果是否由本人确认、AI 建议中哪些被采纳或修改。")}</p>
    </section>

    <section class="panel">
      <h2>完整交互时间线</h2>
      <div class="timeline">{events_html}</div>
    </section>

    <section class="panel">
      <h2>原始日志校验信息</h2>
      <dl class="audit">
        <dt>原始文件</dt><dd><code>{html.escape(str(report.meta.source_path))}</code></dd>
        <dt>会话 ID</dt><dd>{html.escape(report.meta.session_id or "未知")}</dd>
        <dt>工作目录</dt><dd><code>{html.escape(report.meta.cwd or "未知")}</code></dd>
        <dt>脱敏记录</dt><dd>{html.escape(redactions)}</dd>
        <dt>说明</dt><dd>完整事件保留在 HTML 折叠区；同时生成 redacted JSONL 与 manifest 供追溯。</dd>
      </dl>
    </section>
  </main>
  <script>{_js()}</script>
</body>
</html>
"""


def render_markdown(report: Report, *, course: str | None, assignment: str | None, review_note: str | None) -> str:
    lines = [
        f"# {assignment or report.meta.title}",
        "",
        f"- Course: {course or 'N/A'}",
        f"- Assignment: {assignment or 'N/A'}",
        f"- Tool: {report.meta.originator or 'Codex'}",
        f"- Source SHA256: `{report.meta.source_sha256}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in report.stats.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Human Review", "", review_note or "N/A", "", "## Timeline", ""])
    for event in report.events:
        lines.extend(_render_event_markdown(event))
    return "\n".join(lines).rstrip() + "\n"


def _render_event_html(event: Event) -> str:
    classes = " ".join(["event", event.role, event.kind] + sorted(event.tags))
    data = _data_filters(event)
    time = html.escape(_short_time(event.timestamp))
    title = html.escape(event.title)
    text = _format_text(event.text)
    details = _format_pre(event.details)
    command = ""
    if event.command:
        command = f"<pre class=\"cmd\"><code>{html.escape(event.command)}</code></pre>"
    result = ""
    if event.exit_code is not None:
        label = "成功" if event.exit_code == 0 else f"失败 · exit {event.exit_code}"
        result = f"<span class=\"result {'ok' if event.exit_code == 0 else 'fail'}\">{html.escape(label)}</span>"
    assets = "".join(_asset_html(asset) for asset in event.assets)
    detail_block = ""
    if event.details:
        detail_block = f"<details {'open' if not event.collapsed else ''}><summary>查看原始详情</summary>{details}</details>"
    file_list = ""
    if event.files:
        file_list = "<ul class=\"files\">" + "".join(f"<li><code>{html.escape(path)}</code></li>" for path in event.files) + "</ul>"
    return f"""
<article class="{classes}" data-view="{data}">
  <div class="avatar">{html.escape(_avatar(event))}</div>
  <div class="bubble">
    <div class="event-head"><span>{title}</span><time>{time}</time>{result}</div>
    {command}
    <div class="text">{text}</div>
    {file_list}
    {assets}
    {detail_block}
  </div>
</article>
"""


def _render_event_markdown(event: Event) -> list[str]:
    lines = [f"### #{event.index} {event.title}", ""]
    if event.timestamp:
        lines.append(f"Time: `{event.timestamp}`")
        lines.append("")
    if event.command:
        lang = extract_code_block_language(event.command)
        lines.extend([f"```{lang}", event.command, "```", ""])
    if event.text:
        lines.extend([event.text, ""])
    if event.files:
        lines.append("Files:")
        for path in event.files:
            lines.append(f"- `{path}`")
        lines.append("")
    for asset in event.assets:
        if asset.exported_name:
            lines.append(f"![{asset.label}](assets/{asset.exported_name})")
    if event.details:
        lines.extend(["<details>", "<summary>Raw details</summary>", "", "```text", event.details, "```", "", "</details>", ""])
    return lines


def _summary_items(report: Report) -> str:
    return "\n".join(
        f"<div><span>{html.escape(_stat_label(key))}</span><strong>{value}</strong></div>"
        for key, value in report.stats.items()
    )


def _copy_assets(events: list[Event], assets_dir: Path, redactor: Redactor) -> None:
    counter = 1
    for event in events:
        for asset in event.assets:
            if asset.source.startswith("data:"):
                exported = _copy_data_uri(asset.source, assets_dir, counter)
                asset.source = "[DATA_URI_IMAGE]"
                if exported:
                    asset.exported_name = exported
                    counter += 1
                continue
            source = redactor.redact(asset.source)
            path = Path(asset.source).expanduser()
            if not path.exists() or not path.is_file():
                continue
            suffix = path.suffix.lower() or ".bin"
            name = f"asset-{counter:03d}{suffix}"
            shutil.copy2(path, assets_dir / name)
            asset.exported_name = name
            asset.source = source
            counter += 1


def _copy_data_uri(source: str, assets_dir: Path, counter: int) -> str | None:
    match = re.match(r"^data:(?P<mime>image/[A-Za-z0-9.+-]+);base64,(?P<data>.*)$", source, flags=re.DOTALL)
    if not match:
        return None
    mime = match.group("mime").lower()
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
    }.get(mime, ".bin")
    try:
        data = base64.b64decode(match.group("data"), validate=False)
    except (binascii.Error, ValueError):
        return None
    if not data:
        return None
    name = f"asset-{counter:03d}{suffix}"
    (assets_dir / name).write_bytes(data)
    return name


def _write_redacted_jsonl(source: Path, target: Path, redactor: Redactor) -> None:
    with source.open("r", encoding="utf-8") as src, target.open("w", encoding="utf-8") as dst:
        for line in src:
            dst.write(redactor.redact(line.rstrip("\n")) + "\n")


def _manifest(report: Report, *, course: str | None, assignment: str | None) -> dict[str, object]:
    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "tool_version": "0.1.0",
        "course": course,
        "assignment": assignment,
        "source": str(report.meta.source_path),
        "source_sha256": report.meta.source_sha256,
        "session_id": report.meta.session_id,
        "originator": report.meta.originator,
        "cwd": report.meta.cwd,
        "stats": report.stats,
        "redactions": report.redaction_counts,
        "warnings": report.parse_warnings,
    }


def _format_text(text: str) -> str:
    if not text:
        return ""
    escaped = html.escape(text)
    paragraphs = escaped.split("\n\n")
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs if p.strip())


def _format_pre(text: str) -> str:
    return f"<pre><code>{html.escape(text)}</code></pre>"


def _asset_html(asset: AssetRef) -> str:
    if asset.exported_name:
        return f'<figure><img src="assets/{html.escape(asset.exported_name)}" alt="{html.escape(asset.label)}"><figcaption>{html.escape(asset.label)}</figcaption></figure>'
    return f'<p class="asset-missing">图片引用：<code>{html.escape(asset.source)}</code></p>'


def _short_time(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    return timestamp.replace("T", " ").replace("Z", "")[:19]


def _avatar(event: Event) -> str:
    if event.role == "user":
        return "U"
    if event.role == "assistant":
        return "A"
    return "T"


def _data_filters(event: Event) -> str:
    filters = ["all"]
    if event.importance >= 3 or event.kind in {"prompt", "file_edit", "terminal_command"}:
        filters.append("default")
    if event.kind == "prompt":
        filters.append("prompt")
    if event.kind == "terminal_command":
        filters.append("command")
    if "validation" in event.tags:
        filters.append("validation")
    if event.kind == "file_edit" or "file_edit" in event.tags:
        filters.append("edit")
    return " ".join(filters)


def _css() -> str:
    return """
:root { color-scheme: light; --bg:#f7f7f4; --ink:#202124; --muted:#64645f; --line:#deded7; --panel:#ffffff; --user:#e8f1ff; --assistant:#ffffff; --tool:#f2efe7; --accent:#276b5d; --fail:#a43b32; --ok:#2f7d4f; }
* { box-sizing: border-box; }
body { margin:0; font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--ink); }
.top { display:grid; grid-template-columns: 1.2fr .8fr; gap:24px; padding:32px clamp(18px,4vw,56px); border-bottom:1px solid var(--line); background:#fbfbf8; }
.eyebrow { color:var(--accent); font-weight:700; margin:0 0 8px; }
h1 { margin:0; font-size:clamp(28px,4vw,44px); letter-spacing:0; }
h2 { margin:0 0 18px; font-size:22px; }
.sub { color:var(--muted); max-width:760px; }
.meta-grid, .stats { display:grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap:10px; }
.meta-grid div, .stats div { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
.meta-grid .wide { grid-column:1 / -1; }
span { color:var(--muted); }
strong { display:block; font-size:18px; margin-top:2px; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
main { max-width:1120px; margin:0 auto; padding:22px clamp(14px,3vw,28px) 56px; }
.toolbar { position:sticky; top:0; z-index:2; display:flex; flex-wrap:wrap; gap:8px; padding:12px 0; background:var(--bg); }
button { border:1px solid var(--line); background:#fff; color:var(--ink); border-radius:8px; padding:8px 12px; cursor:pointer; }
button.active { background:var(--accent); color:white; border-color:var(--accent); }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:22px; margin:16px 0; }
.note { background:#f6f1df; border-left:4px solid #b89134; padding:12px 14px; border-radius:6px; }
.timeline { display:flex; flex-direction:column; gap:14px; }
.event { display:grid; grid-template-columns:42px minmax(0,1fr); gap:12px; }
.event[hidden] { display:none; }
.avatar { width:34px; height:34px; border-radius:50%; display:grid; place-items:center; font-weight:800; color:white; background:var(--accent); }
.assistant .avatar { background:#5c6570; }
.tool .avatar { background:#9a7a37; }
.bubble { border:1px solid var(--line); border-radius:8px; background:var(--assistant); padding:14px; min-width:0; }
.user .bubble { background:var(--user); }
.tool .bubble { background:var(--tool); }
.event-head { display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:8px; font-weight:700; }
time { color:var(--muted); font-size:13px; font-weight:400; margin-left:auto; }
.result { border-radius:999px; padding:2px 8px; font-size:12px; font-weight:700; }
.result.ok { background:#dff1e6; color:var(--ok); }
.result.fail { background:#f6dedb; color:var(--fail); }
p { margin:0 0 10px; }
pre { overflow:auto; background:#202124; color:#f4f4f4; border-radius:8px; padding:12px; max-height:520px; }
.cmd { background:#111; }
details { margin-top:10px; }
summary { cursor:pointer; color:var(--accent); font-weight:700; }
.files { margin:8px 0; padding-left:22px; }
figure { margin:12px 0 0; }
img { max-width:100%; border:1px solid var(--line); border-radius:8px; }
figcaption, .asset-missing { color:var(--muted); font-size:13px; }
.audit { display:grid; grid-template-columns:160px minmax(0,1fr); gap:8px 14px; }
.audit dt { color:var(--muted); }
.audit dd { margin:0; min-width:0; overflow-wrap:anywhere; }
@media (max-width: 760px) { .top { grid-template-columns:1fr; } .meta-grid,.stats { grid-template-columns:1fr; } .event { grid-template-columns:1fr; } .avatar { display:none; } .audit { grid-template-columns:1fr; } }
"""


def _js() -> str:
    return """
const buttons = document.querySelectorAll('[data-filter]');
const events = document.querySelectorAll('.event');
function setFilter(filter) {
  buttons.forEach(btn => btn.classList.toggle('active', btn.dataset.filter === filter));
  events.forEach(event => {
    const views = (event.dataset.view || '').split(/\\s+/);
    event.hidden = !views.includes(filter);
  });
}
buttons.forEach(btn => btn.addEventListener('click', () => setFilter(btn.dataset.filter)));
setFilter('default');
"""


def _safe_report_dir_name(report: Report) -> str:
    base = report.meta.session_id or report.meta.source_path.stem
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in base)
    return safe[:48] or "session"


def _stat_label(key: str) -> str:
    labels = {
        "events": "总事件",
        "user_prompts": "用户提示词",
        "assistant_replies": "AI 回复",
        "tool_calls": "工具调用",
        "terminal_commands": "终端命令",
        "file_reads": "文件读取",
        "file_edits": "文件修改",
        "validation_runs": "验证运行",
        "failed_commands": "失败命令",
        "images": "图片",
    }
    return labels.get(key, key)
