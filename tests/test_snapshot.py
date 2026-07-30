from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.snapshot import SnapshotPhase, SnapshotService


class SnapshotTest(unittest.TestCase):
    def test_restores_modified_and_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Path(directory, ".side-snapshots")
            existing = root / "existing.txt"
            created = root / "created.txt"
            existing.write_text("before", encoding="utf-8")
            service = SnapshotService(root, store)
            snapshot = service.capture(
                ["existing.txt", "created.txt"],
                SnapshotPhase.BEFORE,
            )
            existing.write_text("after", encoding="utf-8")
            created.write_text("new", encoding="utf-8")

            result = service.restore(snapshot.id)

            self.assertEqual("before", existing.read_text(encoding="utf-8"))
            self.assertFalse(created.exists())
            self.assertIn("existing.txt", result.restored)
            self.assertIn("created.txt", result.removed)

    def test_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                SnapshotService(directory).capture(
                    ["../outside"],
                    SnapshotPhase.BEFORE,
                )


if __name__ == "__main__":
    unittest.main()
