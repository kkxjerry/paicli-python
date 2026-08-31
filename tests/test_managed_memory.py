from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paicli.managed_memory import (
    ManagedMemoryStore,
    MemoryKind,
    MemoryStatus,
)


class ManagedMemoryTest(unittest.TestCase):
    def test_deduplicates_repeated_write_and_preserves_highest_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagedMemoryStore(Path(directory, "memory.db"))
            try:
                first = store.save_record(
                    "The project uses SQLite",
                    tags=("database",),
                    confidence=0.6,
                )
                second = store.save_record(
                    "The project uses SQLite",
                    tags=("database",),
                    confidence=0.9,
                )

                self.assertEqual(first.id, second.id)
                self.assertEqual(1, len(store.entries()))
                self.assertEqual(0.9, store.get(first.id).confidence)
            finally:
                store.close()

    def test_new_source_hash_marks_old_source_memory_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagedMemoryStore(Path(directory, "memory.db"))
            try:
                old = store.save_record(
                    "Timeout is 30 seconds",
                    source="config.py",
                    source_hash="old",
                )
                current = store.save_record(
                    "Timeout is 60 seconds",
                    source="config.py",
                    source_hash="new",
                )

                self.assertEqual(MemoryStatus.STALE, store.get(old.id).status)
                self.assertEqual(MemoryStatus.ACTIVE, store.get(current.id).status)
                matches = store.search("timeout seconds", 10)
                self.assertEqual([current.id], [item.id for item in matches])
            finally:
                store.close()

    def test_explicit_supersede_and_conflict_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagedMemoryStore(Path(directory, "memory.db"))
            try:
                old = store.save_record(
                    "Use Python 3.11",
                    kind=MemoryKind.DECISION,
                )
                new = store.save_record(
                    "Use Python 3.12",
                    kind=MemoryKind.DECISION,
                    supersedes_id=old.id,
                )
                alternative = store.save_record(
                    "Use Python 3.13",
                    kind=MemoryKind.DECISION,
                )

                store.resolve_conflict(new.id, [alternative.id])

                self.assertEqual(MemoryStatus.SUPERSEDED, store.get(old.id).status)
                self.assertEqual(
                    MemoryStatus.SUPERSEDED,
                    store.get(alternative.id).status,
                )
                self.assertEqual(MemoryStatus.ACTIVE, store.get(new.id).status)
            finally:
                store.close()

    def test_delete_removes_memory_from_default_read_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagedMemoryStore(Path(directory, "memory.db"))
            try:
                record = store.save("User prefers concise answers", ("preference",))
                store.delete(record.id)

                self.assertEqual([], store.entries())
                self.assertEqual([], store.search("concise answers"))
                self.assertEqual(1, store.stats()[MemoryStatus.DELETED.value])
            finally:
                store.close()

    def test_imports_legacy_jsonl_without_duplicate_active_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.jsonl"
            line = {
                "content": "The API uses JSON",
                "tags": ["api"],
                "created_at": 1.0,
            }
            source.write_text(
                json.dumps(line) + "\n" + json.dumps(line) + "\n",
                encoding="utf-8",
            )
            store = ManagedMemoryStore(root / "memory.db")
            try:
                self.assertEqual(2, store.import_jsonl(source))
                self.assertEqual(1, len(store.entries()))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
