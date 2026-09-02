"""Phase 16：输出渲染器抽象与紧凑型终端 UI。

Agent 只产生 event/status，不关心“纯文本”还是“行内 UI”。Renderer 将两者分开，
以后可以替换终端库，而不需改 Agent 循环。当前 InlineRenderer 仍是轻量实现，
没有真正的键盘交互或全屏 TUI。
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Protocol, TextIO


@dataclass(frozen=True)
class StatusInfo:
    """状态栏中可显示的模型、运行模式与上下文信息。"""

    provider: str = ""
    model: str = ""
    mode: str = "react"
    context: str = ""


class Renderer(Protocol):
    """Agent 输出端只需实现的两个方法。"""

    def event(self, kind: str, text: str) -> None:
        """Render an Agent event."""

    def status(self, info: StatusInfo) -> None:
        """Render model and runtime status."""


class PlainRenderer:
    """无 ANSI 控制字符的纯文本渲染，适合日志、重定向和 CI。"""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self.stream = stream

    def event(self, kind: str, text: str) -> None:
        if kind == "content_delta":
            self.stream.write(text)
            self.stream.flush()
        elif kind == "reasoning_delta":
            # Plain/log mode retains the channel label. Interactive rendering
            # can present reasoning more compactly without changing the Agent.
            self.stream.write(f"[thinking] {text}")
            self.stream.flush()
        elif kind == "answer":
            self.stream.write(f"{text}\n")
        else:
            self.stream.write(f"[{kind}] {text}\n")

    def status(self, info: StatusInfo) -> None:
        fields = [info.provider, info.model, info.mode, info.context]
        self.stream.write(" | ".join(field for field in fields if field) + "\n")


@dataclass
class FoldableBlock:
    """一个可折叠的工具调用块；状态本身与终端输出分离。"""

    title: str
    content: str
    expanded: bool = False

    def render(self, width: int = 80) -> str:
        # 本期用 ASCII > / v，避免依赖终端的 Unicode 图标能力。
        marker = "v" if self.expanded else ">"
        header = f"{marker} {self.title}"[:width]
        if not self.expanded:
            return header
        # 每行硬截断到终端宽度，当前不做单词换行。
        body = "\n".join(line[:width] for line in self.content.splitlines())
        return f"{header}\n{body}"


class BlockRegistry:
    """按输出顺序保存工具块，并通过下标切换展开状态。"""

    def __init__(self) -> None:
        self.blocks: list[FoldableBlock] = []

    def add(self, block: FoldableBlock) -> int:
        self.blocks.append(block)
        return len(self.blocks) - 1

    def toggle(self, index: int) -> FoldableBlock:
        block = self.blocks[index]
        block.expanded = not block.expanded
        return block


class SlashPalette:
    """对 /model 之类的斜杠命令做简单前缀补全。"""

    def __init__(self, commands: dict[str, str]) -> None:
        self.commands = dict(commands)

    def complete(self, prefix: str) -> list[tuple[str, str]]:
        return sorted(
            (
                (command, description)
                for command, description in self.commands.items()
                if command.startswith(prefix)
            ),
            key=lambda item: item[0],
        )


@dataclass(frozen=True)
class TerminalCapabilities:
    """启动时探测终端是否交互、支持 ANSI，以及可用宽度。"""

    ansi: bool
    width: int
    interactive: bool

    @classmethod
    def detect(cls, stream: TextIO = sys.stdout) -> TerminalCapabilities:
        # StringIO/管道/文件通常不是 TTY，此时不能输出光标控制序列。
        interactive = bool(getattr(stream, "isatty", lambda: False)())
        term = os.getenv("TERM", "")
        return cls(
            ansi=interactive and term.lower() != "dumb",
            width=max(20, shutil.get_terminal_size((80, 24)).columns),
            interactive=interactive,
        )


class InlineRenderer:
    """用行内状态和紧凑预览显示 Agent 执行过程。"""

    def __init__(
        self,
        stream: TextIO = sys.stdout,
        *,
        capabilities: TerminalCapabilities | None = None,
    ) -> None:
        self.stream = stream
        self.capabilities = capabilities or TerminalCapabilities.detect(stream)
        self.blocks = BlockRegistry()
        self._status = ""
        self._reasoning_active = False
        self._content_active = False

    def event(self, kind: str, text: str) -> None:
        if kind == "reasoning_delta":
            if not self._reasoning_active:
                self.stream.write("Thinking: ")
                self._reasoning_active = True
            self.stream.write(text)
            self.stream.flush()
        elif kind == "content_delta":
            if self._reasoning_active:
                self.stream.write("\n")
                self._reasoning_active = False
            self._content_active = True
            self.stream.write(text)
            self.stream.flush()
        elif kind == "tool":
            if self._reasoning_active or self._content_active:
                self.stream.write("\n")
                self._reasoning_active = False
                self._content_active = False
            # tool 事件先创建 waiting 块，result 到达时再回填。
            index = self.blocks.add(FoldableBlock(f"tool {text}", "waiting"))
            self.stream.write(self.blocks.blocks[index].render(self.capabilities.width))
            self.stream.write("\n")
        elif kind == "result":
            if self.blocks.blocks:
                self.blocks.blocks[-1].content = text
            # 11 是“  result: ”前缀预留的宽度，预览不让终端换行。
            preview = text.replace("\n", " ")[: max(1, self.capabilities.width - 11)]
            self.stream.write(f"  result: {preview}\n")
        elif kind == "answer":
            if self._reasoning_active or self._content_active:
                self.stream.write("\n")
                self._reasoning_active = False
                self._content_active = False
            else:
                self.stream.write(f"{text}\n")
        else:
            self.stream.write(f"[{kind}] {text}\n")

    def status(self, info: StatusInfo) -> None:
        fields = [info.provider, info.model, info.mode, info.context]
        status = " | ".join(field for field in fields if field)
        self._status = status[: self.capabilities.width]
        if self.capabilities.ansi:
            # \r 回到行首，ESC[2K 清除整行，然后原地刷新状态。
            self.stream.write(f"\r\x1b[2K{self._status}")
        else:
            # 非交互输出不能覆盖旧行，因此降级为普通日志。
            self.stream.write(f"[status] {self._status}\n")
        self.stream.flush()


RendererFactory = Callable[[str, TextIO], Renderer]


def create_renderer(name: str, stream: TextIO = sys.stdout) -> Renderer:
    """根据配置名称创建渲染器，未知名称立即报错。"""

    if name == "plain":
        return PlainRenderer(stream)
    if name == "inline":
        return InlineRenderer(stream)
    raise ValueError(f"unknown renderer: {name}")
