"""Plan and Team runtimes over the shared Agent loop and unified DAG.

Phase 6 connects LLM planning to real task workers. Phase 7 adds independent
reviewers with bounded local retry. Phase 8 executes only read-scoped tasks in
parallel; mutation-capable tasks remain serial until worktree isolation or
explicit plan-level resource declarations are available.
"""

from __future__ import annotations

import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Protocol

from .agents.models import AgentOutcome
from .context import TokenUsage
from .llm_client import LlmClient
from .observability import (
    TraceStore,
    current_trace_context,
    trace_scope,
    traced_span,
)
from .planning import (
    DagScheduler,
    ExecutionPlan,
    PlanExecuteAgent,
    Planner,
    PlanValidator,
    Task,
    TaskConcurrencyPolicy,
    TaskStatus,
)
from .review import ReviewResult, ReviewRun, ReviewVerdict, ReviewerAgent
from .subagents import SubAgentFactory, TaskPacket, ToolScope

EventHandler = Callable[[str, str], None]


class PlanReviewAction(str, Enum):
    """Human decision after inspecting a validated plan."""

    EXECUTE = "execute"
    CANCEL = "cancel"
    SUPPLEMENT = "supplement"


@dataclass(frozen=True)
class PlanReviewDecision:
    """Execute, cancel, or request a complete revised plan."""

    action: PlanReviewAction
    feedback: str = ""

    @classmethod
    def execute(cls) -> PlanReviewDecision:
        return cls(PlanReviewAction.EXECUTE)

    @classmethod
    def cancel(cls) -> PlanReviewDecision:
        return cls(PlanReviewAction.CANCEL)

    @classmethod
    def supplement(cls, feedback: str) -> PlanReviewDecision:
        normalized = str(feedback).strip()
        if not normalized:
            raise ValueError("plan revision feedback cannot be empty")
        return cls(PlanReviewAction.SUPPLEMENT, normalized)


PlanReviewHandler = Callable[
    [ExecutionPlan],
    PlanReviewDecision | bool,
]
PlanApproval = PlanReviewHandler


class OrchestrationObserver(Protocol):
    """Persistence/snapshot hooks that do not participate in task decisions."""

    def plan_ready(
        self,
        mode: str,
        plan: ExecutionPlan,
        records: Mapping[str, "TaskRunRecord"],
    ) -> None: ...

    def before_task(
        self,
        mode: str,
        plan: ExecutionPlan,
        task: Task,
        records: Mapping[str, "TaskRunRecord"],
    ) -> None: ...

    def review_finished(
        self,
        mode: str,
        plan: ExecutionPlan,
        task: Task,
        records: Mapping[str, "TaskRunRecord"],
    ) -> None: ...

    def after_task(
        self,
        mode: str,
        plan: ExecutionPlan,
        task: Task,
        records: Mapping[str, "TaskRunRecord"],
    ) -> None: ...


class OrchestrationMode(str, Enum):
    PLAN = "plan"
    TEAM = "team"


class OrchestrationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskRunRecord:
    task_id: str
    worker_name: str
    tool_scope: ToolScope
    worker_outcomes: list[AgentOutcome] = field(default_factory=list)
    reviews: list[ReviewRun] = field(default_factory=list)

    @property
    def changed_files(self) -> tuple[str, ...]:
        values: list[str] = []
        for outcome in self.worker_outcomes:
            for path in outcome.changed_files:
                if path not in values:
                    values.append(path)
        return tuple(values)

    @property
    def final_worker_outcome(self) -> AgentOutcome | None:
        return self.worker_outcomes[-1] if self.worker_outcomes else None

    @property
    def final_review(self) -> ReviewResult | None:
        return self.reviews[-1].result if self.reviews else None


@dataclass(frozen=True)
class AggregationResult:
    answer: str
    outcome: AgentOutcome | None = None


class ResultAggregator(Protocol):
    def aggregate(
        self,
        mode: OrchestrationMode,
        plan: ExecutionPlan,
        records: Mapping[str, TaskRunRecord],
    ) -> AggregationResult:
        """Produce one user-facing result without changing task state."""
        ...


class DeterministicResultAggregator:
    """Truthful availability fallback and deterministic test aggregator."""

    def aggregate(
        self,
        mode: OrchestrationMode,
        plan: ExecutionPlan,
        records: Mapping[str, TaskRunRecord],
    ) -> AggregationResult:
        del records
        lines = [f"{mode.value.title()} run for: {plan.goal}"]
        for task in plan.tasks:
            detail = task.result or task.error or "no result"
            lines.append(
                f"- [{task.status.value}] {task.id}: {_truncate(detail, 2_000)}"
            )
        return AggregationResult("\n".join(lines))


class LlmResultAggregator:
    """Use an isolated no-tool sub-agent, with deterministic fallback."""

    def __init__(self, factory: SubAgentFactory) -> None:
        self.factory = factory
        self.fallback = DeterministicResultAggregator()

    def aggregate(
        self,
        mode: OrchestrationMode,
        plan: ExecutionPlan,
        records: Mapping[str, TaskRunRecord],
    ) -> AggregationResult:
        payload = {
            "mode": mode.value,
            "goal": plan.goal,
            "plan_summary": plan.summary,
            "tasks": [
                {
                    "id": task.id,
                    "description": task.description,
                    "status": task.status.value,
                    "result": _truncate(task.result, 5_000),
                    "error": task.error,
                    "changed_files": list(
                        records[task.id].changed_files
                        if task.id in records
                        else ()
                    ),
                    "review": (
                        {
                            "verdict": records[task.id].final_review.verdict.value,
                            "summary": records[task.id].final_review.summary,
                            "issues": list(records[task.id].final_review.issues),
                        }
                        if task.id in records
                        and records[task.id].final_review is not None
                        else None
                    ),
                }
                for task in plan.tasks
            ],
        }
        prompt = (
            "Produce the final user-facing answer from this orchestration "
            "record. State completed work and changed files, disclose failed "
            "or skipped tasks, and do not invent tests or actions.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )
        subagent = self.factory.create_aggregator()
        try:
            outcome = subagent.run_prompt(prompt)
        except Exception:
            return self.fallback.aggregate(mode, plan, records)
        if not outcome.succeeded or not outcome.content.strip():
            fallback = self.fallback.aggregate(mode, plan, records)
            return AggregationResult(fallback.answer, outcome)
        return AggregationResult(outcome.content, outcome)


@dataclass(frozen=True)
class OrchestrationResult:
    mode: OrchestrationMode
    status: OrchestrationStatus
    plan: ExecutionPlan
    answer: str
    records: Mapping[str, TaskRunRecord]
    planner_usage: TokenUsage = TokenUsage(0, 0, 0)
    aggregation_outcome: AgentOutcome | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is OrchestrationStatus.SUCCEEDED

    @property
    def usage(self) -> TokenUsage:
        values = [self.planner_usage]
        for record in self.records.values():
            values.extend(outcome.usage for outcome in record.worker_outcomes)
            for review in record.reviews:
                values.extend(outcome.usage for outcome in review.model_outcomes)
        if self.aggregation_outcome is not None:
            values.append(self.aggregation_outcome.usage)
        return _sum_usage(values)

    @property
    def changed_files(self) -> tuple[str, ...]:
        paths: list[str] = []
        for record in self.records.values():
            for path in record.changed_files:
                if path not in paths:
                    paths.append(path)
        return tuple(paths)


class TaskAgentExecutionError(RuntimeError):
    def __init__(self, task_id: str, outcome: AgentOutcome) -> None:
        self.task_id = task_id
        self.outcome = outcome
        detail = outcome.error or outcome.finish_reason.value
        super().__init__(f"sub-agent for {task_id} stopped: {detail}")


class _PlanWorkerExecutor:
    def __init__(
        self,
        plan: ExecutionPlan,
        factory: SubAgentFactory,
        records: dict[str, TaskRunRecord],
        on_event: EventHandler,
        run_id: str = "",
    ) -> None:
        self.plan = plan
        self.factory = factory
        self.records = records
        self.on_event = on_event
        self.run_id = run_id
        self._lock = threading.Lock()

    def set_plan(self, plan: ExecutionPlan) -> None:
        self.plan = plan

    def __call__(self, task: Task, completed_results: dict[str, str]) -> str:
        worker = self.factory.create_worker(task, name=f"plan-worker-{task.id}")
        with self._lock:
            record = self.records.get(task.id)
            if record is None:
                record = TaskRunRecord(task.id, worker.name, worker.scope)
                self.records[task.id] = record
            else:
                record.worker_name = worker.name
                record.tool_scope = worker.scope
            changed_by_task = {
                task_id: item.changed_files
                for task_id, item in self.records.items()
            }
        packet = TaskPacket.from_task(
            self.plan,
            task,
            completed_results,
            changed_files=changed_by_task,
            attempt=task.execution_attempts,
            run_id=self.run_id,
        )
        self.on_event("task", f"{task.id} started with {worker.scope.value} tools")
        outcome = worker.run_task(packet)
        record.worker_outcomes.append(outcome)
        if not outcome.succeeded:
            raise TaskAgentExecutionError(task.id, outcome)
        self.on_event("task", f"{task.id} completed")
        return outcome.content


class PlanModeRuntime:
    """LLM Planner -> validated DAG -> real shared-loop workers -> result."""

    def __init__(
        self,
        planner: Planner,
        factory: SubAgentFactory,
        *,
        max_workers: int = 4,
        max_plan_revisions: int = 2,
        aggregator: ResultAggregator | None = None,
        on_event: EventHandler | None = None,
        trace_store: TraceStore | None = None,
    ) -> None:
        if max_plan_revisions < 0:
            raise ValueError("max_plan_revisions cannot be negative")
        self.planner = planner
        self.factory = factory
        self.concurrency = TaskConcurrencyPolicy(max_workers=max_workers)
        self.max_plan_revisions = max_plan_revisions
        self.aggregator = aggregator or LlmResultAggregator(factory)
        self.on_event = on_event or (lambda _kind, _text: None)
        self.trace_store = trace_store

    def set_client(self, client: LlmClient) -> None:
        if hasattr(self.planner, "client"):
            setattr(self.planner, "client", client)
        self.factory.set_client(client)

    def run(
        self,
        goal: str,
        *,
        approval: PlanApproval | None = None,
        observer: OrchestrationObserver | None = None,
    ) -> OrchestrationResult:
        trace = current_trace_context()
        with traced_span(
            self.trace_store,
            "agent",
            "planner",
            run_id=trace.run_id,
            agent_role="planner",
            agent_name="planner",
        ):
            plan = self.planner.create_plan(goal)
        PlanValidator(require_tasks=True).validate(plan)
        revisions = 0
        while True:
            self.on_event("plan", plan.render())
            if approval is None:
                decision = PlanReviewDecision.execute()
            else:
                raw_decision = approval(plan)
                decision = (
                    PlanReviewDecision.execute()
                    if raw_decision is True
                    else PlanReviewDecision.cancel()
                    if raw_decision is False
                    else raw_decision
                )
                if not isinstance(decision, PlanReviewDecision):
                    raise TypeError(
                        "plan approval must return bool or PlanReviewDecision"
                    )

            if decision.action is PlanReviewAction.CANCEL:
                return OrchestrationResult(
                    OrchestrationMode.PLAN,
                    OrchestrationStatus.CANCELLED,
                    plan,
                    "Plan execution was cancelled before any task ran.",
                    {},
                    _planner_usage(self.planner),
                )
            if decision.action is PlanReviewAction.EXECUTE:
                break
            if decision.action is not PlanReviewAction.SUPPLEMENT:
                raise ValueError(f"unknown plan review action: {decision.action}")
            if revisions >= self.max_plan_revisions:
                return OrchestrationResult(
                    OrchestrationMode.PLAN,
                    OrchestrationStatus.CANCELLED,
                    plan,
                    "Plan execution was cancelled because the revision limit "
                    f"({self.max_plan_revisions}) was reached.",
                    {},
                    _planner_usage(self.planner),
                )
            revise = getattr(self.planner, "revise_plan", None)
            if not callable(revise):
                raise TypeError("configured planner does not support revision")
            plan = revise(plan, decision.feedback)
            PlanValidator(require_tasks=True).validate(plan)
            revisions += 1

        records: dict[str, TaskRunRecord] = {}
        _notify_observer(
            observer,
            "plan_ready",
            OrchestrationMode.PLAN.value,
            plan,
            records,
        )
        return self.execute_plan(
            plan,
            records=records,
            observer=observer,
            planner_usage=_planner_usage(self.planner),
        )

    def resume(
        self,
        plan: ExecutionPlan,
        *,
        records: Mapping[str, TaskRunRecord] | None = None,
        observer: OrchestrationObserver | None = None,
    ) -> OrchestrationResult:
        PlanValidator(require_tasks=True).validate(plan)
        return self.execute_plan(
            plan,
            records=dict(records or {}),
            observer=observer,
            planner_usage=TokenUsage(0, 0, 0),
        )

    def execute_plan(
        self,
        plan: ExecutionPlan,
        *,
        records: dict[str, TaskRunRecord],
        observer: OrchestrationObserver | None,
        planner_usage: TokenUsage,
    ) -> OrchestrationResult:
        trace = current_trace_context()
        worker_executor = _PlanWorkerExecutor(
            plan,
            self.factory,
            records,
            self.on_event,
            trace.run_id,
        )
        executor = PlanExecuteAgent(
            self.planner,
            worker_executor,
            concurrency=self.concurrency,
            before_task=lambda current, task: _notify_observer(
                observer,
                "before_task",
                OrchestrationMode.PLAN.value,
                current,
                task,
                records,
            ),
            after_task=lambda current, task: _notify_observer(
                observer,
                "after_task",
                OrchestrationMode.PLAN.value,
                current,
                task,
                records,
            ),
        )
        completed_plan = executor.execute(plan)
        aggregation = self.aggregator.aggregate(
            OrchestrationMode.PLAN,
            completed_plan,
            records,
        )
        return OrchestrationResult(
            OrchestrationMode.PLAN,
            _status_for_plan(completed_plan),
            completed_plan,
            aggregation.answer,
            dict(records),
            planner_usage,
            aggregation.outcome,
        )


class TeamModeRuntime:
    """Planner -> isolated workers -> reviewer gate -> final aggregation."""

    def __init__(
        self,
        planner: Planner,
        factory: SubAgentFactory,
        *,
        max_workers: int = 2,
        max_review_retries: int = 2,
        review_repair_attempts: int = 1,
        aggregator: ResultAggregator | None = None,
        on_event: EventHandler | None = None,
        trace_store: TraceStore | None = None,
    ) -> None:
        if max_review_retries < 0:
            raise ValueError("max_review_retries cannot be negative")
        self.planner = planner
        self.factory = factory
        self.concurrency = TaskConcurrencyPolicy(max_workers=max_workers)
        self.max_review_retries = max_review_retries
        self.review_repair_attempts = review_repair_attempts
        self.aggregator = aggregator or LlmResultAggregator(factory)
        self.on_event = on_event or (lambda _kind, _text: None)
        self.trace_store = trace_store

    def set_client(self, client: LlmClient) -> None:
        if hasattr(self.planner, "client"):
            setattr(self.planner, "client", client)
        self.factory.set_client(client)

    def run(
        self,
        goal: str,
        *,
        observer: OrchestrationObserver | None = None,
    ) -> OrchestrationResult:
        trace = current_trace_context()
        with traced_span(
            self.trace_store,
            "agent",
            "planner",
            run_id=trace.run_id,
            agent_role="planner",
            agent_name="planner",
        ):
            plan = self.planner.create_plan(goal)
        PlanValidator(require_tasks=True).validate(plan)
        self.on_event("plan", plan.render())
        records: dict[str, TaskRunRecord] = {}
        _notify_observer(
            observer,
            "plan_ready",
            OrchestrationMode.TEAM.value,
            plan,
            records,
        )
        return self.execute_plan(
            plan,
            records=records,
            observer=observer,
            planner_usage=_planner_usage(self.planner),
        )

    def resume(
        self,
        plan: ExecutionPlan,
        *,
        records: Mapping[str, TaskRunRecord] | None = None,
        observer: OrchestrationObserver | None = None,
    ) -> OrchestrationResult:
        PlanValidator(require_tasks=True).validate(plan)
        restored_records = dict(records or {})
        _notify_observer(
            observer,
            "plan_ready",
            OrchestrationMode.TEAM.value,
            plan,
            restored_records,
        )
        return self.execute_plan(
            plan,
            records=restored_records,
            observer=observer,
            planner_usage=TokenUsage(0, 0, 0),
        )

    def execute_plan(
        self,
        plan: ExecutionPlan,
        *,
        records: dict[str, TaskRunRecord],
        observer: OrchestrationObserver | None,
        planner_usage: TokenUsage,
    ) -> OrchestrationResult:
        trace = current_trace_context()
        while not plan.is_finished():
            ready = plan.ready_tasks()
            if not ready:
                marked = DagScheduler.mark_blocked(plan)
                if marked:
                    continue
                for task in plan.tasks:
                    if task.status is TaskStatus.PENDING:
                        task.mark_skipped("no executable task remained")
                break

            completed_snapshot = plan.completed_results()
            changed_snapshot = {
                task_id: record.changed_files
                for task_id, record in records.items()
            }
            for wave in self.concurrency.execution_waves(ready):
                self._execute_wave(
                    plan,
                    wave,
                    completed_snapshot,
                    changed_snapshot,
                    records,
                    trace.run_id,
                    observer,
                )
                DagScheduler.mark_blocked(plan)
                completed_snapshot = plan.completed_results()
                changed_snapshot = {
                    task_id: record.changed_files
                    for task_id, record in records.items()
                }

        aggregation = self.aggregator.aggregate(
            OrchestrationMode.TEAM,
            plan,
            records,
        )
        return OrchestrationResult(
            OrchestrationMode.TEAM,
            _status_for_plan(plan),
            plan,
            aggregation.answer,
            dict(records),
            planner_usage,
            aggregation.outcome,
        )

    def _execute_wave(
        self,
        plan: ExecutionPlan,
        wave: list[Task],
        completed_results: Mapping[str, str],
        changed_files: Mapping[str, tuple[str, ...]],
        records: dict[str, TaskRunRecord],
        run_id: str,
        observer: OrchestrationObserver | None,
    ) -> None:
        if len(wave) == 1:
            task = wave[0]
            records[task.id] = self._execute_task(
                plan,
                task,
                completed_results,
                changed_files,
                run_id,
                observer,
            )
            return

        executor = ThreadPoolExecutor(
            max_workers=min(self.concurrency.max_workers, len(wave)),
            thread_name_prefix="paicli-team-task",
        )
        futures = {
            task.id: executor.submit(
                contextvars.copy_context().run,
                self._execute_task,
                plan,
                task,
                dict(completed_results),
                dict(changed_files),
                run_id,
                observer,
            )
            for task in wave
        }
        try:
            for task in wave:
                try:
                    records[task.id] = futures[task.id].result()
                except Exception as exc:
                    task.mark_failed(f"{type(exc).__name__}: {exc}")
                    records[task.id] = TaskRunRecord(
                        task.id,
                        f"worker-{task.id}",
                        self.factory.scope_for_task(task),
                    )
        finally:
            executor.shutdown(wait=True, cancel_futures=False)

    def _execute_task(
        self,
        plan: ExecutionPlan,
        task: Task,
        completed_results: Mapping[str, str],
        changed_files: Mapping[str, tuple[str, ...]],
        run_id: str,
        observer: OrchestrationObserver | None,
    ) -> TaskRunRecord:
        worker = self.factory.create_worker(task, name=f"worker-{task.id}")
        reviewer = ReviewerAgent(
            self.factory.create_reviewer(name=f"reviewer-{task.id}"),
            max_repair_attempts=self.review_repair_attempts,
        )
        record = TaskRunRecord(task.id, worker.name, worker.scope)
        feedback: tuple[str, ...] = ()

        def finish() -> TaskRunRecord:
            _notify_observer(
                observer,
                "after_task",
                OrchestrationMode.TEAM.value,
                plan,
                task,
                {task.id: record},
            )
            return record

        for retry_index in range(self.max_review_retries + 1):
            _notify_observer(
                observer,
                "before_task",
                OrchestrationMode.TEAM.value,
                plan,
                task,
                {task.id: record},
            )
            task.mark_running()
            packet = TaskPacket.from_task(
                plan,
                task,
                completed_results,
                changed_files=changed_files,
                attempt=task.execution_attempts,
                review_feedback=feedback,
                run_id=run_id,
            )
            self.on_event(
                "task",
                f"{task.id} worker attempt {task.execution_attempts} started",
            )
            outcome = worker.run_task(packet)
            record.worker_outcomes.append(outcome)
            if not outcome.succeeded:
                task.mark_failed(
                    outcome.error
                    or f"worker stopped with {outcome.finish_reason.value}"
                )
                return finish()

            review_run = reviewer.review(packet, outcome)
            record.reviews.append(review_run)
            task.review_attempts += 1
            review = review_run.result
            self.on_event(
                "review",
                f"{task.id}: {review.verdict.value} — {review.summary}",
            )
            _notify_observer(
                observer,
                "review_finished",
                OrchestrationMode.TEAM.value,
                plan,
                task,
                {task.id: record},
            )

            if review.verdict is ReviewVerdict.APPROVED:
                task.mark_completed(outcome.content)
                return finish()
            if review.verdict is ReviewVerdict.ERROR:
                task.mark_failed("reviewer error: " + (review.error or review.summary))
                return finish()
            # The reviewer explicitly declares whether the same worker can
            # repair the current task. Non-retryable rejection is a plan-level
            # or safety failure and must not be converted into an endless local
            # edit loop.
            if not review.retryable:
                task.mark_failed("review rejected task: " + review.feedback())
                return finish()
            if retry_index >= self.max_review_retries:
                task.mark_failed(
                    "review changes were not resolved after "
                    f"{self.max_review_retries} local retry attempt(s): "
                    + review.feedback()
                )
                return finish()
            feedback = (review.feedback(),)

        task.mark_failed("review loop ended without a verdict")
        return finish()


def _notify_observer(
    observer: OrchestrationObserver | None,
    method: str,
    *arguments: object,
) -> None:
    if observer is None:
        return
    callback = getattr(observer, method, None)
    if callable(callback):
        callback(*arguments)


def _status_for_plan(plan: ExecutionPlan) -> OrchestrationStatus:
    completed = sum(task.status is TaskStatus.COMPLETED for task in plan.tasks)
    failed = sum(
        task.status in {TaskStatus.FAILED, TaskStatus.SKIPPED}
        for task in plan.tasks
    )
    if completed == len(plan.tasks) and plan.tasks:
        return OrchestrationStatus.SUCCEEDED
    if completed and failed:
        return OrchestrationStatus.PARTIAL
    return OrchestrationStatus.FAILED


def _planner_usage(planner: object) -> TokenUsage:
    value = getattr(planner, "last_usage", None)
    return value if isinstance(value, TokenUsage) else TokenUsage(0, 0, 0)


def _sum_usage(values: list[TokenUsage]) -> TokenUsage:
    return TokenUsage(
        sum(value.input_tokens for value in values),
        sum(value.output_tokens for value in values),
        sum(value.cached_input_tokens for value in values),
    )


def _truncate(value: str, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "\n...(truncated)"


__all__ = [
    "AggregationResult",
    "DeterministicResultAggregator",
    "LlmResultAggregator",
    "OrchestrationMode",
    "OrchestrationObserver",
    "PlanReviewAction",
    "PlanReviewDecision",
    "PlanReviewHandler",
    "OrchestrationResult",
    "OrchestrationStatus",
    "PlanModeRuntime",
    "ResultAggregator",
    "TaskAgentExecutionError",
    "TaskRunRecord",
    "TeamModeRuntime",
]
