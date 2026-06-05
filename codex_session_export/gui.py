from __future__ import annotations

import html
import json
import mimetypes
import secrets
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .parser import parse_session
from .redact import Redactor
from .render import write_batch_index, write_report
from .utils import SESSION_ROOT, list_session_summaries, resolve_session_ids, session_summary


@dataclass
class GuiState:
    token: str
    default_root: Path
    default_out: Path
    exports: dict[str, Path] = field(default_factory=dict)


def run_gui(*, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True, root: Path = SESSION_ROOT, out: Path | None = None) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("GUI 只能监听 127.0.0.1 或 localhost。")
    default_out = out or Path.cwd() / "exports"
    state = GuiState(token=secrets.token_urlsafe(24), default_root=root.expanduser(), default_out=default_out.expanduser())
    server = _make_server(host, port, state)
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}/?token={state.token}"
    print(f"Codex Session Export GUI: {url}")
    print("按 Ctrl+C 退出。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGUI 已关闭。")
    finally:
        server.server_close()


def _make_server(host: str, port: int, state: GuiState) -> ThreadingHTTPServer:
    last_error: OSError | None = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), GuiHandler)
            server.state = state  # type: ignore[attr-defined]
            return server
        except OSError as exc:
            last_error = exc
    raise OSError(f"无法启动 GUI，端口 {port}-{port + 19} 都不可用。") from last_error


class GuiHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def state(self) -> GuiState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(_html(self.state.token, self.state.default_root, self.state.default_out))
            return
        if parsed.path == "/api/sessions":
            if not self._check_token(parsed):
                return
            self._handle_sessions(parsed)
            return
        if parsed.path.startswith("/browse/"):
            self._serve_export_file(parsed.path, head_only=False)
            return
        self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/browse/"):
            self._serve_export_file(parsed.path, head_only=True)
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/export":
            self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        if not self._check_token(parsed):
            return
        self._handle_export()

    def _check_token(self, parsed: Any) -> bool:
        query = parse_qs(parsed.query)
        token = query.get("token", [""])[0]
        if token != self.state.token:
            self._send_json({"error": "invalid_token"}, status=HTTPStatus.FORBIDDEN)
            return False
        return True

    def _handle_sessions(self, parsed: Any) -> None:
        query = parse_qs(parsed.query)
        try:
            root = _validate_scan_root(query.get("root", [""])[0] or str(self.state.default_root))
            limit = _bounded_int(query.get("limit", ["200"])[0], default=200, minimum=1, maximum=1000)
            summaries = list_session_summaries(root)[:limit]
            sessions = [_session_payload(summary.path) for summary in summaries]
            self._send_json({"root": str(root), "count": len(sessions), "sessions": sessions})
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_export(self) -> None:
        try:
            body = self._read_json()
            root = _validate_scan_root(body.get("root") or str(self.state.default_root))
            ids = [str(item).strip() for item in body.get("ids", []) if str(item).strip()]
            if not ids:
                raise ValueError("请先选择至少一个会话。")
            out_dir = _validate_output_dir(body.get("out") or _default_export_dir(self.state.default_out))
            redaction = body.get("redaction") or "basic"
            if redaction not in {"none", "basic", "strict"}:
                raise ValueError("脱敏模式必须是 none、basic 或 strict。")
            max_output_lines = _bounded_int(body.get("max_output_lines", 80), default=80, minimum=10, maximum=1000)
            selected = resolve_session_ids(ids, root=root)
            result = _write_export(
                selected,
                out_dir,
                redaction=redaction,
                max_output_lines=max_output_lines,
                course=(body.get("course") or None),
                assignment=(body.get("assignment") or None),
                review_note=(body.get("review_note") or None),
            )
            export_id = secrets.token_urlsafe(16)
            self.state.exports[export_id] = out_dir
            entry = "index.html" if result["kind"] == "batch" else "report.html"
            result["view_url"] = f"/browse/{export_id}/{entry}"
            self._send_json(result)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _serve_export_file(self, request_path: str, *, head_only: bool) -> None:
        parts = request_path.split("/", 3)
        if len(parts) < 4:
            self._send_json({"error": "missing_export_file"}, status=HTTPStatus.NOT_FOUND)
            return
        export_id = parts[2]
        rel = unquote(parts[3])
        root = self.state.exports.get(export_id)
        if root is None:
            self._send_json({"error": "unknown_export"}, status=HTTPStatus.NOT_FOUND)
            return
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            self._send_json({"error": "invalid_path"}, status=HTTPStatus.FORBIDDEN)
            return
        if not target.exists() or not target.is_file():
            self._send_json({"error": "file_not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        if not raw:
            return {}
        return json.loads(raw)

    def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _write_export(
    sessions: list[Path],
    out_dir: Path,
    *,
    redaction: str,
    max_output_lines: int,
    course: str | None,
    assignment: str | None,
    review_note: str | None,
) -> dict[str, Any]:
    if len(sessions) == 1:
        session = sessions[0]
        redactor = Redactor(mode=redaction, workspace=Path.cwd())
        report = parse_session(session, redactor=redactor, max_output_lines=max_output_lines)
        if assignment:
            report.meta.title = assignment
        write_report(report, out_dir, redactor=redactor, course=course, assignment=assignment, review_note=review_note)
        return {
            "kind": "single",
            "count": 1,
            "out": str(out_dir),
            "files": ["report.html", "report.md", "raw/manifest.json", "raw/session.redacted.jsonl"],
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    files = ["index.html"]
    for session in sessions:
        redactor = Redactor(mode=redaction, workspace=Path.cwd())
        report = parse_session(session, redactor=redactor, max_output_lines=max_output_lines)
        if assignment:
            report.meta.title = assignment
        reports.append(report)
        child = out_dir / _report_dir_name(report.meta.session_id or session_summary(session).short_id)
        write_report(report, child, redactor=redactor, course=course, assignment=assignment, review_note=review_note)
        files.append(f"{child.name}/report.html")
    write_batch_index(reports, out_dir, course=course, assignment=assignment)
    return {"kind": "batch", "count": len(sessions), "out": str(out_dir), "files": files}


def _session_payload(path: Path) -> dict[str, Any]:
    summary = session_summary(path)
    prompt = _first_prompt(path)
    stat = path.stat()
    return {
        "id": summary.session_id or summary.short_id,
        "short_id": summary.short_id,
        "started_at": summary.started_at or "",
        "originator": summary.originator or "Codex",
        "cwd": summary.cwd or "",
        "path": str(path),
        "line_count": summary.line_count,
        "size": stat.st_size,
        "size_label": _format_bytes(stat.st_size),
        "first_prompt": prompt,
        "mtime": stat.st_mtime,
    }


def _first_prompt(path: Path, max_lines: int = 1500) -> str:
    markers = (
        "# AGENTS.md instructions",
        "<environment_context>",
        "<permissions instructions>",
        "<app-context>",
        "<skills_instructions>",
        "<plugins_instructions>",
        "<collaboration_mode>",
    )
    try:
        with path.open("r", encoding="utf-8") as handle:
            for idx, line in enumerate(handle, 1):
                if idx > max_lines:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload") or {}
                if payload.get("type") != "message" or payload.get("role") != "user":
                    continue
                text = _message_text(payload)
                if not text or text.strip().startswith(markers):
                    continue
                return _one_line(text, 160)
    except OSError:
        return ""
    return ""


def _message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("input_text") or item.get("output_text")
                if text:
                    parts.append(str(text))
    return "\n".join(parts)


def _one_line(text: str, limit: int) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _validate_scan_root(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    home = Path.home().resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"扫描目录不存在：{path}")
    if path == Path(path.anchor) or path == home:
        raise ValueError("扫描目录不能是根目录或整个 Home 目录。")
    return path


def _validate_output_dir(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    home = Path.home().resolve()
    codex = (home / ".codex").resolve()
    if path == Path(path.anchor) or path == home:
        raise ValueError("输出目录不能是根目录或整个 Home 目录。")
    try:
        path.relative_to(codex)
        raise ValueError("输出目录不能写入 ~/.codex。")
    except ValueError as exc:
        if "不能写入" in str(exc):
            raise
    return path


def _default_export_dir(base: Path) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(base / f"gui-{stamp}")


def _report_dir_name(raw: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw)
    return safe[:48] or "session"


def _html(token: str, root: Path, out: Path) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Session Export 控制台</title>
  <style>{_css()}</style>
</head>
<body>
  <div class="app">
    <aside class="side">
      <div class="brand">
        <p>Codex Session Export</p>
        <h1>导出控制台</h1>
      </div>
      <section>
        <h2>扫描</h2>
        <label>会话目录
          <input id="root" value="{html.escape(str(root))}">
        </label>
        <label>读取上限
          <input id="limit" type="number" min="1" max="1000" value="200">
        </label>
        <button id="scanBtn" class="primary">扫描会话</button>
      </section>
      <section>
        <h2>导出信息</h2>
        <label>课程名称
          <input id="course" placeholder="例如：软件工程">
        </label>
        <label>作业/项目名称
          <input id="assignment" placeholder="例如：实验二 AI 辅助记录">
        </label>
        <label>人工审核说明
          <textarea id="review" rows="5" placeholder="说明最终内容如何由本人检查、修改和验证"></textarea>
        </label>
        <label>脱敏模式
          <select id="redaction">
            <option value="basic" selected>普通脱敏</option>
            <option value="strict">严格脱敏</option>
            <option value="none">不脱敏，仅本地归档</option>
          </select>
        </label>
        <label>最大输出行数
          <input id="maxOutput" type="number" min="10" max="1000" value="80">
        </label>
        <label>输出目录
          <input id="out" value="{html.escape(str(out / 'gui-export'))}">
        </label>
        <button id="exportBtn" class="primary">导出所选会话</button>
      </section>
    </aside>
    <main class="main">
      <div class="topbar">
        <div>
          <h2>选择要导出的会话</h2>
          <p id="summary">尚未扫描。默认读取 ~/.codex/sessions。</p>
        </div>
        <div class="actions">
          <input id="search" placeholder="搜索 ID、路径、首条 Prompt、工作目录">
          <button id="selectVisible">选择当前结果</button>
          <button id="clearSelected">清空选择</button>
          <button id="recentFive">最近 5 个</button>
        </div>
      </div>
      <div class="status" id="status">准备就绪。</div>
      <div class="tableWrap">
        <table>
          <thead>
            <tr>
              <th class="check"></th>
              <th>时间</th>
              <th>会话 ID</th>
              <th>首条 Prompt</th>
              <th>工作目录</th>
              <th>规模</th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
      <section class="result" id="result" hidden></section>
    </main>
  </div>
  <script>
    const TOKEN = {json.dumps(token)};
    {_js()}
  </script>
</body>
</html>"""


def _css() -> str:
    return """
:root {
  --bg: #f6f4ef;
  --ink: #202124;
  --muted: #6c6b66;
  --line: #d9d4ca;
  --panel: #fffdf8;
  --accent: #215c5c;
  --accent-dark: #163f40;
  --warn: #8a4b15;
  --ok: #24613b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background: var(--bg);
  font-family: ui-serif, Georgia, "Songti SC", "Noto Serif CJK SC", serif;
}
button, input, textarea, select { font: inherit; }
.app { display: grid; grid-template-columns: minmax(320px, 380px) 1fr; min-height: 100vh; }
.side {
  border-right: 1px solid var(--line);
  background: #ede8dc;
  padding: 22px;
  overflow: auto;
}
.brand { border-bottom: 1px solid var(--line); padding-bottom: 18px; margin-bottom: 18px; }
.brand p { margin: 0 0 6px; color: var(--accent); font-weight: 700; letter-spacing: .04em; text-transform: uppercase; font-size: 12px; }
.brand h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
section { margin: 0 0 22px; }
section h2, .topbar h2 { margin: 0 0 12px; font-size: 18px; }
label { display: grid; gap: 6px; margin: 0 0 12px; color: var(--muted); font-size: 13px; }
input, textarea, select {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 10px 11px;
  background: var(--panel);
  color: var(--ink);
}
textarea { resize: vertical; line-height: 1.5; }
button {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
  color: var(--ink);
  padding: 9px 12px;
  cursor: pointer;
}
button:hover { border-color: var(--accent); color: var(--accent-dark); }
button.primary { width: 100%; background: var(--accent); border-color: var(--accent); color: white; font-weight: 700; }
button.primary:hover { background: var(--accent-dark); color: white; }
.main { padding: 22px; min-width: 0; }
.topbar {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(420px, .9fr);
  gap: 18px;
  align-items: end;
  margin-bottom: 14px;
}
.topbar p { margin: 0; color: var(--muted); line-height: 1.5; }
.actions { display: grid; grid-template-columns: 1fr auto auto auto; gap: 8px; align-items: center; }
.status {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
  padding: 10px 12px;
  color: var(--muted);
  margin-bottom: 14px;
}
.status.error { color: #8b1a1a; border-color: #d8b1a8; background: #fff7f5; }
.status.ok { color: var(--ok); border-color: #b8d4bf; background: #f4fbf5; }
.tableWrap {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  overflow: auto;
  max-height: calc(100vh - 190px);
}
table { width: 100%; border-collapse: collapse; min-width: 980px; }
th, td { border-bottom: 1px solid var(--line); padding: 10px; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: #f7f2e8; z-index: 1; font-size: 12px; color: var(--muted); }
td { font-size: 13px; line-height: 1.45; }
.check { width: 42px; }
.id { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
.muted { color: var(--muted); }
.prompt { max-width: 420px; }
.cwd { max-width: 280px; overflow-wrap: anywhere; }
tr.selected { background: #eef5f3; }
.result {
  margin-top: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  padding: 16px;
}
.result h3 { margin: 0 0 8px; }
.result a { color: var(--accent-dark); font-weight: 700; }
@media (max-width: 980px) {
  .app { grid-template-columns: 1fr; }
  .side { border-right: 0; border-bottom: 1px solid var(--line); }
  .topbar { grid-template-columns: 1fr; }
  .actions { grid-template-columns: 1fr 1fr; }
  .actions input { grid-column: 1 / -1; }
}
"""


def _js() -> str:
    return r"""
let sessions = [];
let filtered = [];
let selected = new Set();

const el = id => document.getElementById(id);

function setStatus(text, type = '') {
  const box = el('status');
  box.textContent = text;
  box.className = 'status' + (type ? ' ' + type : '');
}

function sessionText(item) {
  return [item.id, item.short_id, item.started_at, item.cwd, item.path, item.first_prompt].join(' ').toLowerCase();
}

async function scanSessions() {
  setStatus('正在扫描会话...');
  el('result').hidden = true;
  const params = new URLSearchParams({
    token: TOKEN,
    root: el('root').value,
    limit: el('limit').value
  });
  const response = await fetch('/api/sessions?' + params.toString());
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error || '扫描失败', 'error');
    return;
  }
  sessions = data.sessions || [];
  selected.clear();
  el('summary').textContent = `已扫描 ${data.count} 个会话，目录：${data.root}`;
  applyFilter();
  setStatus('扫描完成。选择会话后填写导出信息。', 'ok');
}

function applyFilter() {
  const q = el('search').value.trim().toLowerCase();
  filtered = q ? sessions.filter(item => sessionText(item).includes(q)) : sessions.slice();
  renderRows();
}

function renderRows() {
  const rows = el('rows');
  if (!filtered.length) {
    rows.innerHTML = '<tr><td colspan="6" class="muted">没有匹配的会话。</td></tr>';
    updateStatusCount();
    return;
  }
  rows.innerHTML = filtered.map(item => {
    const checked = selected.has(item.id);
    return `<tr class="${checked ? 'selected' : ''}">
      <td class="check"><input type="checkbox" data-id="${escapeHtml(item.id)}" ${checked ? 'checked' : ''}></td>
      <td>${escapeHtml(item.started_at || 'unknown')}<div class="muted">${escapeHtml(item.originator || '')}</div></td>
      <td class="id">${escapeHtml(item.id)}<div class="muted">${escapeHtml(item.short_id)}</div></td>
      <td class="prompt">${escapeHtml(item.first_prompt || '未识别到用户 Prompt')}</td>
      <td class="cwd">${escapeHtml(item.cwd || '未知')}</td>
      <td>${escapeHtml(item.size_label)}<div class="muted">${item.line_count} lines</div></td>
    </tr>`;
  }).join('');
  rows.querySelectorAll('input[type="checkbox"]').forEach(box => {
    box.addEventListener('change', event => {
      const id = event.target.dataset.id;
      if (event.target.checked) selected.add(id);
      else selected.delete(id);
      renderRows();
    });
  });
  updateStatusCount();
}

function updateStatusCount() {
  const text = sessions.length
    ? `当前结果 ${filtered.length} 个，已选择 ${selected.size} 个。`
    : '准备就绪。';
  setStatus(text);
}

function selectVisible() {
  filtered.forEach(item => selected.add(item.id));
  renderRows();
}

function selectRecent(count) {
  selected.clear();
  sessions.slice(0, count).forEach(item => selected.add(item.id));
  applyFilter();
}

async function exportSelected() {
  if (!selected.size) {
    setStatus('请先选择要导出的会话。', 'error');
    return;
  }
  setStatus(`正在导出 ${selected.size} 个会话...`);
  const payload = {
    root: el('root').value,
    ids: Array.from(selected),
    course: el('course').value,
    assignment: el('assignment').value,
    review_note: el('review').value,
    redaction: el('redaction').value,
    max_output_lines: el('maxOutput').value,
    out: el('out').value
  };
  const response = await fetch('/api/export?token=' + encodeURIComponent(TOKEN), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error || '导出失败', 'error');
    return;
  }
  const result = el('result');
  result.hidden = false;
  result.innerHTML = `<h3>导出完成</h3>
    <p>已导出 ${data.count} 个会话到 <code>${escapeHtml(data.out)}</code></p>
    <p><a href="${escapeHtml(data.view_url)}" target="_blank" rel="noreferrer">打开最终 HTML</a></p>
    <p class="muted">单会话打开 report.html；多会话打开 index.html。HTML 是最终展示形式，GUI 只是导出控制台。</p>`;
  setStatus('导出完成。', 'ok');
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

el('scanBtn').addEventListener('click', scanSessions);
el('exportBtn').addEventListener('click', exportSelected);
el('search').addEventListener('input', applyFilter);
el('selectVisible').addEventListener('click', selectVisible);
el('clearSelected').addEventListener('click', () => { selected.clear(); renderRows(); });
el('recentFive').addEventListener('click', () => selectRecent(5));

scanSessions();
"""
