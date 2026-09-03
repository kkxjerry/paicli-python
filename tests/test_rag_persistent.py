from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.managed_memory import ManagedLongTermMemory, MemoryStatus
from paicli.rag import CodeIndex
from paicli.tools import ToolRegistry


class PersistentRagTest(unittest.TestCase):
    def test_incremental_index_returns_exact_symbol_and_source_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "math_utils.py").write_text(
                "def add(a, b):\n    return a + b\n\n"
                "def calculate_total(values):\n    return sum(values)\n",
                encoding="utf-8",
            )
            index = CodeIndex(root, root / ".paicli" / "code.db")
            try:
                first = index.build()
                result = index.search("calculate_total", 1)[0]
                second = index.build()
            finally:
                index.close()

            self.assertEqual(1, first["changed"])
            self.assertEqual(0, second["changed"])
            self.assertEqual("calculate_total", result.symbol)
            self.assertEqual("math_utils.py", result.path)
            self.assertIn("symbol", result.channels)
            self.assertGreaterEqual(result.start_line, 1)

    def test_removed_and_changed_files_are_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "service.py"
            source.write_text("def old_name():\n    return 1\n", encoding="utf-8")
            index = CodeIndex(root, root / ".paicli" / "code.db")
            try:
                index.build()
                source.write_text("def new_name():\n    return 2\n", encoding="utf-8")
                changed = index.build()
                self.assertFalse(index.search("old_name", 5))
                self.assertEqual("new_name", index.search("new_name", 1)[0].symbol)
                source.unlink()
                removed = index.build()
                self.assertEqual([], index.search("new_name", 5))
            finally:
                index.close()
            self.assertEqual(1, changed["changed"])
            self.assertEqual(1, removed["removed"])

    def test_search_tool_returns_evidence_not_only_a_similarity_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "auth.py").write_text(
                "def validate_token(token: str) -> bool:\n    return bool(token)\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(root)
            index = CodeIndex(root)
            try:
                index.build()
                index.register_tool(registry)
                result = registry.execute(
                    "search_code",
                    '{"query":"validate token","top_k":1}',
                )
            finally:
                index.close()
            self.assertIn("SOURCE auth.py:L", result)
            self.assertIn("def validate_token", result)


class ManagedMemoryTest(unittest.TestCase):
    def test_dedup_verify_stale_and_delete_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagedLongTermMemory(Path(directory, "memory.db"))
            try:
                first = store.save(
                    "The project uses SQLite",
                    ("database",),
                    source="README.md",
                    source_hash="v1",
                )
                duplicate = store.save(
                    "  the project uses sqlite  ",
                    ("architecture",),
                    source="README.md",
                    source_hash="v1",
                )
                self.assertEqual(first.id, duplicate.id)
                self.assertEqual(MemoryStatus.UNVERIFIED, store.status(first.id))
                store.verify(first.id)
                self.assertEqual(MemoryStatus.VERIFIED, store.status(first.id))
                self.assertEqual(1, len(store.search("database sqlite")))
                self.assertEqual(1, store.mark_stale("README.md", "v2"))
                self.assertEqual(MemoryStatus.STALE, store.status(first.id))
                self.assertEqual([], store.search("database sqlite"))
                store.delete(first.id)
                self.assertEqual(MemoryStatus.DELETED, store.status(first.id))
            finally:
                store.close()

    def test_unverified_memory_is_excluded_unless_explicitly_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagedLongTermMemory(Path(directory, "memory.db"))
            try:
                unverified = store.save("database uses postgres", ("database",))
                verified = store.save("database uses sqlite", ("database",), verified=True)
                trusted = store.search("database uses", limit=2)
                inspected = store.search(
                    "database uses",
                    limit=2,
                    include_unverified=True,
                )
            finally:
                store.close()
            self.assertEqual([verified.id], [item.id for item in trusted])
            self.assertEqual(verified.id, inspected[0].id)
            self.assertEqual(unverified.id, inspected[1].id)


if __name__ == "__main__":
    unittest.main()
