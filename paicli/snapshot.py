"""Phase 18: side-store snapshots that never mutate the main Git history."""

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
    BEFORE = "before"
    AFTER = "after"


@dataclass(frozen=True)
class TurnSnapshot:
    id: str
    phase: SnapshotPhase
    created_at: float
    files: dict[str, str | None]


@dataclass(frozen=True)
class RestoreResult:
    restored: tuple[str, ...]
    removed: tuple[str, ...]


class SnapshotService:
    """Stores file bytes outside the worktree's Git object database."""

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
        files: dict[str, str | None] = {}
        for raw_path in paths:
            path = self._safe_path(raw_path)
            if not path.exists():
                files[raw_path] = None
                continue
            if not path.is_file():
                raise ValueError(f"snapshot path is not a file: {raw_path}")
            data = path.read_bytes()
            if len(data) > self.max_file_bytes:
                raise ValueError(f"snapshot file is too large: {raw_path}")
            files[raw_path] = base64.b64encode(data).decode("ascii")

        snapshot = TurnSnapshot(uuid.uuid4().hex, phase, time.time(), files)
        self.store.mkdir(parents=True, exist_ok=True)
        payload = asdict(snapshot)
        payload["phase"] = phase.value
        (self.store / f"{snapshot.id}.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        return snapshot

    def load(self, snapshot_id: str) -> TurnSnapshot:
        path = self.store / f"{snapshot_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return TurnSnapshot(
            payload["id"],
            SnapshotPhase(payload["phase"]),
            float(payload["created_at"]),
            dict(payload["files"]),
        )

    def restore(self, snapshot_id: str) -> RestoreResult:
        snapshot = self.load(snapshot_id)
        restored: list[str] = []
        removed: list[str] = []
        for raw_path, encoded in snapshot.files.items():
            path = self._safe_path(raw_path)
            if encoded is None:
                if path.is_file():
                    path.unlink()
                    removed.append(raw_path)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded))
            restored.append(raw_path)
        return RestoreResult(tuple(restored), tuple(removed))

    def list_snapshots(self) -> list[TurnSnapshot]:
        if not self.store.is_dir():
            return []
        snapshots = [self.load(path.stem) for path in self.store.glob("*.json")]
        return sorted(snapshots, key=lambda item: item.created_at)

    def _safe_path(self, raw_path: str) -> Path:
        path = (self.project_root / raw_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("snapshot path escapes project root")
        return path
