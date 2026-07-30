from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.interaction import (
    CliCommandParser,
    PaiCliHistory,
    StatusDock,
    normalize_input,
)


class InteractionTest(unittest.TestCase):
    def test_parses_commands_without_confusing_regular_prompts(self) -> None:
        command = CliCommandParser.parse("/model deepseek")

        self.assertEqual("model", command.name)  # type: ignore[union-attr]
        self.assertEqual(("deepseek",), command.arguments)  # type: ignore[union-attr]
        self.assertIsNone(CliCommandParser.parse("explain /model"))

    def test_history_persists_and_deduplicates_adjacent_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "history.json")
            history = PaiCliHistory(path)
            history.add("first")
            history.add("first")
            history.add("second")

            loaded = PaiCliHistory(path)

            self.assertEqual(["first", "second"], loaded.entries)

    def test_status_dock_never_exceeds_terminal_width(self) -> None:
        dock = StatusDock("deepseek", "a-very-long-model-name", activity="thinking")

        self.assertEqual(20, len(dock.render(20)))
        self.assertEqual("hello", normalize_input("\r\nhello\r\n"))


if __name__ == "__main__":
    unittest.main()
