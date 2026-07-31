"""Phase 19：确定性的分层系统提示词组装。

不再把所有规则拼成一个难维护的大字符串，而是按固定顺序合并：
base -> mode -> project -> skills -> resources -> runtime。
固定前缀也有利于模型端的 prompt cache；动态内容尽量放在后面。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable


class PromptMode(str, Enum):
    """当前 Agent 的工作方式，用来选择对应的模式指令。"""

    REACT = "react"
    PLAN = "plan"
    TEAM = "team"


@dataclass(frozen=True)
class PromptContext:
    """一次系统提示词组装所需的动态上下文。"""

    mode: PromptMode = PromptMode.REACT
    project_root: Path = Path(".")
    skill_instructions: tuple[str, ...] = ()
    resource_index: tuple[str, ...] = ()
    runtime_notes: tuple[str, ...] = ()


@dataclass
class PromptRepository:
    """保存稳定基础提示词和各模式的附加提示词。"""

    base_prompt: str
    mode_prompts: dict[PromptMode, str] = field(default_factory=dict)

    def for_mode(self, mode: PromptMode) -> str:
        # 允许某个模式没有专用指令，返回空字符串后会被组装器忽略。
        return self.mode_prompts.get(mode, "")


class PromptAssembler:
    """将稳定的前缀层放在每轮动态上下文之前。"""

    def __init__(self, repository: PromptRepository) -> None:
        self.repository = repository

    def assemble(self, context: PromptContext) -> str:
        # 列表顺序就是最终 prompt 顺序，不依赖 dict 或文件扫描的偶然顺序。
        layers: list[tuple[str, str]] = [
            ("base", self.repository.base_prompt),
            ("mode", self.repository.for_mode(context.mode)),
            ("project", self._project_instructions(context.project_root)),
            ("skills", "\n\n".join(context.skill_instructions)),
            ("resources", "\n".join(context.resource_index)),
            ("runtime", "\n".join(context.runtime_notes)),
        ]
        # 空层不输出；标签让模型知道每段内容的来源和边界。
        return "\n\n".join(
            f"<{name}>\n{content.strip()}\n</{name}>"
            for name, content in layers
            if content.strip()
        )

    @staticmethod
    def _project_instructions(project_root: Path) -> str:
        # AGENTS.md 优先级高于旧式 .paicli.md；找到第一个就停止。
        for name in ("AGENTS.md", ".paicli.md"):
            path = project_root / name
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        return ""


def resource_index_lines(
    resources: Iterable[tuple[str, str, str]],
) -> tuple[str, ...]:
    """把 MCP 资源元组转成可放入 resources 层的简短索引。"""

    return tuple(
        f"- {server}: {uri} ({description})"
        for server, uri, description in resources
    )
