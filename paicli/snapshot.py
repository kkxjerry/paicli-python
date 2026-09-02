"""Bounded file and whole-workspace snapshots outside the Git object store.

Explicit snapshots preserve the original learning API. Tree snapshots add a
transaction boundary for complete Agent runs: files present at capture time are
restored byte-for-byte and later-created files are removed, while generated
state under ``.paicli`` and dependency/build directories is excluded.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
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
    tree: bool = False
    skipped: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RestoreResult:
    restored: tuple[str, ...]
    removed: tuple[str, ...]
    skipped: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


class SnapshotService:
    """Persist bounded byte-accurate snapshots outside repository history."""

    SYMLINK_PREFIX = "__PAICLI_SYMLINK__:"
    DEFAULT_IGNORED_DIRECTORIES = {
        ".git",
        ".paicli",
        ".venv",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
    }

    def __init__(
        self,
        project_root: str | Path,
        store: str | Path | None = None,
        *,
        max_file_bytes: int = 5_000_000,
        max_total_bytes: int = 50_000_000,
        max_files: int = 5_000,
        ignored_directories: set[str] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = (
            Path(store).resolve()
            if store
            else self.project_root / ".paicli" / "snapshots"
        )
        if max_file_bytes < 1 or max_total_bytes < 1 or max_files < 1:
            raise ValueError("snapshot limits must be positive")
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_files = max_files
        self.ignored_directories = (
            set(ignored_directories)
            if ignored_directories is not None
            else set(self.DEFAULT_IGNORED_DIRECTORIES)
        )

    def capture(
        self,
        paths: Iterable[str],
        phase: SnapshotPhase,
    ) -> TurnSnapshot:
        """Capture explicitly named files, including their non-existence."""

        files: dict[str, str | None] = {}
        total_bytes = 0
        for raw_path in paths:
            normalized = Path(raw_path).as_posix()
            path = self._safe_path(normalized)
            if not path.exists():
                files[normalized] = None
                continue
            if not path.is_file():
                raise ValueError(f"snapshot path is not a file: {normalized}")
            data = self._read_bounded(path, normalized)
            total_bytes += len(data)
            self._check_total(total_bytes)
            files[normalized] = base64.b64encode(data).decode("ascii")

        snapshot = TurnSnapshot(uuid.uuid4().hex, phase, time.time(), files)
        self._persist(snapshot)
        return snapshot

    def capture_tree(self, phase: SnapshotPhase) -> TurnSnapshot:
        """Capture the bounded part of a project without blocking the run.

        Large/generated repositories routinely contain artifacts that are not
        reasonable to copy into a JSON snapshot.  Those paths are recorded as
        skipped and are protected from deletion during restore; one oversized
        file therefore no longer prevents the Agent from starting.
        """

        files: dict[str, str | None] = {}
        skipped: dict[str, str] = {}
        total_bytes = 0
        for path in sorted(self.project_root.rglob("*")):
            relative = path.relative_to(self.project_root)
            if self._ignored(relative):
                continue
            key = relative.as_posix()
            if path.is_symlink():
                try:
                    resolved = path.resolve(strict=True)
                except OSError as exc:
                    skipped[key] = f"unreadable symlink: {exc}"
                    continue
                if not resolved.is_relative_to(self.project_root):
                    skipped[key] = "symbolic link escapes project root"
                    continue
                if len(files) >= self.max_files:
                    skipped[key] = f"snapshot file limit reached ({self.max_files})"
                    continue
                files[key] = self.SYMLINK_PREFIX + os.readlink(path)
                continue
            if not path.is_file():
                continue
            if len(files) >= self.max_files:
                skipped[key] = f"snapshot file limit reached ({self.max_files})"
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                skipped[key] = f"could not stat file: {exc}"
                continue
            if size > self.max_file_bytes:
                skipped[key] = (
                    f"file exceeds snapshot limit ({size} > {self.max_file_bytes})"
                )
                continue
            if total_bytes + size > self.max_total_bytes:
                skipped[key] = (
                    "snapshot total-byte limit would be exceeded "
                    f"({self.max_total_bytes})"
                )
                continue
            try:
                data = self._read_bounded(path, key)
            except (OSError, ValueError) as exc:
                # The file may have grown between stat() and read().  Treat it
                # like any other excluded artifact instead of failing startup.
                skipped[key] = str(exc)
                continue
            total_bytes += len(data)
            files[key] = base64.b64encode(data).decode("ascii")

        snapshot = TurnSnapshot(
            uuid.uuid4().hex,
            phase,
            time.time(),
            files,
            True,
            skipped,
        )
        self._persist(snapshot)
        return snapshot

    def load(self, snapshot_id: str) -> TurnSnapshot:
        path = self.store / f"{snapshot_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return TurnSnapshot(
            str(payload["id"]),
            SnapshotPhase(payload["phase"]),
            float(payload["created_at"]),
            dict(payload["files"]),
            bool(payload.get("tree", False)),
            {
                str(key): str(value)
                for key, value in dict(payload.get("skipped", {})).items()
            },
        )

    def restore(self, snapshot_id: str) -> RestoreResult:
        """Restore an explicit snapshot; tree snapshots use tree semantics."""

        snapshot = self.load(snapshot_id)
        if snapshot.tree:
            return self._restore_tree_snapshot(snapshot)
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
        return RestoreResult(
            tuple(restored),
            tuple(removed),
            tuple(sorted(snapshot.skipped)),
        )

    def restore_tree(self, snapshot_id: str) -> RestoreResult:
        snapshot = self.load(snapshot_id)
        if not snapshot.tree:
            raise ValueError("snapshot is not a project-tree snapshot")
        return self._restore_tree_snapshot(snapshot)

    def list_snapshots(self) -> list[TurnSnapshot]:
        if not self.store.is_dir():
            return []
        snapshots = [self.load(path.stem) for path in self.store.glob("*.json")]
        return sorted(snapshots, key=lambda item: item.created_at)

    def prune(
        self,
        *,
        keep_ids: Iterable[str] = (),
        keep_last: int = 40,
        max_age_seconds: float | None = None,
    ) -> tuple[str, ...]:
        """Delete old unreferenced snapshots and return removed IDs."""

        if keep_last < 0:
            raise ValueError("keep_last cannot be negative")
        if max_age_seconds is not None and max_age_seconds < 0:
            raise ValueError("max_age_seconds cannot be negative")
        snapshots = self.list_snapshots()
        protected = {str(value) for value in keep_ids if str(value)}
        if keep_last:
            protected.update(item.id for item in snapshots[-keep_last:])
        cutoff = (
            time.time() - max_age_seconds
            if max_age_seconds is not None
            else None
        )
        removed: list[str] = []
        for snapshot in snapshots:
            if snapshot.id in protected:
                continue
            if cutoff is not None and snapshot.created_at >= cutoff:
                continue
            try:
                (self.store / f"{snapshot.id}.json").unlink()
            except FileNotFoundError:
                continue
            removed.append(snapshot.id)
        return tuple(removed)

    def _restore_tree_snapshot(self, snapshot: TurnSnapshot) -> RestoreResult:
        expected = set(snapshot.files)
        protected = set(snapshot.skipped)
        removed: list[str] = []
        for path in sorted(self.project_root.rglob("*"), reverse=True):
            if not path.is_file() and not path.is_symlink():
                continue
            relative = path.relative_to(self.project_root)
            if self._ignored(relative):
                continue
            key = relative.as_posix()
            if key not in expected and key not in protected:
                path.unlink()
                removed.append(key)

        restored: list[str] = []
        for raw_path, encoded in snapshot.files.items():
            if encoded is None:
                continue
            path = self.project_root / raw_path
            path.parent.mkdir(parents=True, exist_ok=True)
            if encoded.startswith(self.SYMLINK_PREFIX):
                if path.exists() or path.is_symlink():
                    path.unlink()
                os.symlink(encoded.removeprefix(self.SYMLINK_PREFIX), path)
                restored.append(raw_path)
                continue
            safe_path = self._safe_path(raw_path)
            if safe_path.is_symlink():
                safe_path.unlink()
            safe_path.write_bytes(base64.b64decode(encoded))
            restored.append(raw_path)
        self._remove_empty_directories()
        return RestoreResult(
            tuple(restored),
            tuple(removed),
            tuple(sorted(snapshot.skipped)),
        )

    def _persist(self, snapshot: TurnSnapshot) -> None:
        self.store.mkdir(parents=True, exist_ok=True)
        payload = asdict(snapshot)
        payload["phase"] = snapshot.phase.value
        target = self.store / f"{snapshot.id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)

    def _read_bounded(self, path: Path, label: str) -> bytes:
        data = path.read_bytes()
        if len(data) > self.max_file_bytes:
            raise ValueError(f"snapshot file is too large: {label}")
        return data

    def _check_total(self, total_bytes: int) -> None:
        if total_bytes > self.max_total_bytes:
            raise ValueError(
                f"snapshot exceeds total byte limit ({self.max_total_bytes})"
            )

    def _ignored(self, relative: Path) -> bool:
        return any(part in self.ignored_directories for part in relative.parts)

    def _remove_empty_directories(self) -> None:
        for path in sorted(self.project_root.rglob("*"), reverse=True):
            if not path.is_dir() or path == self.project_root:
                continue
            relative = path.relative_to(self.project_root)
            if self._ignored(relative):
                continue
            try:
                path.rmdir()
            except OSError:
                pass

    def _safe_path(self, raw_path: str) -> Path:
        path = (self.project_root / raw_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("snapshot path escapes project root")
        return path
