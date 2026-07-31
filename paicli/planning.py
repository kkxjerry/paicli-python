"""Phase 2: dependency-aware Plan-and-Execute."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Protocol


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Task:
    id: str
    description: str
    # 当前任务开始前必须完成的任务 ID。空元组表示没有依赖。
    dependencies: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""


@dataclass
class ExecutionPlan:
    goal: str
    tasks: list[Task] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 任务 ID 是依赖关系的索引，因此不允许重复。
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")

        # 每个依赖都必须指向计划中已存在的其他任务。
        known = set(ids)
        for task in self.tasks:
            unknown = set(task.dependencies) - known
            if unknown:
                raise ValueError(f"task {task.id} has unknown dependencies: {unknown}")
            if task.id in task.dependencies:
                raise ValueError(f"task {task.id} cannot depend on itself")

        # 即使每条依赖都合法，仍可能形成 A -> B -> A 这样的环。
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        # visiting：当前递归路径上的任务，也就是“正在检查”。
        # visited：已经连同其所有依赖检查完毕，确认无环的任务。
        visiting: set[str] = set()
        visited: set[str] = set()
        # 将列表转为字典，便于通过依赖 ID 找到对应任务。
        by_id = {task.id: task for task in self.tasks}

        def visit(task_id: str) -> None:
            # 同一任务再次出现在当前路径上，说明依赖链绕回了起点。
            # 例如检查 A -> B -> A 时，第二次遇到 A 就会进入这个分支。
            if task_id in visiting:
                raise ValueError("task dependencies contain a cycle")

            # 该任务以前已经完整检查过，无需重复遍历。
            if task_id in visited:
                return

            # 进入当前任务，再递归检查它的每一个依赖。
            visiting.add(task_id)
            for dependency in by_id[task_id].dependencies:
                visit(dependency)

            # 所有依赖均已确认无环：离开当前路径，并标记为已检查。
            visiting.remove(task_id)
            visited.add(task_id)

        # 从每个任务出发，确保整张依赖图的每个部分都被检查到。
        for task_id in by_id:
            visit(task_id)

    def ready_tasks(self) -> list[Task]:
        # 只有当前任务的所有依赖都已完成，它才进入可执行列表。
        completed = {
            task.id for task in self.tasks if task.status is TaskStatus.COMPLETED
        }
        return [
            task
            for task in self.tasks
            if task.status is TaskStatus.PENDING
            and set(task.dependencies).issubset(completed)
        ]

    def is_finished(self) -> bool:
        return all(
            task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}
            for task in self.tasks
        )

    def render(self) -> str:
        lines = [f"Goal: {self.goal}"]
        for task in self.tasks:
            deps = f" <- {', '.join(task.dependencies)}" if task.dependencies else ""
            lines.append(f"[{task.status.value:9}] {task.id}: {task.description}{deps}")
        return "\n".join(lines)


class Planner(Protocol):
    def create_plan(self, goal: str) -> ExecutionPlan:
        """Turn a goal into a dependency graph."""

    def replan(self, plan: ExecutionPlan, failed_task: Task) -> ExecutionPlan:
        """Optionally replace a failed plan."""


TaskExecutor = Callable[[Task, dict[str, str]], str]


class PlanExecuteAgent:
    """Executes a plan in dependency order and supports one replan."""

    def __init__(self, planner: Planner, executor: TaskExecutor) -> None:
        self.planner = planner
        self.executor = executor

    def run(self, goal: str) -> ExecutionPlan:
        plan = self.planner.create_plan(goal)
        return self.execute(plan)

    def execute(self, plan: ExecutionPlan, *, allow_replan: bool = True) -> ExecutionPlan:
        results: dict[str, str] = {}
        while not plan.is_finished():
            ready = plan.ready_tasks()
            if not ready:
                self._skip_blocked(plan)
                break

            for task in ready:
                task.status = TaskStatus.RUNNING
                try:
                    task.result = self.executor(task, dict(results))
                    task.status = TaskStatus.COMPLETED
                    results[task.id] = task.result
                except Exception as exc:
                    task.result = f"{type(exc).__name__}: {exc}"
                    task.status = TaskStatus.FAILED
                    if allow_replan:
                        replacement = self.planner.replan(plan, task)
                        if replacement is not plan:
                            return self.execute(replacement, allow_replan=False)
                    self._skip_blocked(plan)
                    return plan
        return plan

    @staticmethod
    def _skip_blocked(plan: ExecutionPlan) -> None:
        failed = {task.id for task in plan.tasks if task.status is TaskStatus.FAILED}
        changed = True
        while changed:
            changed = False
            for task in plan.tasks:
                if task.status is TaskStatus.PENDING and failed.intersection(
                    task.dependencies
                ):
                    task.status = TaskStatus.SKIPPED
                    task.result = "blocked by a failed dependency"
                    failed.add(task.id)
                    changed = True


class StaticPlanner:
    """Small planner useful for examples and deterministic tests."""

    def __init__(self, tasks: Iterable[Task]) -> None:
        self.tasks = list(tasks)

    def create_plan(self, goal: str) -> ExecutionPlan:
        return ExecutionPlan(
            goal,
            [
                Task(task.id, task.description, task.dependencies)
                for task in self.tasks
            ],
        )

    def replan(self, plan: ExecutionPlan, failed_task: Task) -> ExecutionPlan:
        return plan
