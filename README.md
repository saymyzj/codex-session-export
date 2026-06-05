# Codex Session Export

Codex Session Export 是一个本地导出工具，用来把 Codex 的 JSONL 会话记录整理成可阅读、可追溯、适合课程作业或项目归档的 HTML/Markdown 报告。

它不是简单的“JSONL 转网页”，而是面向 AI 辅助完成记录的证据报告生成器：

- GUI 控制台：扫描本地会话、搜索、勾选要导出的会话、填写课程/作业/人工审核说明。
- HTML 报告：最终展示仍然是可离线打开的 `report.html` 或批量 `index.html`。
- 默认视图：保留关键提示词、AI 回复、命令、验证结果、文件修改等重点内容。
- 完整视图：保留全部解析出的事件，工具调用和长输出默认折叠。
- 多会话导出：可选择一个或多个会话 ID，不需要手写命令参数。
- 可信追溯：生成原始日志 SHA256、redacted JSONL 和 `manifest.json`。
- 脱敏处理：支持隐藏本地路径、邮箱、API Key、Bearer Token 和常见密钥字段。
- 图片处理：识别到的本地图片会复制到 `assets/`，不会以内嵌 base64 形式塞进 HTML。

## 安装

在仓库根目录运行：

```bash
python3 -m pip install -e .
```

也可以不安装，直接用模块方式运行：

```bash
python3 -m codex_session_export --help
```

## 推荐用法：启动 GUI 控制台

```bash
python3 -m codex_session_export gui
```

安装后也可以运行：

```bash
codex-session-export gui
```

启动后会打开本地页面，默认地址类似：

```text
http://127.0.0.1:8765/?token=...
```

这个页面只用于控制导出流程，不是最终报告展示页。

## GUI 使用步骤

1. 点击“扫描会话”，默认扫描 `~/.codex/sessions`。
2. 在会话表格里按 ID、首条 Prompt、工作目录或路径搜索。
3. 勾选要导出的会话，可以选择单个、多个、当前搜索结果或最近 5 个。
4. 填写课程名称、作业/项目名称和人工审核说明。
5. 选择脱敏模式，建议提交给他人前使用“普通脱敏”或“严格脱敏”。
6. 确认输出目录。
7. 点击“导出所选会话”。
8. 导出完成后点击“打开最终 HTML”。

单会话会打开 `report.html`；多会话会打开 `index.html`，再从索引进入每个子报告。

## 导出结果结构

单会话导出：

```text
exports/gui-xxxx/
├─ report.html
├─ report.md
├─ assets/
└─ raw/
   ├─ session.redacted.jsonl
   └─ manifest.json
```

多会话导出：

```text
exports/gui-xxxx/
├─ index.html
├─ 019e9775-f415-77b3-a0be-21492cc2fc14/
│  ├─ report.html
│  ├─ report.md
│  └─ raw/
└─ 019e9740-8e03-7712-a513-6e60dd0ff3b4/
   ├─ report.html
   ├─ report.md
   └─ raw/
```

提交或归档时，通常优先使用 HTML 文件：

- 单会话：提交 `report.html`
- 多会话：提交 `index.html` 和对应子目录
- 需要追溯时：附带 `raw/manifest.json` 和 `raw/session.redacted.jsonl`

## 报告视图

HTML 报告顶部提供多个筛选按钮：

- 默认视图：适合快速阅读和提交。
- 完整视图：保留全部解析出的事件。
- 提示词：只看用户如何指挥 AI。
- 命令记录：只看终端命令和结果。
- 验证记录：只看测试、构建、运行等验证动作。
- 文件修改：只看修改相关记录。

报告会尽量保留原始时间线。默认视图只是折叠和降噪，不会把完整证据链静默删除。

## 脱敏模式

默认是 `basic`，GUI 中显示为“普通脱敏”。

- `basic`：隐藏本地 Home 路径、邮箱、OpenAI Key、Bearer Token、常见密钥赋值。
- `strict`：在 `basic` 基础上进一步隐藏更多绝对路径，适合对外提交。
- `none`：不脱敏，只建议自己本地归档使用。

## 高级用法：命令行

GUI 是推荐入口；CLI 适合自动化、脚本或批量任务。

查看本地会话：

```bash
python3 -m codex_session_export list
```

按 ID 导出单个会话：

```bash
python3 -m codex_session_export export \
  --id 019e9775-f415 \
  --out exports/my-report \
  --course "软件工程" \
  --assignment "实验二 AI 辅助完成记录" \
  --review-note "最终代码、运行结果和报告内容均由本人检查确认。"
```

一次导出多个会话：

```bash
python3 -m codex_session_export export \
  --id 019e9775-f415 \
  --id 019e9740-8e03 \
  --out exports/course-work
```

从文件读取多个 ID：

```bash
python3 -m codex_session_export export --ids-file sessions.txt --out exports/batch
```

导出最新会话：

```bash
python3 -m codex_session_export export --latest --out exports/latest
```

## 常用参数

```text
gui
  --root PATH        指定 Codex sessions 根目录
  --out PATH         GUI 默认输出目录
  --host HOST        监听地址，默认 127.0.0.1
  --port PORT        监听端口，默认 8765
  --no-browser      启动后不自动打开浏览器

list
  --limit N          只显示最新 N 条
  --root PATH        指定 Codex sessions 根目录

export
  --latest           导出最新会话
  --id ID            按会话 ID 或短前缀选择，可重复传入
  --ids-file PATH    从文件读取多个 ID
  --all              导出全部会话
  --limit N          与 --all 连用，限制数量
  --out PATH         输出目录
  --course TEXT      报告中的课程名称
  --assignment TEXT  报告中的作业/项目名称
  --review-note TEXT 人工审核说明
  --redaction MODE   none / basic / strict
```

## 建议工作流

1. 运行 `python3 -m codex_session_export gui`。
2. 在 GUI 中搜索并勾选相关会话。
3. 填写课程、作业和人工审核说明。
4. 使用普通或严格脱敏导出。
5. 打开最终 HTML，检查默认视图、完整视图和原始日志校验信息。
6. 提交 HTML；必要时附带 `raw/manifest.json` 和 `raw/session.redacted.jsonl`。
