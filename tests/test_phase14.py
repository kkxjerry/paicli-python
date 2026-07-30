from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.browser import BrowserMode, BrowserSessionManager
from paicli.memory import LongTermMemory, register_memory_tool
from paicli.tools import ToolRegistry


class Phase14Test(unittest.TestCase):
    def test_reuses_cdp_session_for_same_endpoint(self) -> None:
        """相同 endpoint 默认复用会话，ISOLATED 模式则必须创建新会话。"""

        # Arrange：创建空的会话管理器。
        manager = BrowserSessionManager()

        # Act：用默认 REUSE 连两次，再强制创建一个隔离会话。
        first = manager.connect("http://127.0.0.1:9222")
        second = manager.connect("http://127.0.0.1:9222")
        isolated = manager.connect(
            "http://127.0.0.1:9222",
            mode=BrowserMode.ISOLATED,
        )

        # Assert：不仅 id 相同，而且复用的就是同一个 Python 对象。
        self.assertIs(first, second)
        self.assertNotEqual(first.id, isolated.id)

    def test_save_memory_tool_persists_explicit_fact(self) -> None:
        """模型调用 save_memory 后，内容应真正写入 JSONL 长期记忆。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：内存管理器和工具注册表共用临时目录。
            memory = LongTermMemory(Path(directory, "memory.jsonl"))
            registry = ToolRegistry(directory)
            register_memory_tool(registry, memory)

            # Act：这条 JSON 模拟模型发出的 tool call arguments。
            result = registry.execute(
                "save_memory",
                '{"content":"Prefer Python","tags":["preference"]}',
            )

            # Assert：既校验工具返回值，也重新读取持久化结果。
            self.assertEqual("Memory saved.", result)
            self.assertEqual("Prefer Python", memory.entries()[0].content)


if __name__ == "__main__":
    unittest.main()
