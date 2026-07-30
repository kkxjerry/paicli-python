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
        with tempfile.TemporaryDirectory() as directory:
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

            prompt = assembler.assemble(
                PromptContext(
                    mode=PromptMode.PLAN,
                    project_root=root,
                    skill_instructions=("Run tests.",),
                    resource_index=("docs:file://guide",),
                    runtime_notes=("Shell is disabled.",),
                )
            )

            positions = [
                prompt.index(f"<{name}>")
                for name in ("base", "mode", "project", "skills", "resources", "runtime")
            ]
            self.assertEqual(sorted(positions), positions)
            self.assertIn("Follow project rules.", prompt)


if __name__ == "__main__":
    unittest.main()
