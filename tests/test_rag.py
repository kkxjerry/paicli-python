from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.rag import CodeIndex
from paicli.tools import ToolRegistry


class RagTest(unittest.TestCase):
    def test_indexes_symbols_and_retrieves_relevant_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "billing.py").write_text(
                "def calculate_invoice_total(items):\n"
                "    return sum(item.price for item in items)\n\n"
                "def send_email(address):\n"
                "    return address\n",
                encoding="utf-8",
            )
            index = CodeIndex(root)

            count = index.rebuild()
            results = index.search("calculate invoice total")

            self.assertEqual(2, count)
            self.assertEqual("calculate_invoice_total", results[0].chunk.symbol)

    def test_registers_search_code_as_agent_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text(
                "def authenticate_user(token):\n    return bool(token)\n",
                encoding="utf-8",
            )
            index = CodeIndex(root)
            index.rebuild()
            registry = ToolRegistry(root)
            index.register_tool(registry)

            result = registry.execute(
                "search_code",
                '{"query":"authenticate user"}',
            )

            self.assertIn("authenticate_user", result)
            self.assertIn("search_code", registry.names())


if __name__ == "__main__":
    unittest.main()
