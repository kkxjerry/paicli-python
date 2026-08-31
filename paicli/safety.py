"""Workspace transaction helpers shared by CLI and recovery tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Generic, TypeVar

from .snapshot import SnapshotPhase, SnapshotService

T = TypeVar("T")


class RollbackPolicy(str, Enum):
    ALWAYS = "always"
    ASK = "ask"
    NEVER = "never"


@dataclass(frozen=True)
class GuardedResult(Generic[T]):
    value: T | None
    before_snapshot_id: str
    after_snapshot_id: str
    rolled_back: bool
    error: str = ""


class WorkspaceRunGuard:
    """Compatibility transaction wrapper for isolated library calls.

    The production CLI uses :class:`paicli.execution.RunCoordinator`, which
    additionally persists state and trace data. This helper remains useful in
    focused tests and custom integrations.
    """

    def __init__(
        self,
        project_root: str | Path,
        *,
        snapshots: SnapshotService | None = None,
        rollback_policy: RollbackPolicy | str = RollbackPolicy.ALWAYS,
        rollback_handler: Callable[[str], bool] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.snapshots = snapshots or SnapshotService(self.project_root)
        self.rollback_policy = (
            rollback_policy
            if isinstance(rollback_policy, RollbackPolicy)
            else RollbackPolicy(rollback_policy)
        )
        self.rollback_handler = rollback_handler

    def run(self, operation: Callable[[], T]) -> GuardedResult[T]:
        before = self.snapshots.capture_tree(SnapshotPhase.BEFORE)
        try:
            value = operation()
        except Exception as exc:
            rolled_back = self._should_restore()
            if rolled_back:
                self.snapshots.restore_tree(before.id)
            after = self.snapshots.capture_tree(SnapshotPhase.AFTER)
            return GuardedResult(
                None,
                before.id,
                after.id,
                rolled_back,
                f"{type(exc).__name__}: {exc}",
            )
        after = self.snapshots.capture_tree(SnapshotPhase.AFTER)
        return GuardedResult(value, before.id, after.id, False)

    def _should_restore(self) -> bool:
        if self.rollback_policy is RollbackPolicy.ALWAYS:
            return True
        if self.rollback_policy is RollbackPolicy.NEVER:
            return False
        return bool(
            self.rollback_handler
            and self.rollback_handler("Operation failed; restore BEFORE snapshot?")
        )


__all__ = [
    "GuardedResult",
    "RollbackPolicy",
    "WorkspaceRunGuard",
]
