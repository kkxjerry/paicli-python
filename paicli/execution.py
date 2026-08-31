"""Top-level transactional execution, tracing, budgets, and crash recovery.

``RunCoordinator`` is the only production execution entry point used by the
CLI and evaluation harness.  It owns the run ID, parent budget, trace scope,
workspace snapshots, durable checkpoints, terminal status, and resume rules.
The lower ReAct/Plan/Team runtimes remain independently testable mechanisms.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .agents.models import AgentOutcome, RunStatus
from .bootstrap import ApplicationRuntime
from .images import ImageAttachment
from .observability import (
    RunBudget,
    RunBudgetExceeded,
    RunLimits,
    run_budget_scope,
    trace_scope,
    traced_span,
)
from .orchestration import (
    OrchestrationObserver,
    OrchestrationResult,
    OrchestrationStatus,
    PlanApproval,
    TaskRunRecord,
)
from .planning import ExecutionPlan, Task, TaskStatus, TaskType
from .safety import RollbackPolicy
from .snapshot import SnapshotPhase, SnapshotService
from .state import RunStateStore, StoredRun, StoredRunStatus

RollbackDecision = Callable[[str], bool]


@dataclass(frozen=True)
class CoordinatedRun:
    run_id: str
    mode: str
    status: str
    answer: str
    error: str = ""
    exit_code: int = 0
    before_snapshot_id: str = ""
    after_snapshot_id: str = ""
    rolled_back: bool = False
    resumed: bool = False
    agent_outcome: AgentOutcome | None = None
    orchestration_result: OrchestrationResult | None = None
    budget: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "budget", dict(self.budget or {}))


class RunCheckpointObserver(OrchestrationObserver):
    """Persist DAG state and pre-attempt snapshots for uncertain side effects."""

    MUTATING_TYPES = {
        TaskType.FILE_WRITE,
        TaskType.COMMAND,
        TaskType.VERIFICATION,
    }

    def __init__(
        self,
        run_id: str,
        store: RunStateStore,
        snapshots: SnapshotService | None,
        *,
        records: Mapping[str, TaskRunRecord] | None = None,
    ) -> None:
        self.run_id = run_id
        self.store = store
        self.snapshots = snapshots
        self.records: dict[str, TaskRunRecord] = dict(records or {})
        self._lock = threading.RLock()
        self.current_task_snapshot_id = ""

    def plan_ready(
        self,
        mode: str,
        plan: ExecutionPlan,
        records: Mapping[str, TaskRunRecord],
    ) -> None:
        with self._lock:
            self.records.update(records)
            self.store.checkpoint(
                self.run_id,
                phase=f"{mode}:plan_ready",
                plan=plan,
                records=self.records,
            )

    def before_task(
        self,
        mode: str,
        plan: ExecutionPlan,
        task: Task,
        records: Mapping[str, TaskRunRecord],
    ) -> None:
        with self._lock:
            self.records.update(records)
            snapshot_id = ""
            if self.snapshots is not None and task.task_type in self.MUTATING_TYPES:
                snapshot_id = self.snapshots.capture_tree(SnapshotPhase.BEFORE).id
            self.current_task_snapshot_id = snapshot_id
            self.store.checkpoint(
                self.run_id,
                phase=(
                    f"{mode}:before_task:{task.id}:"
                    f"attempt_{task.execution_attempts + 1}"
                ),
                plan=plan,
                records=self.records,
                current_task_id=task.id,
                current_task_snapshot_id=snapshot_id,
            )

    def review_finished(
        self,
        mode: str,
        plan: ExecutionPlan,
        task: Task,
        records: Mapping[str, TaskRunRecord],
    ) -> None:
        with self._lock:
            self.records.update(records)
            self.store.checkpoint(
                self.run_id,
                phase=f"{mode}:review:{task.id}:{task.review_attempts}",
                plan=plan,
                records=self.records,
                current_task_id=task.id,
                current_task_snapshot_id=self.current_task_snapshot_id,
            )

    def after_task(
        self,
        mode: str,
        plan: ExecutionPlan,
        task: Task,
        records: Mapping[str, TaskRunRecord],
    ) -> None:
        with self._lock:
            self.records.update(records)
            self.store.checkpoint(
                self.run_id,
                phase=f"{mode}:after_task:{task.id}:{task.status.value}",
                plan=plan,
                records=self.records,
            )
            self.current_task_snapshot_id = ""


class RunCoordinator:
    """Execute and resume every user-visible mode through one trusted path."""

    def __init__(
        self,
        runtime: ApplicationRuntime,
        project_root: str | Path,
        *,
        state_store: RunStateStore | None = None,
        snapshot_service: SnapshotService | None = None,
        enable_snapshots: bool = True,
        limits: RunLimits | None = None,
        rollback_policy: RollbackPolicy | str = RollbackPolicy.ALWAYS,
        rollback_handler: RollbackDecision | None = None,
    ) -> None:
        self.runtime = runtime
        self.project_root = Path(project_root).resolve()
        self.state_store = state_store or RunStateStore(
            self.project_root / ".paicli" / "runs.db"
        )
        self.snapshots = (
            snapshot_service or SnapshotService(self.project_root)
            if enable_snapshots
            else None
        )
        self.limits = limits or RunLimits()
        self.rollback_policy = (
            rollback_policy
            if isinstance(rollback_policy, RollbackPolicy)
            else RollbackPolicy(rollback_policy)
        )
        self.rollback_handler = rollback_handler
        # This process has not started a run yet, so any still-running row came
        # from an interrupted earlier owner. The store is intentionally local
        # single-process state, not a distributed lease service.
        self.state_store.mark_stale_running_interrupted()

    def close(self) -> None:
        self.state_store.close()
        self.runtime.close()

    def execute(
        self,
        mode: str,
        prompt: str,
        *,
        images: tuple[ImageAttachment, ...] = (),
        plan_approval: PlanApproval | None = None,
    ) -> CoordinatedRun:
        normalized_mode = self._validate_mode(mode)
        if not prompt.strip() and not images:
            raise ValueError("prompt and images cannot both be empty")
        if normalized_mode != "react" and images:
            raise ValueError("Plan and Team modes currently accept text tasks only")

        before_id = self._capture(SnapshotPhase.BEFORE)
        run_id = self.runtime.begin_run(
            normalized_mode,
            prompt,
            metadata={"before_snapshot_id": before_id},
        ) or ("run_" + uuid.uuid4().hex)
        try:
            self.state_store.create(
                run_id=run_id,
                mode=normalized_mode,
                goal=prompt,
                prompt=prompt,
                before_snapshot_id=before_id,
                metadata={"schema_version": 1},
            )
        except Exception:
            self.runtime.finish_run(
                run_id,
                status=StoredRunStatus.FAILED,
                error="failed to create durable run state",
            )
            raise
        return self._execute_existing(
            run_id,
            normalized_mode,
            prompt,
            images=images,
            plan_approval=plan_approval,
            before_snapshot_id=before_id,
            resumed=False,
        )

    def resume(
        self,
        run_id: str,
        *,
        plan_approval: PlanApproval | None = None,
    ) -> CoordinatedRun:
        stored = self.state_store.load(run_id)
        if not stored.resumable:
            raise ValueError(
                f"run {run_id} is not resumable; current status is {stored.status}"
            )
        self._prepare_resume(stored)
        self.state_store.mark_running(run_id)
        if self.runtime.trace_store is not None:
            self.runtime.trace_store.resume_run(run_id)
        return self._execute_existing(
            run_id,
            stored.mode,
            stored.prompt,
            images=(),
            plan_approval=plan_approval,
            before_snapshot_id=stored.before_snapshot_id,
            resumed=True,
            stored=stored,
        )

    def recent_runs(self, limit: int = 20) -> list[StoredRun]:
        return self.state_store.recent(limit)

    def resumable_runs(self) -> list[StoredRun]:
        return self.state_store.resumable_runs()

    def _execute_existing(
        self,
        run_id: str,
        mode: str,
        prompt: str,
        *,
        images: tuple[ImageAttachment, ...],
        plan_approval: PlanApproval | None,
        before_snapshot_id: str,
        resumed: bool,
        stored: StoredRun | None = None,
    ) -> CoordinatedRun:
        budget = RunBudget(self.limits)
        observer = RunCheckpointObserver(
            run_id,
            self.state_store,
            self.snapshots,
            records=stored.records if stored is not None else None,
        )
        agent_outcome: AgentOutcome | None = None
        orchestration: OrchestrationResult | None = None
        answer = ""
        error = ""
        status = StoredRunStatus.FAILED

        try:
            with run_budget_scope(budget), trace_scope(
                run_id=run_id,
                agent_role=mode,
                agent_name="main",
            ), traced_span(
                self.runtime.trace_store,
                "run",
                mode,
                run_id=run_id,
                agent_role=mode,
                agent_name="main",
                attributes={"resumed": resumed},
            ):
                if mode == "react":
                    if resumed:
                        self.runtime.react.agent.clear_history()
                    agent_outcome = self.runtime.react.agent.run_outcome(
                        prompt,
                        images=images,
                    )
                    answer = agent_outcome.content
                    status = (
                        StoredRunStatus.SUCCEEDED
                        if agent_outcome.succeeded
                        else StoredRunStatus.CANCELLED
                        if agent_outcome.status is RunStatus.CANCELLED
                        else StoredRunStatus.FAILED
                    )
                    error = agent_outcome.error
                elif mode == "plan":
                    orchestration = (
                        self.runtime.plan.resume(
                            self._require_plan(stored),
                            records=stored.records if stored is not None else {},
                            observer=observer,
                        )
                        if resumed
                        else self.runtime.plan.run(
                            prompt,
                            approval=plan_approval,
                            observer=observer,
                        )
                    )
                    answer = orchestration.answer
                    status = _stored_status(orchestration.status)
                else:
                    orchestration = (
                        self.runtime.team.resume(
                            self._require_plan(stored),
                            records=stored.records if stored is not None else {},
                            observer=observer,
                        )
                        if resumed
                        else self.runtime.team.run(prompt, observer=observer)
                    )
                    answer = orchestration.answer
                    status = _stored_status(orchestration.status)
        except RunBudgetExceeded as exc:
            error = str(exc)
            status = StoredRunStatus.FAILED
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            status = StoredRunStatus.FAILED

        rolled_back = False
        if status in {StoredRunStatus.FAILED, StoredRunStatus.PARTIAL}:
            rolled_back = self._rollback_if_requested(before_snapshot_id)
            if rolled_back:
                self.runtime.react.agent.history.append(
                    {
                        "role": "system",
                        "content": (
                            "The previous run failed and its workspace changes "
                            "were rolled back. Do not treat those side effects as "
                            "present in later turns."
                        ),
                    }
                )
        after_id = self._capture(SnapshotPhase.AFTER)
        budget_snapshot = budget.snapshot()
        final_plan = orchestration.plan if orchestration is not None else None
        final_records = (
            orchestration.records if orchestration is not None else observer.records
        )
        self.state_store.finish(
            run_id,
            status=status,
            answer=answer,
            error=error,
            plan=final_plan,
            records=final_records,
            after_snapshot_id=after_id,
            metadata={
                "budget": budget_snapshot,
                "rolled_back": rolled_back,
                "resumed": resumed,
            },
        )
        self.runtime.finish_run(
            run_id,
            status=status,
            error=error,
            metadata={
                "budget": budget_snapshot,
                "before_snapshot_id": before_snapshot_id,
                "after_snapshot_id": after_id,
                "rolled_back": rolled_back,
                "resumed": resumed,
            },
        )
        return CoordinatedRun(
            run_id=run_id,
            mode=mode,
            status=status,
            answer=answer,
            error=error,
            exit_code=(
                0
                if status
                in {StoredRunStatus.SUCCEEDED, StoredRunStatus.CANCELLED}
                else 1
            ),
            before_snapshot_id=before_snapshot_id,
            after_snapshot_id=after_id,
            rolled_back=rolled_back,
            resumed=resumed,
            agent_outcome=agent_outcome,
            orchestration_result=orchestration,
            budget=budget_snapshot,
        )

    def _prepare_resume(self, stored: StoredRun) -> None:
        if stored.mode == "react":
            if self.snapshots is not None and stored.before_snapshot_id:
                self.snapshots.restore_tree(stored.before_snapshot_id)
            return

        plan = self._require_plan(stored)
        if self.snapshots is not None and stored.current_task_snapshot_id:
            self.snapshots.restore_tree(stored.current_task_snapshot_id)
        elif stored.current_task_id:
            # Without a task-level snapshot we cannot prove whether an
            # interrupted side effect committed. Restore the complete run and
            # restart its DAG rather than duplicate an uncertain operation.
            if self.snapshots is None or not stored.before_snapshot_id:
                raise ValueError(
                    "cannot safely resume an interrupted side-effect task "
                    "without its pre-task snapshot"
                )
            self.snapshots.restore_tree(stored.before_snapshot_id)
            for task in plan.tasks:
                _reset_task(task)
            stored.records.clear()
            return

        for task in plan.tasks:
            if task.status is TaskStatus.RUNNING:
                _reset_task(task)

    def _rollback_if_requested(self, before_snapshot_id: str) -> bool:
        if (
            self.snapshots is None
            or not before_snapshot_id
            or self.rollback_policy is RollbackPolicy.NEVER
        ):
            return False
        should_restore = self.rollback_policy is RollbackPolicy.ALWAYS
        if self.rollback_policy is RollbackPolicy.ASK:
            should_restore = bool(
                self.rollback_handler
                and self.rollback_handler("Agent run failed; restore BEFORE snapshot?")
            )
        if should_restore:
            self.snapshots.restore_tree(before_snapshot_id)
            return True
        return False

    def _capture(self, phase: SnapshotPhase) -> str:
        return self.snapshots.capture_tree(phase).id if self.snapshots else ""

    @staticmethod
    def _validate_mode(mode: str) -> str:
        normalized = str(mode).strip().lower()
        if normalized not in {"react", "plan", "team"}:
            raise ValueError(f"unknown execution mode: {mode!r}")
        return normalized

    @staticmethod
    def _require_plan(stored: StoredRun | None) -> ExecutionPlan:
        if stored is None or stored.plan is None:
            raise ValueError("resumable Plan/Team run has no persisted plan")
        return stored.plan


def _reset_task(task: Task) -> None:
    task.status = TaskStatus.PENDING
    task.result = ""
    task.error = ""
    task.started_at = None
    task.finished_at = None


def _stored_status(status: OrchestrationStatus) -> str:
    return {
        OrchestrationStatus.SUCCEEDED: StoredRunStatus.SUCCEEDED,
        OrchestrationStatus.PARTIAL: StoredRunStatus.PARTIAL,
        OrchestrationStatus.FAILED: StoredRunStatus.FAILED,
        OrchestrationStatus.CANCELLED: StoredRunStatus.CANCELLED,
    }[status]


__all__ = [
    "CoordinatedRun",
    "RollbackDecision",
    "RunCheckpointObserver",
    "RunCoordinator",
]
