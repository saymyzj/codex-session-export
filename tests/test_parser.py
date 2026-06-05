from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_session_export.cli import _resolve_sessions
from codex_session_export.gui import _session_payload, _validate_output_dir
from codex_session_export.parser import parse_session
from codex_session_export.redact import Redactor
from codex_session_export.render import write_report


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


class ParserTests(unittest.TestCase):
    def test_parse_terminal_command_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            session = tmp_path / "rollout-test.jsonl"
            write_jsonl(
                session,
                [
                    {"timestamp": "2026-06-05T00:00:00Z", "type": "session_meta", "payload": {"id": "abc", "cwd": "/Users/alice/demo", "originator": "Codex Desktop"}},
                    {"timestamp": "2026-06-05T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "帮我运行测试"}]}},
                    {"timestamp": "2026-06-05T00:00:02Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "{\"cmd\":\"pytest -q\",\"workdir\":\"/Users/alice/demo\"}", "call_id": "call_1"}},
                    {"timestamp": "2026-06-05T00:00:03Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_1", "output": "Process exited with code 0\nOutput:\n2 passed"}},
                ],
            )
            report = parse_session(session, Redactor(mode="strict", workspace=tmp_path))

            self.assertEqual(report.meta.session_id, "abc")
            self.assertEqual(report.stats["user_prompts"], 1)
            self.assertEqual(report.stats["terminal_commands"], 1)
            self.assertEqual(report.stats["validation_runs"], 1)
            command = next(event for event in report.events if event.kind == "terminal_command")
            self.assertEqual(command.exit_code, 0)
            self.assertIn("2 passed", command.details)
            self.assertEqual(command.cwd, "[ABSOLUTE_PATH]")

    def test_redacts_secret_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "rollout-secret.jsonl"
            write_jsonl(
                session,
                [
                    {"timestamp": "2026-06-05T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "TOKEN=abc123456789 and a@b.com"}]}},
                ],
            )
            report = parse_session(session, Redactor(mode="basic"))
            self.assertIn("[SECRET_REDACTED]", report.events[0].text)
            self.assertIn("[EMAIL_REDACTED]", report.events[0].text)

    def test_internal_context_is_not_counted_as_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "rollout-context.jsonl"
            write_jsonl(
                session,
                [
                    {"timestamp": "2026-06-05T00:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /tmp/demo"}]}},
                    {"timestamp": "2026-06-05T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "请导出这个会话"}]}},
                ],
            )
            report = parse_session(session, Redactor(mode="none"))
            self.assertEqual(report.stats["user_prompts"], 1)
            self.assertEqual(report.events[0].kind, "system_context")
            self.assertEqual(report.events[1].kind, "prompt")


class CliSelectionTests(unittest.TestCase):
    def test_resolve_sessions_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "2026" / "06" / "05" / "rollout-demo.jsonl"
            session.parent.mkdir(parents=True, exist_ok=True)
            write_jsonl(
                session,
                [
                    {"timestamp": "2026-06-05T00:00:00Z", "type": "session_meta", "payload": {"id": "019e9775-f415-77b3-a0be-21492cc2fc14", "cwd": "/demo", "originator": "Codex Desktop"}},
                ],
            )

            args = SimpleNamespace(
                session=None,
                latest=False,
                ids=["019e9775-f415"],
                ids_file=None,
                all=False,
                limit=None,
                root=root,
            )

            resolved = _resolve_sessions(args)
            self.assertEqual(resolved, [session])


class GuiTests(unittest.TestCase):
    def test_session_payload_includes_first_real_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "rollout-gui.jsonl"
            write_jsonl(
                session,
                [
                    {"timestamp": "2026-06-05T00:00:00Z", "type": "session_meta", "payload": {"id": "gui-session-id", "cwd": "/demo", "originator": "Codex Desktop"}},
                    {"timestamp": "2026-06-05T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>hidden</environment_context>"}]}},
                    {"timestamp": "2026-06-05T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "请帮我导出这个会话"}]}},
                ],
            )

            payload = _session_payload(session)

            self.assertEqual(payload["id"], "gui-session-id")
            self.assertIn("请帮我导出", payload["first_prompt"])

    def test_output_dir_rejects_codex_home(self) -> None:
        bad_path = Path.home() / ".codex" / "exports"
        with self.assertRaises(ValueError):
            _validate_output_dir(bad_path)


class RenderAssetTests(unittest.TestCase):
    def test_data_uri_image_is_written_to_assets(self) -> None:
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            session = tmp_path / "rollout-image.jsonl"
            out_dir = tmp_path / "out"
            write_jsonl(
                session,
                [
                    {"timestamp": "2026-06-05T00:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": pixel, "alt": "pixel"}]}},
                ],
            )

            redactor = Redactor(mode="basic")
            report = parse_session(session, redactor)
            write_report(report, out_dir, redactor=redactor)

            asset = out_dir / "assets" / "asset-001.png"
            html = (out_dir / "report.html").read_text(encoding="utf-8")
            self.assertTrue(asset.exists())
            self.assertIn('src="assets/asset-001.png"', html)
            self.assertNotIn("data:image/png;base64", html)


if __name__ == "__main__":
    unittest.main()
