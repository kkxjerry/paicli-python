"""Phase 22：斜杠命令解析、输入历史与状态栏数据。

这些类不直接 input/print，只处理数据：因此容易测试，也可以被未来的
prompt_toolkit/Textual UI 复用。真正的 CLI 输入循环仍在 __main__.py。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CliCommand:
    """经解析的斜杠命令，参数保留输入顺序。"""

    name: str
    arguments: tuple[str, ...] = ()


class CliCommandParser:
    """只把以 / 开头的整条输入解析为 CLI 命令。"""

    # 白名单防止拼写错误的命令被静默当成普通 prompt。
    COMMANDS = {
        "clear",
        "config",
        "context",
        "exit",
        "help",
        "history",
        "model",
        "plan",
        "resume",
        "runs",
        "team",
        "tools",
        "trace",
    }

    @classmethod
    def parse(cls, text: str) -> CliCommand | None:
        # 普通 prompt 中即使包含 /model，只要不在开头就不是命令。
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        # 去掉 / 后按空白分词；当前不支持 shell 引号和转义语法。
        parts = stripped[1:].split()
        if not parts:
            raise ValueError("empty slash command")
        name = parts[0].lower()
        if name not in cls.COMMANDS:
            raise ValueError(f"unknown command: /{name}")
        return CliCommand(name, tuple(parts[1:]))


class PaiCliHistory:
    """持久化用户输入，限制条数并去掉连续重复。"""

    def __init__(self, path: str | Path, *, limit: int = 500) -> None:
        self.path = Path(path)
        self.limit = limit
        self.entries = self._load()

    def add(self, text: str) -> None:
        # 空输入不记录；只对“相邻且相同”的输入去重。
        value = text.strip()
        if not value or (self.entries and self.entries[-1] == value):
            return
        self.entries.append(value)
        # 超出上限时丢弃最旧记录。
        self.entries = self.entries[-self.limit :]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def recent(self, limit: int = 20) -> list[str]:
        """返回最近 N 条输入，原列表不变。"""

        return self.entries[-limit:]

    def _load(self) -> list[str]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        # 历史文件损坏不应导致 CLI 无法启动，降级成空历史。
        except (json.JSONDecodeError, OSError):
            return []
        return [str(item) for item in data if str(item).strip()][-self.limit :]


@dataclass
class StatusDock:
    """状态栏的可变状态：模型信息、当前活动和 token 用量。"""

    provider: str
    model: str
    mode: str = "react"
    activity: str = "ready"
    token_usage: str = ""

    def update_activity(self, activity: str) -> None:
        self.activity = activity

    def render(self, width: int) -> str:
        # 空字段不显示，最后硬截断，确保不撑破终端一行。
        parts = [self.provider, self.model, self.mode, self.activity, self.token_usage]
        return " | ".join(part for part in parts if part)[: max(1, width)]


def normalize_input(text: str) -> str:
    """统一 Windows/old Mac 换行符，但不改变内部有意保留的空格。"""

    return text.replace("\r\n", "\n").replace("\r", "\n").strip()
