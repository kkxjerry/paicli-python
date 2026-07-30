"""Phase 19: deterministic layered system-prompt assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable


class PromptMode(str, Enum):
    REACT = "react"
    PLAN = "plan"
    TEAM = "team"


@dataclass(frozen=True)
class PromptContext:
    mode: PromptMode = PromptMode.REACT
    project_root: Path = Path(".")
    skill_instructions: tuple[str, ...] = ()
    resource_index: tuple[str, ...] = ()
    runtime_notes: tuple[str, ...] = ()


@dataclass
class PromptRepository:
    base_prompt: str
    mode_prompts: dict[PromptMode, str] = field(default_factory=dict)

    def for_mode(self, mode: PromptMode) -> str:
        return self.mode_prompts.get(mode, "")


class PromptAssembler:
    """Orders stable prefix layers before dynamic per-turn context."""

    def __init__(self, repository: PromptRepository) -> None:
        self.repository = repository

    def assemble(self, context: PromptContext) -> str:
        layers: list[tuple[str, str]] = [
            ("base", self.repository.base_prompt),
            ("mode", self.repository.for_mode(context.mode)),
            ("project", self._project_instructions(context.project_root)),
            ("skills", "\n\n".join(context.skill_instructions)),
            ("resources", "\n".join(context.resource_index)),
            ("runtime", "\n".join(context.runtime_notes)),
        ]
        return "\n\n".join(
            f"<{name}>\n{content.strip()}\n</{name}>"
            for name, content in layers
            if content.strip()
        )

    @staticmethod
    def _project_instructions(project_root: Path) -> str:
        for name in ("AGENTS.md", ".paicli.md"):
            path = project_root / name
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        return ""


def resource_index_lines(
    resources: Iterable[tuple[str, str, str]],
) -> tuple[str, ...]:
    return tuple(
        f"- {server}: {uri} ({description})"
        for server, uri, description in resources
    )
