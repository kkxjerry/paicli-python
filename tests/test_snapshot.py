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
