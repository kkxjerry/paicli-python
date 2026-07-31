from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.skills import (
    SkillContextBuffer,
    SkillRegistry,
    SkillStateStore,
    register_skill_tool,
)
from paicli.tools import ToolRegistry


class SkillsTest(unittest.TestCase):
    def test_discovers_and_lazily_loads_skill(self) -> None:
        """技能可从磁盘发现，但详细指令只在首次调用工具时加载。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：现场创建一个最小 SKILL.md，不依赖项目中的真实技能文件。
            root = Path(directory)
            skill_dir = root / "skills" / "testing"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: testing\n"
                "description: Run focused tests\n"
                "allowed-tools: [read_file, execute_command]\n"
                "---\n"
                "Always run the smallest relevant test first.\n",
                encoding="utf-8",
            )
            skills = SkillRegistry([root / "skills"])
            skills.discover()
            tools = ToolRegistry(root)
            buffer = register_skill_tool(tools, skills)

            # Act：对同一技能连续加载两次。
            first = tools.execute("load_skill", '{"name":"testing"}')
            second = tools.execute("load_skill", '{"name":"testing"}')

            # Assert：首次返回正文，第二次阻止重复注入。
            self.assertIn("smallest relevant test", first)
            self.assertIn("already loaded", second)
            self.assertIn("testing", buffer.loaded)

    def test_state_store_round_trips_enabled_skills(self) -> None:
        """启用状态经过“写 JSON -> 读 JSON”后不丢失。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange + Act：写入一个无序 set。
            store = SkillStateStore(Path(directory, "state.json"))
            store.save_enabled({"web", "testing"})

            # Assert：读回后仍是相同集合。
            self.assertEqual({"web", "testing"}, store.load_enabled())

    def test_context_buffer_can_be_cleared(self) -> None:
        """新会话开始时可清空“已加载”标记。"""

        # Arrange：模拟已加载 demo。
        buffer = SkillContextBuffer()
        buffer.loaded.add("demo")
        # Act + Assert：清空后集合应为空。
        buffer.clear()
        self.assertEqual(set(), buffer.loaded)


if __name__ == "__main__":
    unittest.main()
