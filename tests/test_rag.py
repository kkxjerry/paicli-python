from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.rag import CodeIndex
from paicli.tools import ToolRegistry


class RagTest(unittest.TestCase):
    """测试代码索引的两层能力：先验证检索本身，再验证它能作为 Agent 工具调用。"""

    def test_indexes_symbols_and_retrieves_relevant_code(self) -> None:
        """验证 Python 文件能按函数分块，并把与查询最相关的函数排在第一位。

        流程：写入两个函数 -> rebuild() 建索引 -> 搜索 calculate invoice total -> 命中同名函数。
        """

        # Arrange（准备）：临时创建一个只有 billing.py 的小项目。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 文件内有两个顶层函数，AST 应将它们切成两个 CodeChunk。
            (root / "billing.py").write_text(
                "def calculate_invoice_total(items):\n"
                "    return sum(item.price for item in items)\n\n"
                "def send_email(address):\n"
                "    return address\n",
                encoding="utf-8",
            )
            index = CodeIndex(root)

            # Act（执行）：先建索引，再用拆开后的函数名搜索。
            count = index.rebuild()
            results = index.search("calculate invoice total")

            # Assert 1：两个顶层函数应产生两个代码块。
            self.assertEqual(2, count)
            # Assert 2：查询中的三个词与 calculate_invoice_total 完全重合，它应排名第一。
            self.assertEqual("calculate_invoice_total", results[0].chunk.symbol)

    def test_registers_search_code_as_agent_tool(self) -> None:
        """验证 CodeIndex 可以注册为 ToolRegistry 中的 search_code 工具。

        流程：建立代码索引 -> 注册工具 -> 通过统一 execute 入口调用 -> 获得代码文本。
        """

        # Arrange（准备）：创建包含 authenticate_user 的代码并完成索引。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text(
                "def authenticate_user(token):\n    return bool(token)\n",
                encoding="utf-8",
            )
            index = CodeIndex(root)
            index.rebuild()
            # ToolRegistry 原本只有文件等内置工具，register_tool 会向它加入 search_code。
            registry = ToolRegistry(root)
            index.register_tool(registry)

            # Act（执行）：模拟 Agent 使用 JSON 参数调用 search_code。
            result = registry.execute(
                "search_code",
                '{"query":"authenticate user"}',
            )

            # Assert 1：工具返回的文本应包含命中的函数名。
            self.assertIn("authenticate_user", result)
            # Assert 2：工具名确实已经加入注册表。
            self.assertIn("search_code", registry.names())


if __name__ == "__main__":
    unittest.main()
