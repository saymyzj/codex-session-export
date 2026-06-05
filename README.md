# Codex Session Export

Codex Session Export 是一个本地命令行工具，用来把 Codex 的 JSONL 会话日志导出成可阅读、可追溯、适合归档或提交的 HTML/Markdown 报告。

它的重点不是简单把 JSONL 转成网页，而是把 AI 辅助完成作业或项目的过程整理成一份“证据报告”：

- 默认视图：保留关键提示词、AI 回复、命令、验证结果、文件修改等重点内容。
- 完整视图：保留全部解析出的事件，工具调用和长输出默认折叠。
- 支持按会话 ID 选择导出，一个或多个会话都可以。
- 自动生成原始日志 SHA256、redacted JSONL 和 manifest，方便追溯。
- 自动脱敏本地路径、邮箱、API Key、Bearer Token 和常见密钥字段。
- 图片不会以内嵌 base64 形式塞进 HTML；能识别到的本地图片会复制到 `assets/`。

## 安装

在仓库根目录运行：

```bash
python3 -m pip install -e .
```

也可以不安装，直接用模块方式运行：

```bash
python3 -m codex_session_export --help
```

## 查看本地会话

默认扫描 `~/.codex/sessions`：

```bash
python3 -m codex_session_export list
```

输出里会看到短 ID、完整 ID、时间、来源、行数和 JSONL 路径，例如：

```text
 1. 019e9775-f41  2026-06-05T11:05:50.357Z  Codex Desktop  161 lines
    id: 019e9775-f415-77b3-a0be-21492cc2fc14
    path: /Users/you/.codex/sessions/2026/06/05/rollout-xxx.jsonl
    cwd: /Users/you/project
```

后续导出可以复制完整 ID，也可以复制足够唯一的短前缀。

## 导出最新会话

```bash
python3 -m codex_session_export export --latest --out exports/latest
```

生成：

```text
exports/latest/
├─ report.html
├─ report.md
├─ assets/
└─ raw/
   ├─ session.redacted.jsonl
   └─ manifest.json
```

打开 `report.html` 即可查看带筛选按钮和折叠详情的报告。

## 按 ID 导出单个会话

先用 `list` 找到 ID，然后：

```bash
python3 -m codex_session_export export \
  --id 019e9775-f415 \
  --out exports/my-report \
  --course "软件工程" \
  --assignment "实验二 AI 辅助完成记录" \
  --review-note "最终代码、运行结果和报告内容均由本人检查确认。"
```

`--id` 支持完整会话 ID，也支持唯一前缀。

## 一次导出多个会话

重复传入 `--id`：

```bash
python3 -m codex_session_export export \
  --id 019e9775-f415 \
  --id 019e9740-8e03 \
  --out exports/course-work
```

批量导出会生成一个总入口：

```text
exports/course-work/
├─ index.html
├─ 019e9775-f415-77b3-a0be-21492cc2fc14/
│  ├─ report.html
│  └─ raw/manifest.json
└─ 019e9740-8e03-7712-a513-6e60dd0ff3b4/
   ├─ report.html
   └─ raw/manifest.json
```

也可以把 ID 写到文本文件，每行一个：

```bash
python3 -m codex_session_export export --ids-file sessions.txt --out exports/batch
```

## 导出全部或最新 N 个

导出扫描范围内全部会话：

```bash
python3 -m codex_session_export export --all --out exports/all
```

只导出最新 5 个：

```bash
python3 -m codex_session_export export --all --limit 5 --out exports/recent-5
```

## 脱敏模式

默认是 `basic`：

```bash
python3 -m codex_session_export export --latest --redaction basic
```

可选值：

- `basic`：脱敏本地 Home 路径、邮箱、OpenAI Key、Bearer Token、常见密钥赋值。
- `strict`：在 `basic` 基础上进一步隐藏更多绝对路径，适合对外提交。
- `none`：不脱敏，只建议自己本地归档使用。

## 常用参数

```text
list
  --limit N          只显示最新 N 条
  --root PATH        指定 Codex sessions 根目录

export
  --latest           导出最新会话
  --id ID            按会话 ID 导出，可重复传入
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

1. 运行 `list` 找到要提交的会话 ID。
2. 用 `export --id ...` 导出报告。
3. 打开 `report.html` 检查默认视图是否清晰。
4. 补充 `--course`、`--assignment`、`--review-note` 后重新导出正式版本。
5. 提交 `report.html`，必要时附带 `raw/manifest.json` 和 `raw/session.redacted.jsonl`。

