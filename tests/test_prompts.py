from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.prompts import (
    PromptAssembler,
    PromptContext,
    PromptMode,
    PromptRepository,
)


class PromptsTest(unittest.TestCase):
    def test_assembles_layers_in_stable_order(self) -> None:
        """六层 prompt 必须按规定顺序出现，并读入项目级 AGENTS.md。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：建立项目指令，并准备 base + plan 模式提示词。
            root = Path(directory)
            (root / "AGENTS.md").write_text(
                "Follow project rules.",
                encoding="utf-8",
            )
            assembler = PromptAssembler(
                PromptRepository(
                    "You are PaiCLI.",
                    {PromptMode.PLAN: "Create a plan before editing."},
                )
            )

            # Act：六层均提供内容，便于完整验证顺序。
            prompt = assembler.assemble(
                PromptContext(
                    mode=PromptMode.PLAN,
                    project_root=root,
                    skill_instructions=("Run tests.",),
                    resource_index=("docs:file://guide",),
                    runtime_notes=("Shell is disabled.",),
                )
            )

            # Assert：取每个开始标签的位置，若本来就递增，排序后应不变。
            positions = [
                prompt.index(f"<{name}>")
                for name in ("base", "mode", "project", "skills", "resources", "runtime")
            ]
            self.assertEqual(sorted(positions), positions)
            self.assertIn("Follow project rules.", prompt)


if __name__ == "__main__":
    unittest.main()
