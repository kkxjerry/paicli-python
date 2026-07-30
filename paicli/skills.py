"""Phase 15: discoverable, lazily loaded SKILL.md instructions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .tools import ToolRegistry, ToolSpec


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    path: Path
    allowed_tools: tuple[str, ...] = ()


class SkillFrontmatterParser:
    @staticmethod
    def parse(path: str | Path) -> Skill:
        skill_path = Path(path)
        text = skill_path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError(f"{skill_path} has no frontmatter")
        try:
            header, body = text[4:].split("\n---\n", 1)
        except ValueError as exc:
            raise ValueError(f"{skill_path} has unclosed frontmatter") from exc

        metadata: dict[str, str] = {}
        for line in header.splitlines():
            if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("\"'")
        name = metadata.get("name", "").strip()
        description = metadata.get("description", "").strip()
        if not name or not description:
            raise ValueError("skill requires name and description")
        raw_tools = metadata.get("allowed-tools", "")
        tools = tuple(
            item.strip()
            for item in raw_tools.strip("[]").split(",")
            if item.strip()
        )
        return Skill(name, description, body.strip(), skill_path, tools)


class SkillRegistry:
    def __init__(self, roots: Iterable[str | Path]) -> None:
        self.roots = [Path(root) for root in roots]
        self._skills: dict[str, Skill] = {}

    def discover(self) -> list[Skill]:
        discovered: dict[str, Skill] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                skill = SkillFrontmatterParser.parse(path)
                discovered[skill.name] = skill
        self._skills = discovered
        return list(discovered.values())

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise KeyError(f"unknown skill: {name}") from exc

    def index(self) -> str:
        return "\n".join(
            f"- {skill.name}: {skill.description}"
            for skill in sorted(self._skills.values(), key=lambda item: item.name)
        )


class SkillContextBuffer:
    def __init__(self) -> None:
        self.loaded: set[str] = set()

    def load(self, skill: Skill) -> str:
        if skill.name in self.loaded:
            return f"Skill {skill.name!r} is already loaded."
        self.loaded.add(skill.name)
        return (
            f"<skill name=\"{skill.name}\">\n"
            f"{skill.instructions}\n"
            "</skill>"
        )

    def clear(self) -> None:
        self.loaded.clear()


class SkillStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save_enabled(self, names: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"enabled": sorted(names)}, indent=2) + "\n",
            encoding="utf-8",
        )

    def load_enabled(self) -> set[str]:
        if not self.path.is_file():
            return set()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return set(data.get("enabled", []))


def register_skill_tool(
    tools: ToolRegistry,
    skills: SkillRegistry,
    buffer: SkillContextBuffer | None = None,
) -> SkillContextBuffer:
    context = buffer or SkillContextBuffer()
    tools.register(
        ToolSpec(
            "load_skill",
            "Load detailed instructions for one relevant skill.",
            tools.object_schema(
                {
                    "name": {
                        "type": "string",
                        "enum": sorted(skills._skills),
                    }
                },
                required=["name"],
            ),
            lambda arguments: context.load(skills.get(str(arguments["name"]))),
        )
    )
    return context
