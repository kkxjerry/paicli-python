"""Phase 18：独立于 Git 历史的可恢复文件快照。

在 Agent 改文件前记录 BEFORE 快照，出问题后可把文件恢复到当时的字节。
快照保存在 .paicli/snapshots 或指定目录，不创建 Git commit，也不修改 Git 对象库。
它只处理显式传入的文件，不是整个项目的完整备份。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable


class SnapshotPhase(str, Enum):
    """标记快照是一轮修改前还是修改后捕获。"""

    BEFORE = "before"
    AFTER = "after"


@dataclass(frozen=True)
class TurnSnapshot:
    """快照元数据；files 的 value 是 Base64 文件内容或“当时不存在”。"""

    id: str
    phase: SnapshotPhase
    created_at: float
    files: dict[str, str | None]


@dataclass(frozen=True)
class RestoreResult:
    """恢复过程中被写回和被删除的相对路径。"""

    restored: tuple[str, ...]
    removed: tuple[str, ...]


class SnapshotService:
    """在工作树的 Git 对象库之外保存文件字节。"""

    def __init__(
        self,
        project_root: str | Path,
        store: str | Path | None = None,
        *,
        max_file_bytes: int = 5_000_000,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = (
            Path(store).resolve()
            if store
            else self.project_root / ".paicli" / "snapshots"
        )
        self.max_file_bytes = max_file_bytes

    def capture(
        self,
        paths: Iterable[str],
        phase: SnapshotPhase,
    ) -> TurnSnapshot:
        """捕获指定路径的当前状态，并将一个 JSON 快照落盘。"""

        files: dict[str, str | None] = {}
        for raw_path in paths:
            path = self._safe_path(raw_path)
            if not path.exists():
                # None 不是“忽略”，而是记住该文件在快照时尚未存在。
                files[raw_path] = None
                continue
            if not path.is_file():
                raise ValueError(f"snapshot path is not a file: {raw_path}")
            # 按 bytes 读取，所以二进制文件和不同文本编码都可精确恢复。
            data = path.read_bytes()
            if len(data) > self.max_file_bytes:
                raise ValueError(f"snapshot file is too large: {raw_path}")
            # JSON 不能直接放 bytes，因此使用 Base64 编码成 ASCII 字符串。
            files[raw_path] = base64.b64encode(data).decode("ascii")

        snapshot = TurnSnapshot(uuid.uuid4().hex, phase, time.time(), files)
        self.store.mkdir(parents=True, exist_ok=True)
        payload = asdict(snapshot)
        # Enum 显式转成 before/after，避免 JSON 序列化失败。
        payload["phase"] = phase.value
        (self.store / f"{snapshot.id}.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        return snapshot

    def load(self, snapshot_id: str) -> TurnSnapshot:
        """按 id 读取 JSON，并把 phase 还原为枚举。"""

        path = self.store / f"{snapshot_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return TurnSnapshot(
            payload["id"],
            SnapshotPhase(payload["phase"]),
            float(payload["created_at"]),
            dict(payload["files"]),
        )

    def restore(self, snapshot_id: str) -> RestoreResult:
        """将工作树恢复到该快照所记录的文件状态。"""

        snapshot = self.load(snapshot_id)
        restored: list[str] = []
        removed: list[str] = []
        for raw_path, encoded in snapshot.files.items():
            path = self._safe_path(raw_path)
            if encoded is None:
                # 快照时不存在、现在却存在：说明它是后续新建文件，需删除。
                if path.is_file():
                    path.unlink()
                    removed.append(raw_path)
                continue
            # 原文件的父目录也可能被删，写回前先重建。
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded))
            restored.append(raw_path)
        return RestoreResult(tuple(restored), tuple(removed))

    def list_snapshots(self) -> list[TurnSnapshot]:
        """按创建时间从旧到新列出快照。"""

        if not self.store.is_dir():
            return []
        snapshots = [self.load(path.stem) for path in self.store.glob("*.json")]
        return sorted(snapshots, key=lambda item: item.created_at)

    def _safe_path(self, raw_path: str) -> Path:
        # 与 read_file/write_file 一样，拒绝 ../ 和符号链接绕出项目根目录。
        path = (self.project_root / raw_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("snapshot path escapes project root")
        return path
