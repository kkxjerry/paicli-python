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
    """按“准备数据 -> 调用被测代码 -> 断言结果”的顺序测试 Memory。"""

    def test_long_term_memory_persists_and_retrieves(self) -> None:
        """验证记忆写入 JSONL 后可以按关键词/标签找回。

        流程：保存两条记忆 -> 用 database 搜索 -> 只找到 SQLite 那条。
        """

        # Arrange（准备）：使用临时目录，测试结束后自动删，不污染真实项目。
        with tempfile.TemporaryDirectory() as directory:
            # 临时目录内的 memory.jsonl 就是这次测试的长期记忆库。
            memory = LongTermMemory(Path(directory, "memory.jsonl"))
            # 两次 save 会向 JSONL 追加两行，而不是彼此覆盖。
            memory.save("The project uses SQLite", ("database",))
            memory.save("The UI uses green buttons", ("frontend",))

            # Act（执行）：search() 重读 JSONL 并计算交集。database 只命中第一条的 tag。
            matches = memory.search("Which database does the project use?")

            # Assert（验证）：应该只有一条匹配，而且内容确实是 SQLite。
            self.assertEqual(1, len(matches))
            self.assertIn("SQLite", matches[0].content)

    def test_context_is_compacted_when_over_budget(self) -> None:
        """验证对话超过 token 预算时，旧消息会被压缩但最新消息仍保留。

        流程：构造 11 条消息 -> 设置极小预算 -> prepare() -> 检查摘要和最新原文。
        """

        # Arrange（准备）：构造 1 条 system + 10 条 user 消息。
        messages = [
            {"role": "system", "content": "rules"},
            *[
                {"role": "user", "content": f"message {number} with several words"}
                for number in range(10)
            ],
        ]
        # max_tokens=5 足够小，可以稳定触发压缩，而不用依赖真实模型。
        manager = MemoryManager(max_tokens=5, compressor=ContextCompressor())

        # Act（执行）：准备本轮要发给 LLM 的消息。
        # compact() 默认保留最后 6 条，前 5 条会合并成 1 条摘要。
        prepared = manager.prepare(messages)

        # Assert 1：第一条已经不是原始 system 消息，而是旧消息摘要。
        self.assertIn("Summary of earlier conversation", prepared[0]["content"])
        # Assert 2：最后一条仍等于原始最新消息，说明近期细节没被摘要改写。
        self.assertEqual(messages[-1], prepared[-1])
        # Assert 3：11 条原始消息压缩后变成“1 条摘要 + 6 条原文”，数量应减少。
        self.assertLess(len(prepared), len(messages))

    def test_token_estimate_handles_chinese_and_ascii(self) -> None:
        """验证粗略 token 估算器能同时计算英文词和中文字符。"""

        # Arrange + Act：hello、Python 是 2 个 ASCII 词，“你好”是 2 个非 ASCII 字符。
        estimated = estimate_tokens("hello Python 你好")

        # Assert：当前算法结果是 4。用 >= 而不是 ==，允许以后将估算改得更保守。
        self.assertGreaterEqual(estimated, 4)


if __name__ == "__main__":
    unittest.main()
