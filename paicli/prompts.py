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
    """One deterministic set of prompt layers for a role."""

    mode: PromptMode = PromptMode.REACT
    project_root: Path = Path(".")
    skill_index: tuple[str, ...] = ()
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

    PROJECT_INSTRUCTION_FILES = ("AGENTS.md", ".paicli.md")

    def __init__(
        self,
        repository: PromptRepository,
        *,
        max_project_chars: int = 24_000,
    ) -> None:
        if max_project_chars < 1:
            raise ValueError("max_project_chars must be positive")
        self.repository = repository
        self.max_project_chars = max_project_chars

    def assemble(self, context: PromptContext) -> str:
        # 列表顺序就是最终 prompt 顺序，不依赖 dict 或文件扫描的偶然顺序。
        layers: list[tuple[str, str]] = [
            ("base", self.repository.base_prompt),
            ("mode", self.repository.for_mode(context.mode)),
            ("project", self.project_instructions(context.project_root)),
            (
                "skills",
                "\n\n".join(
                    (*context.skill_index, *context.skill_instructions)
                ),
            ),
            ("resources", "\n".join(context.resource_index)),
            ("runtime", "\n".join(context.runtime_notes)),
        ]
        # 空层不输出；标签让模型知道每段内容的来源和边界。
        return "\n\n".join(
            f"<{name}>\n{content.strip()}\n</{name}>"
            for name, content in layers
            if content.strip()
        )

    def project_instructions(self, project_root: Path) -> str:
        """Read one bounded project instruction file without following escapes."""

        root = Path(project_root).resolve()
        for name in self.PROJECT_INSTRUCTION_FILES:
            candidate = root / name
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                continue
            content = resolved.read_text(encoding="utf-8", errors="replace")
            if len(content) <= self.max_project_chars:
                return content
            return (
                content[: self.max_project_chars]
                + "\n\n[Project instructions truncated by PaiCLI.]"
            )
        return ""


def assemble_system_prompt(
    base_prompt: str,
    *,
    mode: PromptMode,
    project_root: str | Path,
    mode_prompt: str = "",
    skill_index: Iterable[str] = (),
    skill_instructions: Iterable[str] = (),
    resource_index: Iterable[str] = (),
    runtime_notes: Iterable[str] = (),
    max_project_chars: int = 24_000,
) -> str:
    """Build the same ordered prompt layers for every Agent role."""

    assembler = PromptAssembler(
        PromptRepository(str(base_prompt), {mode: str(mode_prompt)}),
        max_project_chars=max_project_chars,
    )
    return assembler.assemble(
        PromptContext(
            mode=mode,
            project_root=Path(project_root),
            skill_index=tuple(str(item) for item in skill_index if str(item).strip()),
            skill_instructions=tuple(
                str(item) for item in skill_instructions if str(item).strip()
            ),
            resource_index=tuple(
                str(item) for item in resource_index if str(item).strip()
            ),
            runtime_notes=tuple(
                str(item) for item in runtime_notes if str(item).strip()
            ),
        )
    )


def resource_index_lines(
    resources: Iterable[tuple[str, str, str]],
) -> tuple[str, ...]:
    """把 MCP 资源元组转成可放入 resources 层的简短索引。"""

    return tuple(
        f"- {server}: {uri} ({description})"
        for server, uri, description in resources
    )
