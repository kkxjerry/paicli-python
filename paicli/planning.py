"""Unified planning contracts, DAG validation, scheduling, and LLM planning.

The model proposes task descriptions and dependency IDs. Deterministic code
owns all structural guarantees: schema parsing, unique IDs, known dependencies,
self-dependency rejection, cycle detection, topological order, execution
batches, and blocked-task propagation. Plan and future Team modes reuse this
single graph model instead of maintaining separate DAG implementations.
"""

from __future__ import annotations

import contextvars
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Protocol

from .context import TokenUsage
from .llm_client import LlmClient


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskType(str, Enum):
    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    COMMAND = "COMMAND"
    ANALYSIS = "ANALYSIS"
    VERIFICATION = "VERIFICATION"


@dataclass
class Task:
    """One executable node in a plan.

    The first five fields retain the original positional constructor so older
    learning-phase examples continue to work.
    """

    id: str
    description: str
    dependencies: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    task_type: TaskType = TaskType.ANALYSIS
    acceptance_criteria: tuple[str, ...] = ()
    error: str = ""
    execution_attempts: int = 0
    review_attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        self.id = str(self.id).strip()
        self.description = str(self.description).strip()
        if isinstance(self.dependencies, str):
            raise TypeError("dependencies must be an iterable of task IDs, not a string")
        self.dependencies = tuple(str(item).strip() for item in self.dependencies)
        self.acceptance_criteria = tuple(
            str(item).strip() for item in self.acceptance_criteria if str(item).strip()
        )
        if not isinstance(self.status, TaskStatus):
            self.status = TaskStatus(str(self.status).lower())
        if not isinstance(self.task_type, TaskType):
            self.task_type = TaskType(str(self.task_type).upper())

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING
        self.execution_attempts += 1
        self.started_at = time.time()
        self.finished_at = None
        self.error = ""

    def mark_completed(self, result: str) -> None:
        self.status = TaskStatus.COMPLETED
        self.result = str(result)
        self.error = ""
        self.finished_at = time.time()

    def mark_failed(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error = str(error)
        self.result = str(error)
        self.finished_at = time.time()

    def mark_skipped(self, reason: str) -> None:
        self.status = TaskStatus.SKIPPED
        self.error = str(reason)
        self.result = str(reason)
        self.finished_at = time.time()

    def clone(self, *, reset_runtime: bool = True) -> Task:
        return Task(
            self.id,
            self.description,
            self.dependencies,
            TaskStatus.PENDING if reset_runtime else self.status,
            "" if reset_runtime else self.result,
            self.task_type,
            self.acceptance_criteria,
            "" if reset_runtime else self.error,
            0 if reset_runtime else self.execution_attempts,
            0 if reset_runtime else self.review_attempts,
            None if reset_runtime else self.started_at,
            None if reset_runtime else self.finished_at,
        )


class PlanValidationError(ValueError):
    """The proposed plan is not a valid executable DAG."""


@dataclass
class ExecutionPlan:
    goal: str
    tasks: list[Task] = field(default_factory=list)
    summary: str = ""
    plan_id: str = field(default_factory=lambda: "plan_" + uuid.uuid4().hex[:12])
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.goal = str(self.goal).strip()
        self.summary = str(self.summary).strip() or self.goal
        self.tasks = list(self.tasks)
        PlanValidator(require_tasks=False).validate(self)

    def task(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(f"unknown task: {task_id}")

    def ready_tasks(self) -> list[Task]:
        return DagScheduler.ready_tasks(self)

    def blocked_tasks(self) -> list[Task]:
        return DagScheduler.blocked_tasks(self)

    def topological_order(self) -> list[Task]:
        return DagScheduler.topological_order(self)

    def execution_batches(self) -> list[list[Task]]:
        return DagScheduler.execution_batches(self)

    def completed_results(self) -> dict[str, str]:
        return {
            task.id: task.result
            for task in self.tasks
            if task.status is TaskStatus.COMPLETED
        }

    def inherit_completed_from(self, previous: ExecutionPlan) -> None:
        """Carry verified work into a replacement plan when IDs still match."""

        previous_completed = {
            task.id: task
            for task in previous.tasks
            if task.status is TaskStatus.COMPLETED
        }
        for task in self.tasks:
            old = previous_completed.get(task.id)
            if (
                old is None
                or old.description != task.description
                or old.task_type is not task.task_type
                or old.dependencies != task.dependencies
                or old.acceptance_criteria != task.acceptance_criteria
            ):
                continue
            task.status = TaskStatus.COMPLETED
            task.result = old.result
            task.error = ""
            task.execution_attempts = old.execution_attempts
            task.started_at = old.started_at
            task.finished_at = old.finished_at

    def is_finished(self) -> bool:
        return all(
            task.status
            in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}
            for task in self.tasks
        )

    def progress(self) -> float:
        if not self.tasks:
            return 1.0
        terminal = sum(
            task.status
            in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}
            for task in self.tasks
        )
        return terminal / len(self.tasks)

    def render(self) -> str:
        lines = [f"Goal: {self.goal}", f"Plan: {self.plan_id} — {self.summary}"]
        for task in self.tasks:
            deps = f" <- {', '.join(task.dependencies)}" if task.dependencies else ""
            lines.append(
                f"[{task.status.value:9}] {task.id} "
                f"({task.task_type.value}): {task.description}{deps}"
            )
        return "\n".join(lines)


class PlanValidator:
    """Validate every plan before a scheduler or worker sees it."""

    def __init__(self, *, require_tasks: bool = True) -> None:
        self.require_tasks = require_tasks

    def validate(self, plan: ExecutionPlan) -> None:
        if not plan.goal:
            raise PlanValidationError("plan goal cannot be empty")
        if self.require_tasks and not plan.tasks:
            raise PlanValidationError("plan must contain at least one task")

        ids = [task.id for task in plan.tasks]
        if any(not task_id for task_id in ids):
            raise PlanValidationError("task id cannot be empty")
        if len(ids) != len(set(ids)):
            raise PlanValidationError("task ids must be unique")
        if any(not task.description for task in plan.tasks):
            raise PlanValidationError("task description cannot be empty")

        known = set(ids)
        by_id = {task.id: task for task in plan.tasks}
        for task in plan.tasks:
            if len(task.dependencies) != len(set(task.dependencies)):
                raise PlanValidationError(
                    f"task {task.id} contains duplicate dependencies"
                )
            unknown = sorted(set(task.dependencies) - known)
            if unknown:
                raise PlanValidationError(
                    f"task {task.id} has unknown dependencies: {', '.join(unknown)}"
                )
            if task.id in task.dependencies:
                raise PlanValidationError(f"task {task.id} cannot depend on itself")

        visiting: list[str] = []
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                start = visiting.index(task_id)
                cycle = [*visiting[start:], task_id]
                raise PlanValidationError(
                    "task dependencies contain a cycle: " + " -> ".join(cycle)
                )
            if task_id in visited:
                return
            visiting.append(task_id)
            for dependency in by_id[task_id].dependencies:
                visit(dependency)
            visiting.pop()
            visited.add(task_id)

        for task_id in ids:
            visit(task_id)


class DagScheduler:
    """Deterministic graph traversal and runtime readiness decisions."""

    @staticmethod
    def topological_order(plan: ExecutionPlan) -> list[Task]:
        PlanValidator(require_tasks=False).validate(plan)
        by_id = {task.id: task for task in plan.tasks}
        position = {task.id: index for index, task in enumerate(plan.tasks)}
        indegree = {task.id: len(task.dependencies) for task in plan.tasks}
        dependents: dict[str, list[str]] = {task.id: [] for task in plan.tasks}
        for task in plan.tasks:
            for dependency in task.dependencies:
                dependents[dependency].append(task.id)
        for values in dependents.values():
            values.sort(key=position.__getitem__)

        ready = [task.id for task in plan.tasks if indegree[task.id] == 0]
        ordered: list[Task] = []
        while ready:
            task_id = ready.pop(0)
            ordered.append(by_id[task_id])
            for dependent in dependents[task_id]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
            ready.sort(key=position.__getitem__)
        if len(ordered) != len(plan.tasks):
            raise PlanValidationError("task dependencies contain a cycle")
        return ordered

    @staticmethod
    def execution_batches(plan: ExecutionPlan) -> list[list[Task]]:
        PlanValidator(require_tasks=False).validate(plan)
        by_id = {task.id: task for task in plan.tasks}
        position = {task.id: index for index, task in enumerate(plan.tasks)}
        remaining = {task.id: len(task.dependencies) for task in plan.tasks}
        dependents: dict[str, list[str]] = {task.id: [] for task in plan.tasks}
        for task in plan.tasks:
            for dependency in task.dependencies:
                dependents[dependency].append(task.id)

        current = [task.id for task in plan.tasks if remaining[task.id] == 0]
        batches: list[list[Task]] = []
        visited = 0
        while current:
            current.sort(key=position.__getitem__)
            batches.append([by_id[task_id] for task_id in current])
            visited += len(current)
            next_batch: list[str] = []
            for task_id in current:
                for dependent in dependents[task_id]:
                    remaining[dependent] -= 1
                    if remaining[dependent] == 0:
                        next_batch.append(dependent)
            current = next_batch
        if visited != len(plan.tasks):
            raise PlanValidationError("task dependencies contain a cycle")
        return batches

    @staticmethod
    def ready_tasks(plan: ExecutionPlan) -> list[Task]:
        by_id = {task.id: task for task in plan.tasks}
        return [
            task
            for task in plan.tasks
            if task.status is TaskStatus.PENDING
            and all(
                by_id[dependency].status is TaskStatus.COMPLETED
                for dependency in task.dependencies
            )
        ]

    @staticmethod
    def blocked_tasks(plan: ExecutionPlan) -> list[Task]:
        by_id = {task.id: task for task in plan.tasks}
        return [
            task
            for task in plan.tasks
            if task.status is TaskStatus.PENDING
            and any(
                by_id[dependency].status
                in {TaskStatus.FAILED, TaskStatus.SKIPPED}
                for dependency in task.dependencies
            )
        ]

    @staticmethod
    def mark_blocked(plan: ExecutionPlan) -> list[Task]:
        marked: list[Task] = []
        while True:
            blocked = DagScheduler.blocked_tasks(plan)
            if not blocked:
                break
            for task in blocked:
                task.mark_skipped("blocked by a failed dependency")
                marked.append(task)
        return marked


@dataclass(frozen=True)
class TaskConcurrencyPolicy:
    """Partition a ready DAG layer into bounded, side-effect-safe waves.

    The planner's task type is not trusted as permission by itself. Phase 8
    combines this scheduler with scoped tool runtimes: only FILE_READ and
    ANALYSIS tasks may share a worker wave, and those workers receive read-only
    tools. FILE_WRITE, COMMAND, and VERIFICATION tasks run alone until isolated
    worktrees or explicit read/write sets are available.
    """

    max_workers: int = 1
    parallel_task_types: frozenset[TaskType] = frozenset(
        {TaskType.FILE_READ, TaskType.ANALYSIS}
    )

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("max_workers must be positive")

    def can_run_in_parallel(self, task: Task) -> bool:
        return self.max_workers > 1 and task.task_type in self.parallel_task_types

    def execution_waves(self, tasks: Iterable[Task]) -> list[list[Task]]:
        waves: list[list[Task]] = []
        current: list[Task] = []
        for task in tasks:
            if not self.can_run_in_parallel(task):
                if current:
                    waves.append(current)
                    current = []
                waves.append([task])
                continue
            current.append(task)
            if len(current) >= self.max_workers:
                waves.append(current)
                current = []
        if current:
            waves.append(current)
        return waves


class Planner(Protocol):
    def create_plan(self, goal: str) -> ExecutionPlan:
        """Convert a goal into a validated plan."""
        ...

    def replan(self, plan: ExecutionPlan, failed_task: Task) -> ExecutionPlan:
        """Return a replacement plan, or the same object to keep the original."""
        ...


class PlanGenerationError(RuntimeError):
    """The planner could not obtain a valid plan after bounded repair."""


class LlmPlanner:
    """Generate candidate DAGs with an LLM, then validate them in code."""

    SYSTEM_PROMPT = """You are a coding-task planner.
Return only one JSON object with this shape:
{
  "summary": "short plan summary",
  "tasks": [
    {
      "id": "task_1",
      "description": "concrete executable task",
      "type": "FILE_READ | FILE_WRITE | COMMAND | ANALYSIS | VERIFICATION",
      "dependencies": [],
      "acceptance_criteria": ["observable completion condition"]
    }
  ]
}
Rules:
- IDs must be unique and dependencies must reference existing IDs.
- Add a dependency only when a task truly needs another task's result.
- Use the smallest sufficient plan: combine related repository reads and avoid
  redundant post-verification analysis tasks.
- Keep truly independent tasks independent so a scheduler can form parallel batches.
- Every FILE_WRITE task must have a downstream VERIFICATION task that depends
  on it and performs a deterministic test, diagnostic, or observable check.
- Prefer 1-3 tasks for simple work and 4-7 only when complexity requires it.
- Do not include markdown fences or prose outside the JSON object.
"""

    def __init__(
        self,
        client: LlmClient,
        *,
        max_repair_attempts: int = 1,
        simple_goal_detector: Callable[[str], bool] | None = None,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts cannot be negative")
        self.client = client
        self.max_repair_attempts = max_repair_attempts
        self.simple_goal_detector = simple_goal_detector or _is_simple_goal
        self.last_raw_response = ""
        self.last_error = ""
        self.last_usage = TokenUsage(0, 0, 0)

    def create_plan(self, goal: str) -> ExecutionPlan:
        normalized = goal.strip()
        if not normalized:
            raise ValueError("goal cannot be empty")
        self.last_raw_response = ""
        self.last_error = ""
        self.last_usage = TokenUsage(0, 0, 0)
        if self.simple_goal_detector(normalized):
            plan = ExecutionPlan(
                normalized,
                [
                    Task(
                        "task_1",
                        normalized,
                        task_type=_infer_task_type(normalized),
                        acceptance_criteria=("The requested result is produced.",),
                    )
                ],
                summary="Direct execution for a simple goal",
                metadata={"source": "simple_rule"},
            )
            PlanValidator(require_tasks=True).validate(plan)
            return plan
        return self._generate(normalized, request_context="")

    def replan(self, plan: ExecutionPlan, failed_task: Task) -> ExecutionPlan:
        completed = []
        for task in plan.tasks:
            if task.status is not TaskStatus.COMPLETED:
                continue
            preview = task.result[:1_000]
            completed.append(
                f"- {task.id}: {task.description}\n  result: {preview}"
            )
        failure = failed_task.error or failed_task.result or "unknown failure"
        request_context = (
            "The previous plan failed and must be replaced.\n"
            f"Failed task: {failed_task.id} — {failed_task.description}\n"
            f"Failure: {failure}\n"
            "Completed work that may be reused:\n"
            + ("\n".join(completed) if completed else "- none")
            + "\nReturn a complete replacement plan, not a patch. Preserve completed "
            "task IDs when the exact work remains valid."
        )
        return self._generate(plan.goal, request_context=request_context)

    def revise_plan(self, plan: ExecutionPlan, feedback: str) -> ExecutionPlan:
        """Regenerate a complete plan from explicit pre-execution feedback."""

        normalized = str(feedback).strip()
        if not normalized:
            raise ValueError("plan revision feedback cannot be empty")
        request_context = (
            "The user reviewed the validated plan before execution and requested "
            "a revision.\n"
            "Current plan:\n"
            + plan.render()
            + "\n\nRequested changes:\n"
            + normalized
            + "\nReturn a complete replacement plan, not a patch."
        )
        revised = self._generate(plan.goal, request_context=request_context)
        revised.metadata.update(
            {
                "revision_of": plan.plan_id,
                "revision_feedback": normalized,
            }
        )
        return revised

    def _generate(self, goal: str, *, request_context: str) -> ExecutionPlan:
        user_prompt = f"Goal:\n{goal}"
        if request_context:
            user_prompt += "\n\nPlanning context:\n" + request_context
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        last_error = ""
        for attempt in range(self.max_repair_attempts + 1):
            try:
                response = self.client.chat(messages, [])
            except Exception as exc:
                self.last_error = f"planner model call failed: {type(exc).__name__}: {exc}"
                raise PlanGenerationError(self.last_error) from exc
            self.last_usage = TokenUsage(
                self.last_usage.input_tokens + max(0, response.input_tokens),
                self.last_usage.output_tokens + max(0, response.output_tokens),
                self.last_usage.cached_input_tokens
                + max(0, response.cached_input_tokens),
            )
            self.last_raw_response = response.content
            try:
                plan = self.parse_plan(goal, response.content)
                plan.metadata.update(
                    {
                        "source": "llm",
                        "repair_attempts": attempt,
                    }
                )
                self.last_error = ""
                return plan
            except (PlanValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                self.last_error = last_error
                if attempt >= self.max_repair_attempts:
                    break
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": (
                                "The proposed plan is invalid: "
                                + last_error
                                + "\nReturn a corrected complete JSON plan only."
                            ),
                        },
                    ]
                )
        raise PlanGenerationError(
            "planner did not produce a valid plan after "
            f"{self.max_repair_attempts + 1} attempt(s): {last_error}"
        )

    @staticmethod
    def parse_plan(goal: str, raw: str) -> ExecutionPlan:
        cleaned = _extract_json_object(raw)
        root = json.loads(cleaned)
        if not isinstance(root, dict):
            raise PlanValidationError("plan root must be a JSON object")
        items = root.get("tasks")
        if items is None:
            items = root.get("steps")
        if not isinstance(items, list) or not items:
            raise PlanValidationError("plan must contain a non-empty tasks array")

        tasks: list[Task] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise PlanValidationError(f"tasks[{index}] must be an object")
            task_id = _required_string(item, "id", index)
            description = _required_string(item, "description", index)
            dependencies = _string_array(
                item.get("dependencies", []),
                f"tasks[{index}].dependencies",
            )
            criteria = _string_array(
                item.get("acceptance_criteria", item.get("criteria", [])),
                f"tasks[{index}].acceptance_criteria",
            )
            raw_type = str(item.get("type", "ANALYSIS")).upper()
            try:
                task_type = TaskType(raw_type)
            except ValueError as exc:
                allowed = ", ".join(member.value for member in TaskType)
                raise PlanValidationError(
                    f"tasks[{index}].type must be one of: {allowed}"
                ) from exc
            tasks.append(
                Task(
                    task_id,
                    description,
                    dependencies,
                    task_type=task_type,
                    acceptance_criteria=criteria,
                )
            )

        plan = ExecutionPlan(
            goal,
            tasks,
            summary=str(root.get("summary", "")).strip() or goal,
        )
        PlanValidator(require_tasks=True).validate(plan)
        return plan


TaskExecutor = Callable[[Task, dict[str, str]], str]
TaskLifecycleHook = Callable[[ExecutionPlan, Task], None]


class PlanExecuteAgent:
    """Execute a validated DAG and allow one bounded replacement plan."""

    def __init__(
        self,
        planner: Planner,
        executor: TaskExecutor,
        *,
        concurrency: TaskConcurrencyPolicy | None = None,
        before_task: TaskLifecycleHook | None = None,
        after_task: TaskLifecycleHook | None = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.concurrency = concurrency or TaskConcurrencyPolicy()
        self.before_task = before_task or (lambda _plan, _task: None)
        self.after_task = after_task or (lambda _plan, _task: None)

    def run(self, goal: str) -> ExecutionPlan:
        plan = self.planner.create_plan(goal)
        PlanValidator(require_tasks=True).validate(plan)
        return self.execute(plan)

    def execute(self, plan: ExecutionPlan, *, allow_replan: bool = True) -> ExecutionPlan:
        PlanValidator(require_tasks=True).validate(plan)
        results = plan.completed_results()
        while not plan.is_finished():
            ready = plan.ready_tasks()
            if not ready:
                marked = DagScheduler.mark_blocked(plan)
                if marked:
                    continue
                # A valid DAG should not reach this branch. Preserve a terminal,
                # diagnosable state rather than return an unfinished plan.
                for task in plan.tasks:
                    if task.status is TaskStatus.PENDING:
                        task.mark_skipped("no executable task remained")
                break

            for wave in self.concurrency.execution_waves(ready):
                failures = self._execute_wave(plan, wave, dict(results))
                for task in wave:
                    if task.status is TaskStatus.COMPLETED:
                        results[task.id] = task.result

                if failures and allow_replan:
                    failed_task = failures[0]
                    try:
                        replacement = self.planner.replan(plan, failed_task)
                    except Exception as replan_error:
                        failed_task.error += (
                            "; replan failed: "
                            f"{type(replan_error).__name__}: {replan_error}"
                        )
                    else:
                        if replacement is not plan:
                            PlanValidator(require_tasks=True).validate(replacement)
                            replacement.inherit_completed_from(plan)
                            plan_setter = getattr(self.executor, "set_plan", None)
                            if callable(plan_setter):
                                plan_setter(replacement)
                            return self.execute(replacement, allow_replan=False)

                if failures:
                    DagScheduler.mark_blocked(plan)
                # Other tasks in the same ready layer remain eligible even when
                # one independent branch failed.
        return plan

    def _execute_wave(
        self,
        plan: ExecutionPlan,
        wave: list[Task],
        results: dict[str, str],
    ) -> list[Task]:
        for task in wave:
            self.before_task(plan, task)
            task.mark_running()

        if len(wave) == 1:
            executions: list[tuple[Task, str | None, BaseException | None]] = []
            task = wave[0]
            try:
                value = self.executor(task, dict(results))
            except Exception as exc:  # normalized into task state below
                executions.append((task, None, exc))
            else:
                executions.append((task, str(value), None))
        else:
            executor = ThreadPoolExecutor(
                max_workers=min(self.concurrency.max_workers, len(wave)),
                thread_name_prefix="paicli-plan-task",
            )
            futures = {
                task.id: executor.submit(
                    contextvars.copy_context().run,
                    self.executor,
                    task,
                    dict(results),
                )
                for task in wave
            }
            executions = []
            try:
                for task in wave:
                    try:
                        value = futures[task.id].result()
                    except Exception as exc:  # keep stable task ordering
                        executions.append((task, None, exc))
                    else:
                        executions.append((task, str(value), None))
            finally:
                executor.shutdown(wait=True, cancel_futures=False)

        failures: list[Task] = []
        for task, value, error in executions:
            if error is not None:
                task.mark_failed(f"{type(error).__name__}: {error}")
                failures.append(task)
            else:
                task.mark_completed(value or "")
            self.after_task(plan, task)
        return failures

    @staticmethod
    def _skip_blocked(plan: ExecutionPlan) -> None:
        # Backward-compatible helper retained for older examples.
        DagScheduler.mark_blocked(plan)


class StaticPlanner:
    """Clone predefined tasks for deterministic tests and examples."""

    def __init__(self, tasks: Iterable[Task]) -> None:
        self.tasks = list(tasks)

    def create_plan(self, goal: str) -> ExecutionPlan:
        plan = ExecutionPlan(
            goal,
            [task.clone(reset_runtime=True) for task in self.tasks],
            summary="Static test plan",
            metadata={"source": "static"},
        )
        PlanValidator(require_tasks=True).validate(plan)
        return plan

    def replan(self, plan: ExecutionPlan, failed_task: Task) -> ExecutionPlan:
        del failed_task
        return plan


def _extract_json_object(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise PlanValidationError("planner returned empty content")
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise PlanValidationError("planner output does not contain a JSON object")
    return cleaned[start : end + 1]


def _required_string(item: dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(f"tasks[{index}].{key} must be a non-empty string")
    return value.strip()


def _string_array(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PlanValidationError(f"{path} must be a string array")
    if any(not item.strip() for item in value):
        raise PlanValidationError(f"{path} cannot contain empty values")
    return tuple(item.strip() for item in value)


def _is_simple_goal(goal: str) -> bool:
    normalized = " " + re.sub(r"\s+", " ", goal.strip().lower()) + " "
    if len(goal) > 100 or "\n" in goal:
        return False
    complex_markers = (
        " then ",
        " and ",
        " after ",
        " before ",
        "同时",
        "然后",
        "接着",
        "并且",
        "再",
        ";",
        "；",
        "1.",
        "2.",
    )
    if any(marker in normalized for marker in complex_markers):
        return False
    # Fail closed: only obvious single observation/command requests bypass the
    # LLM planner. A short sentence can still describe a repository-wide
    # refactor, so absence of conjunctions alone is not enough evidence.
    simple_prefixes = (
        "read ",
        "show ",
        "list ",
        "print ",
        "open ",
        "查看",
        "读取",
        "列出",
        "显示",
    )
    stripped = goal.strip().lower()
    return any(stripped.startswith(prefix) for prefix in simple_prefixes)


def _infer_task_type(goal: str) -> TaskType:
    lower = goal.lower()
    if any(word in lower for word in ("test", "verify", "验证", "测试", "检查结果")):
        return TaskType.VERIFICATION
    if any(word in lower for word in ("write", "edit", "modify", "create", "修改", "编写", "创建")):
        return TaskType.FILE_WRITE
    if any(word in lower for word in ("read", "inspect", "查看", "读取", "搜索代码")):
        return TaskType.FILE_READ
    if any(word in lower for word in ("run", "command", "list directory", "执行命令", "列出目录")):
        return TaskType.COMMAND
    return TaskType.ANALYSIS


__all__ = [
    "DagScheduler",
    "ExecutionPlan",
    "LlmPlanner",
    "PlanExecuteAgent",
    "PlanGenerationError",
    "PlanValidationError",
    "PlanValidator",
    "Planner",
    "StaticPlanner",
    "Task",
    "TaskExecutor",
    "TaskLifecycleHook",
    "TaskConcurrencyPolicy",
    "TaskStatus",
    "TaskType",
]
