from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.memory import (
    ContextCompressor,
    LongTermMemory,
    MemoryManager,
    estimate_tokens,
)


class MemoryTest(unittest.TestCase):
    def test_long_term_memory_persists_and_retrieves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = LongTermMemory(Path(directory, "memory.jsonl"))
            memory.save("The project uses SQLite", ("database",))
            memory.save("The UI uses green buttons", ("frontend",))

            matches = memory.search("Which database does the project use?")

            self.assertEqual(1, len(matches))
            self.assertIn("SQLite", matches[0].content)

    def test_context_is_compacted_when_over_budget(self) -> None:
        messages = [
            {"role": "system", "content": "rules"},
            *[
                {"role": "user", "content": f"message {number} with several words"}
                for number in range(10)
            ],
        ]
        manager = MemoryManager(max_tokens=5, compressor=ContextCompressor())

        prepared = manager.prepare(messages)

        self.assertIn("Summary of earlier conversation", prepared[0]["content"])
        self.assertEqual(messages[-1], prepared[-1])
        self.assertLess(len(prepared), len(messages))

    def test_token_estimate_handles_chinese_and_ascii(self) -> None:
        self.assertGreaterEqual(estimate_tokens("hello Python 你好"), 4)


if __name__ == "__main__":
    unittest.main()
