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
        # 使用临时目录，测试结束后自动删除记忆文件，不污染真实项目。
        with tempfile.TemporaryDirectory() as directory:
            memory = LongTermMemory(Path(directory, "memory.jsonl"))
            # 两次 save 会向 JSONL 追加两行，而不是彼此覆盖。
            memory.save("The project uses SQLite", ("database",))
            memory.save("The UI uses green buttons", ("frontend",))

            # database 命中第一条记忆的 tag，第二条不相关。
            matches = memory.search("Which database does the project use?")

            self.assertEqual(1, len(matches))
            self.assertIn("SQLite", matches[0].content)

    def test_context_is_compacted_when_over_budget(self) -> None:
        # 构造 1 条 system + 10 条 user 消息，故意超过后面的 5-token 预算。
        messages = [
            {"role": "system", "content": "rules"},
            *[
                {"role": "user", "content": f"message {number} with several words"}
                for number in range(10)
            ],
        ]
        manager = MemoryManager(max_tokens=5, compressor=ContextCompressor())

        prepared = manager.prepare(messages)

        # 旧消息被一条摘要代替。
        self.assertIn("Summary of earlier conversation", prepared[0]["content"])
        # 最新消息仍保留原文，保证最近的上下文不丢失。
        self.assertEqual(messages[-1], prepared[-1])
        # 摘要后的消息总数应少于原始消息数。
        self.assertLess(len(prepared), len(messages))

    def test_token_estimate_handles_chinese_and_ascii(self) -> None:
        # hello/Python 约 2 个 token，“你好”按 2 个非 ASCII 字符计算。
        self.assertGreaterEqual(estimate_tokens("hello Python 你好"), 4)


if __name__ == "__main__":
    unittest.main()
