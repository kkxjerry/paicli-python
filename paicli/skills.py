"""Phase 15：可发现、按需加载的 SKILL.md 技能系统。

核心流程：

    扫描各个 roots 下的 SKILL.md
                 |
                 v
       解析 frontmatter 元数据
                 |
                 v
       先只把名称+描述放进索引
                 |
       模型调用 load_skill(name)
                 |
                 v
       把完整 instructions 注入上下文

“懒加载”的目的是：技能很多时，不必一开始就消耗大量 token。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .tools import ToolRegistry, ToolSpec


@dataclass(frozen=True)
class Skill:
    """从一个 SKILL.md 解析出来的不可变技能定义。"""

    name: str
    description: str
    instructions: str
    path: Path
    allowed_tools: tuple[str, ...] = ()


class SkillFrontmatterParser:
    """解析简化版 YAML frontmatter，不依赖完整 YAML 库。"""

    @staticmethod
    def parse(path: str | Path) -> Skill:
        skill_path = Path(path)
        text = skill_path.read_text(encoding="utf-8")
        # 文件必须以 --- 开头，否则无法区分元数据和正文。
        if not text.startswith("---\n"):
            raise ValueError(f"{skill_path} has no frontmatter")
        try:
            header, body = text[4:].split("\n---\n", 1)
        except ValueError as exc:
            raise ValueError(f"{skill_path} has unclosed frontmatter") from exc

        # 只支持“单行 key: value”，不支持嵌套 YAML、多行值等语法。
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
        # allowed-tools 形如 [read_file, execute_command]，这里手工拆分。
        raw_tools = metadata.get("allowed-tools", "")
        tools = tuple(
            item.strip()
            for item in raw_tools.strip("[]").split(",")
            if item.strip()
        )
        return Skill(name, description, body.strip(), skill_path, tools)


class SkillRegistry:
    """从多个目录发现技能，并按 name 提供查询。"""

    def __init__(self, roots: Iterable[str | Path]) -> None:
        self.roots = [Path(root) for root in roots]
        self._skills: dict[str, Skill] = {}

    def discover(self) -> list[Skill]:
        discovered: dict[str, Skill] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            # rglob 会递归扫描子目录；sorted 保证测试和输出稳定。
            for path in sorted(root.rglob("SKILL.md")):
                skill = SkillFrontmatterParser.parse(path)
                # 同名技能后发现的会覆盖先发现的，当前没有冲突告警。
                discovered[skill.name] = skill
        self._skills = discovered
        return list(discovered.values())

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise KeyError(f"unknown skill: {name}") from exc

    def index(self) -> str:
        # 索引仅包含短描述，完整 instructions 等选中后再加载。
        return "\n".join(
            f"- {skill.name}: {skill.description}"
            for skill in sorted(self._skills.values(), key=lambda item: item.name)
        )


class SkillContextBuffer:
    """记录当前会话已加载的技能，避免重复注入。"""

    def __init__(self) -> None:
        self.loaded: set[str] = set()

    def load(self, skill: Skill) -> str:
        if skill.name in self.loaded:
            return f"Skill {skill.name!r} is already loaded."
        self.loaded.add(skill.name)
        # XML 样式边界让模型更容易区分技能指令和普通对话。
        return (
            f"<skill name=\"{skill.name}\">\n"
            f"{skill.instructions}\n"
            "</skill>"
        )

    def clear(self) -> None:
        self.loaded.clear()


class SkillStateStore:
    """用 JSON 保存“哪些技能已启用”，方便下次启动恢复。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save_enabled(self, names: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            # set 不能直接 JSON 序列化，先排序也能使文件结果稳定。
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
    """注册 load_skill 工具，并返回可供外部观察的上下文缓冲区。"""

    context = buffer or SkillContextBuffer()
    tools.register(
        ToolSpec(
            "load_skill",
            "Load detailed instructions for one relevant skill.",
            tools.object_schema(
                {
                    "name": {
                        "type": "string",
                        # enum 把合法名称直接告诉模型，减少幻觉出不存在的技能。
                        "enum": sorted(skills._skills),
                    }
                },
                required=["name"],
            ),
            # 执行时才从注册表取出完整指令，这就是懒加载的落点。
            lambda arguments: context.load(skills.get(str(arguments["name"]))),
        )
    )
    return context
