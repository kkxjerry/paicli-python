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
    """Create a workspace transaction around an ordinary return-code call.

    The constructor accepts either a project path or an already configured
    :class:`SnapshotService`.  ``run`` returns the wrapped operation's original
    value for compatibility, while ``last_record`` exposes snapshot and
    rollback metadata to the coordinator and tests.
    """

    def __init__(
        self,
        project_root: str | Path | SnapshotService,
        *,
        snapshots: SnapshotService | None = None,
        rollback_policy: RollbackPolicy | str = RollbackPolicy.ALWAYS,
        rollback_handler: Callable[[str], bool] | None = None,
    ) -> None:
        if isinstance(project_root, SnapshotService):
            if snapshots is not None:
                raise ValueError("snapshots cannot be supplied twice")
            self.snapshots = project_root
            self.project_root = project_root.project_root
        else:
            self.project_root = Path(project_root).resolve()
            self.snapshots = snapshots or SnapshotService(self.project_root)
        self.rollback_policy = (
            rollback_policy
            if isinstance(rollback_policy, RollbackPolicy)
            else RollbackPolicy(rollback_policy)
        )
        self.rollback_handler = rollback_handler
        self.last_record: GuardedResult[T] | None = None

    def run(self, operation: Callable[[], T]) -> T | None:
        before = self.snapshots.capture_tree(SnapshotPhase.BEFORE)
        value: T | None = None
        error = ""
        failed = False
        try:
            value = operation()
            # CLI/library operations conventionally return zero for success.
            failed = isinstance(value, int) and not isinstance(value, bool) and value != 0
        except Exception as exc:  # Store a recoverable transaction record.
            failed = True
            error = f"{type(exc).__name__}: {exc}"

        rolled_back = failed and self._should_restore()
        if rolled_back:
            self.snapshots.restore_tree(before.id)
        after = self.snapshots.capture_tree(SnapshotPhase.AFTER)
        self.last_record = GuardedResult(
            value,
            before.id,
            after.id,
            rolled_back,
            error,
        )
        return value

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
