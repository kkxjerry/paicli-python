"""Phase 16: renderer abstraction and compact inline terminal UI."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Protocol, TextIO


@dataclass(frozen=True)
class StatusInfo:
    provider: str = ""
    model: str = ""
    mode: str = "react"
    context: str = ""


class Renderer(Protocol):
    def event(self, kind: str, text: str) -> None:
        """Render an Agent event."""

    def status(self, info: StatusInfo) -> None:
        """Render model and runtime status."""


class PlainRenderer:
    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self.stream = stream

    def event(self, kind: str, text: str) -> None:
        if kind == "answer":
            self.stream.write(f"{text}\n")
        else:
            self.stream.write(f"[{kind}] {text}\n")

    def status(self, info: StatusInfo) -> None:
        fields = [info.provider, info.model, info.mode, info.context]
        self.stream.write(" | ".join(field for field in fields if field) + "\n")


@dataclass
class FoldableBlock:
    title: str
    content: str
    expanded: bool = False

    def render(self, width: int = 80) -> str:
        marker = "v" if self.expanded else ">"
        header = f"{marker} {self.title}"[:width]
        if not self.expanded:
            return header
        body = "\n".join(line[:width] for line in self.content.splitlines())
        return f"{header}\n{body}"


class BlockRegistry:
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
    ansi: bool
    width: int
    interactive: bool

    @classmethod
    def detect(cls, stream: TextIO = sys.stdout) -> TerminalCapabilities:
        interactive = bool(getattr(stream, "isatty", lambda: False)())
        term = os.getenv("TERM", "")
        return cls(
            ansi=interactive and term.lower() != "dumb",
            width=max(20, shutil.get_terminal_size((80, 24)).columns),
            interactive=interactive,
        )


class InlineRenderer:
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

    def event(self, kind: str, text: str) -> None:
        if kind == "tool":
            index = self.blocks.add(FoldableBlock(f"tool {text}", "waiting"))
            self.stream.write(self.blocks.blocks[index].render(self.capabilities.width))
            self.stream.write("\n")
        elif kind == "result":
            if self.blocks.blocks:
                self.blocks.blocks[-1].content = text
            preview = text.replace("\n", " ")[: max(1, self.capabilities.width - 11)]
            self.stream.write(f"  result: {preview}\n")
        elif kind == "answer":
            self.stream.write(f"{text}\n")
        else:
            self.stream.write(f"[{kind}] {text}\n")

    def status(self, info: StatusInfo) -> None:
        fields = [info.provider, info.model, info.mode, info.context]
        status = " | ".join(field for field in fields if field)
        self._status = status[: self.capabilities.width]
        if self.capabilities.ansi:
            self.stream.write(f"\r\x1b[2K{self._status}")
        else:
            self.stream.write(f"[status] {self._status}\n")
        self.stream.flush()


RendererFactory = Callable[[str, TextIO], Renderer]


def create_renderer(name: str, stream: TextIO = sys.stdout) -> Renderer:
    if name == "plain":
        return PlainRenderer(stream)
    if name == "inline":
        return InlineRenderer(stream)
    raise ValueError(f"unknown renderer: {name}")
