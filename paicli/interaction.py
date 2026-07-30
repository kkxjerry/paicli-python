"""Phase 22: command parsing, input history, and status-dock state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CliCommand:
    name: str
    arguments: tuple[str, ...] = ()


class CliCommandParser:
    COMMANDS = {
        "clear",
        "config",
        "context",
        "exit",
        "help",
        "history",
        "model",
        "tools",
    }

    @classmethod
    def parse(cls, text: str) -> CliCommand | None:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        parts = stripped[1:].split()
        if not parts:
            raise ValueError("empty slash command")
        name = parts[0].lower()
        if name not in cls.COMMANDS:
            raise ValueError(f"unknown command: /{name}")
        return CliCommand(name, tuple(parts[1:]))


class PaiCliHistory:
    def __init__(self, path: str | Path, *, limit: int = 500) -> None:
        self.path = Path(path)
        self.limit = limit
        self.entries = self._load()

    def add(self, text: str) -> None:
        value = text.strip()
        if not value or (self.entries and self.entries[-1] == value):
            return
        self.entries.append(value)
        self.entries = self.entries[-self.limit :]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def recent(self, limit: int = 20) -> list[str]:
        return self.entries[-limit:]

    def _load(self) -> list[str]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [str(item) for item in data if str(item).strip()][-self.limit :]


@dataclass
class StatusDock:
    provider: str
    model: str
    mode: str = "react"
    activity: str = "ready"
    token_usage: str = ""

    def update_activity(self, activity: str) -> None:
        self.activity = activity

    def render(self, width: int) -> str:
        parts = [self.provider, self.model, self.mode, self.activity, self.token_usage]
        return " | ".join(part for part in parts if part)[: max(1, width)]


def normalize_input(text: str) -> str:
    """Normalize pasted newlines without changing intentional inner spacing."""

    return text.replace("\r\n", "\n").replace("\r", "\n").strip()
