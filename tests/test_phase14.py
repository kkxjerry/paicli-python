from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.browser import BrowserMode, BrowserSessionManager
from paicli.memory import LongTermMemory, register_memory_tool
from paicli.tools import ToolRegistry


class Phase14Test(unittest.TestCase):
    def test_reuses_cdp_session_for_same_endpoint(self) -> None:
        manager = BrowserSessionManager()

        first = manager.connect("http://127.0.0.1:9222")
        second = manager.connect("http://127.0.0.1:9222")
        isolated = manager.connect(
            "http://127.0.0.1:9222",
            mode=BrowserMode.ISOLATED,
        )

        self.assertIs(first, second)
        self.assertNotEqual(first.id, isolated.id)
        self.assertEqual(2, len(manager.active_sessions()))

    def test_save_memory_tool_persists_explicit_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = LongTermMemory(Path(directory, "memory.jsonl"))
            registry = ToolRegistry(directory)
            register_memory_tool(registry, memory)

            result = registry.execute(
                "save_memory",
                '{"content":"Prefer Python","tags":["preference"]}',
            )

            self.assertEqual("Memory saved.", result)
            self.assertEqual("Prefer Python", memory.entries()[0].content)


if __name__ == "__main__":
    unittest.main()
