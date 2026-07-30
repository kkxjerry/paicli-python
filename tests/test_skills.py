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
        with tempfile.TemporaryDirectory() as directory:
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

            first = tools.execute("load_skill", '{"name":"testing"}')
            second = tools.execute("load_skill", '{"name":"testing"}')

            self.assertIn("smallest relevant test", first)
            self.assertIn("already loaded", second)
            self.assertIn("testing", buffer.loaded)

    def test_state_store_round_trips_enabled_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SkillStateStore(Path(directory, "state.json"))
            store.save_enabled({"web", "testing"})

            self.assertEqual({"web", "testing"}, store.load_enabled())

    def test_context_buffer_can_be_cleared(self) -> None:
        buffer = SkillContextBuffer()
        buffer.loaded.add("demo")
        buffer.clear()
        self.assertEqual(set(), buffer.loaded)


if __name__ == "__main__":
    unittest.main()
