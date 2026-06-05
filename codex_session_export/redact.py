from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Redactor:
    mode: str = "basic"
    workspace: Path | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def redact(self, text: str | None) -> str:
        if text is None:
            return ""
        if self.mode == "none":
            return text

        value = str(text)
        value = self._replace("home_path", re.escape(str(Path.home())), "[HOME]", value)
        value = self._replace(
            "email",
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            "[EMAIL_REDACTED]",
            value,
        )
        value = self._replace(
            "openai_key",
            r"\bsk-[A-Za-z0-9_\-]{16,}\b",
            "[OPENAI_KEY_REDACTED]",
            value,
        )
        value = self._replace(
            "bearer_token",
            r"\bBearer\s+[A-Za-z0-9._\-+/=]{16,}",
            "Bearer [TOKEN_REDACTED]",
            value,
        )
        value = self._replace(
            "secret_assignment",
            r"(?i)\b(API[_-]?KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION)\s*=\s*([^\s\"']+)",
            r"\1=[SECRET_REDACTED]",
            value,
        )

        if self.mode == "strict":
            value = self._redact_absolute_paths(value)

        return value

    def _replace(self, key: str, pattern: str, repl: str, value: str) -> str:
        new_value, count = re.subn(pattern, repl, value)
        if count:
            self.counts[key] = self.counts.get(key, 0) + count
        return new_value

    def _redact_absolute_paths(self, value: str) -> str:
        workspace = str(self.workspace) if self.workspace else ""

        def repl(match: re.Match[str]) -> str:
            path = match.group(0)
            if workspace and path.startswith(workspace):
                return path.replace(workspace, "[WORKSPACE]", 1)
            if path.startswith("/tmp") or path.startswith("/private/tmp"):
                return path
            self.counts["absolute_path"] = self.counts.get("absolute_path", 0) + 1
            return "[ABSOLUTE_PATH]"

        return re.sub(r"(?<![\w.-])/(?:Users|home|var|opt|etc|private/var)/[^\s\"'`<>]+", repl, value)


def redact_json_line(line: str, redactor: Redactor) -> str:
    return redactor.redact(line)

