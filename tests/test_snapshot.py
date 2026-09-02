from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.snapshot import SnapshotPhase, SnapshotService


class SnapshotTest(unittest.TestCase):
    def test_restores_modified_and_new_files(self) -> None:
        """恢复 BEFORE 快照时：旧文件还原，修改后才新建的文件删除。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：existing 快照前存在，created 快照前不存在。
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
            # 模拟 Agent 完成一轮修改：旧文件改了，又新建一个文件。
            existing.write_text("after", encoding="utf-8")
            created.write_text("new", encoding="utf-8")

            # Act：恢复到修改前。
            result = service.restore(snapshot.id)

            # Assert：验证磁盘实际状态和结构化操作报告。
            self.assertEqual("before", existing.read_text(encoding="utf-8"))
            self.assertFalse(created.exists())
            self.assertIn("existing.txt", result.restored)
            self.assertIn("created.txt", result.removed)

    def test_tree_snapshot_restores_project_and_removes_later_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("before\n", encoding="utf-8")
            (root / ".paicli").mkdir()
            (root / ".paicli" / "state.json").write_text("keep", encoding="utf-8")
            service = SnapshotService(root)
            snapshot = service.capture_tree(SnapshotPhase.BEFORE)

            (root / "src" / "app.py").write_text("after\n", encoding="utf-8")
            (root / "src" / "new.py").write_text("new\n", encoding="utf-8")
            (root / ".paicli" / "state.json").write_text("new state", encoding="utf-8")

            result = service.restore_tree(snapshot.id)

            self.assertEqual("before\n", (root / "src" / "app.py").read_text())
            self.assertFalse((root / "src" / "new.py").exists())
            self.assertEqual(
                "new state",
                (root / ".paicli" / "state.json").read_text(),
            )
            self.assertIn("src/app.py", result.restored)
            self.assertIn("src/new.py", result.removed)

    def test_tree_snapshot_skips_total_limit_without_blocking_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "large.txt"
            target.write_text("123456", encoding="utf-8")
            service = SnapshotService(root, max_total_bytes=5)

            snapshot = service.capture_tree(SnapshotPhase.BEFORE)
            target.write_text("changed", encoding="utf-8")
            result = service.restore_tree(snapshot.id)

            self.assertIn("large.txt", snapshot.skipped)
            self.assertIn("total-byte limit", snapshot.skipped["large.txt"])
            self.assertEqual("changed", target.read_text(encoding="utf-8"))
            self.assertIn("large.txt", result.skipped)

    def test_tree_snapshot_records_symbolic_link_to_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory)
            root = outer / "project"
            root.mkdir()
            outside = outer / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            (root / "escape.txt").symlink_to(outside)

            service = SnapshotService(root)
            snapshot = service.capture_tree(SnapshotPhase.BEFORE)
            result = service.restore_tree(snapshot.id)

            self.assertIn("escape.txt", snapshot.skipped)
            self.assertIn("symbolic link escapes", snapshot.skipped["escape.txt"])
            self.assertTrue((root / "escape.txt").is_symlink())
            self.assertIn("escape.txt", result.skipped)

    def test_tree_snapshot_skips_oversized_file_and_preserves_it_on_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "large.bin"
            target.write_bytes(b"123456")
            service = SnapshotService(root, max_file_bytes=5)
            snapshot = service.capture_tree(SnapshotPhase.BEFORE)

            target.write_bytes(b"changed")
            service.restore_tree(snapshot.id)

            self.assertIn("large.bin", snapshot.skipped)
            self.assertEqual(b"changed", target.read_bytes())

    def test_snapshot_prune_preserves_explicit_and_recent_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = SnapshotService(root)
            snapshots = [
                service.capture_tree(SnapshotPhase.BEFORE)
                for _ in range(4)
            ]

            removed = service.prune(
                keep_ids={snapshots[0].id},
                keep_last=1,
            )

            self.assertEqual(
                {snapshots[1].id, snapshots[2].id},
                set(removed),
            )
            self.assertTrue((service.store / f"{snapshots[0].id}.json").is_file())
            self.assertTrue((service.store / f"{snapshots[3].id}.json").is_file())

    def test_tree_snapshot_restores_internal_symbolic_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            link = root / "current.txt"
            target.write_text("before", encoding="utf-8")
            link.symlink_to("target.txt")
            service = SnapshotService(root)
            snapshot = service.capture_tree(SnapshotPhase.BEFORE)

            link.unlink()
            link.write_text("replacement", encoding="utf-8")
            target.write_text("after", encoding="utf-8")
            service.restore_tree(snapshot.id)

            self.assertTrue(link.is_symlink())
            self.assertEqual("target.txt", str(link.readlink()))
            self.assertEqual("before", target.read_text(encoding="utf-8"))

    def test_rejects_path_escape(self) -> None:
        """快照不允许读取项目根目录之外的文件。"""

        with tempfile.TemporaryDirectory() as directory:
            # Act + Assert：../outside resolve 后不在 root 内，必须报错。
            with self.assertRaises(ValueError):
                SnapshotService(directory).capture(
                    ["../outside"],
                    SnapshotPhase.BEFORE,
                )


if __name__ == "__main__":
    unittest.main()
