from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.safety import RollbackPolicy, WorkspaceRunGuard
from paicli.snapshot import SnapshotService


class WorkspaceRunGuardTest(unittest.TestCase):
    def test_success_keeps_changes_and_creates_before_after_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            guard = WorkspaceRunGuard(
                SnapshotService(root),
                rollback_policy=RollbackPolicy.ALWAYS,
            )

            def succeed() -> int:
                target.write_text("after\n", encoding="utf-8")
                return 0

            code = guard.run(succeed)

            self.assertEqual(0, code)
            self.assertEqual("after\n", target.read_text())
            self.assertIsNotNone(guard.last_record)
            self.assertTrue(guard.last_record.after_snapshot_id)  # type: ignore[union-attr]
            self.assertFalse(guard.last_record.rolled_back)  # type: ignore[union-attr]

    def test_failed_run_restores_modified_and_created_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "app.py"
            created = root / "new.py"
            original.write_text("before\n", encoding="utf-8")
            guard = WorkspaceRunGuard(
                SnapshotService(root),
                rollback_policy=RollbackPolicy.ALWAYS,
            )

            def fail() -> int:
                original.write_text("broken\n", encoding="utf-8")
                created.write_text("partial\n", encoding="utf-8")
                return 1

            code = guard.run(fail)

            self.assertEqual(1, code)
            self.assertEqual("before\n", original.read_text())
            self.assertFalse(created.exists())
            self.assertTrue(guard.last_record.rolled_back)  # type: ignore[union-attr]

    def test_never_policy_keeps_failed_state_and_after_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("before\n", encoding="utf-8")
            guard = WorkspaceRunGuard(
                SnapshotService(root),
                rollback_policy=RollbackPolicy.NEVER,
            )

            code = guard.run(
                lambda: target.write_text("broken\n", encoding="utf-8") and 1
            )

            self.assertEqual(1, code)
            self.assertEqual("broken\n", target.read_text())
            self.assertTrue(guard.last_record.after_snapshot_id)  # type: ignore[union-attr]
            self.assertFalse(guard.last_record.rolled_back)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
