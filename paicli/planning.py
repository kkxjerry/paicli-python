"""Phase 2：带依赖关系的 Plan-and-Execute。

DAG 是 Directed Acyclic Graph（有向无环图）：

    inspect -> edit -> test

箭头表示“前一个任务完成后，后一个任务才能执行”。
“有向”代表依赖有先后方向，“无环”代表不能出现 A -> B -> A。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Protocol


class TaskStatus(str, Enum):
    """任务状态机：PENDING -> RUNNING -> COMPLETED/FAILED。

    SKIPPED 表示任务本身没有执行，但它的前置依赖失败了。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Task:
    """计划中的一个任务节点。"""

    id: str
    description: str
    # 当前任务开始前必须完成的任务 ID。空元组表示没有依赖。
    dependencies: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""


@dataclass
class ExecutionPlan:
    """一张由 Task 组成的 DAG，同时负责校验和查找可执行任务。"""

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
        # 所有任务都进入终态时，整个计划才算结束。
        return all(
            task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}
            for task in self.tasks
        )

    def render(self) -> str:
        # 把计划转成人类易读的文本，不参与真正的执行。
        lines = [f"Goal: {self.goal}"]
        for task in self.tasks:
            deps = f" <- {', '.join(task.dependencies)}" if task.dependencies else ""
            lines.append(f"[{task.status.value:9}] {task.id}: {task.description}{deps}")
        return "\n".join(lines)


class Planner(Protocol):
    """只约定 Planner 必须提供哪些方法，不提供具体实现。"""

    def create_plan(self, goal: str) -> ExecutionPlan:
        """把一个大目标拆成带依赖关系的任务图。"""

    def replan(self, plan: ExecutionPlan, failed_task: Task) -> ExecutionPlan:
        """任务失败时尝试生成替代计划；本期的 StaticPlanner 不会重新规划。"""


# 执行器是一个函数：接收当前任务和已完成结果，返回当前任务的字符串结果。
TaskExecutor = Callable[[Task, dict[str, str]], str]


class PlanExecuteAgent:
    """按 DAG 依赖顺序执行计划，并最多允许重新规划一次。"""

    def __init__(self, planner: Planner, executor: TaskExecutor) -> None:
        self.planner = planner
        self.executor = executor

    def run(self, goal: str) -> ExecutionPlan:
        # Plan：先把大目标拆成任务图。
        plan = self.planner.create_plan(goal)
        # Execute：再按依赖关系执行任务图。
        return self.execute(plan)

    def execute(self, plan: ExecutionPlan, *, allow_replan: bool = True) -> ExecutionPlan:
        # 保存已完成任务的结果，后续任务可以把它们当作输入。
        results: dict[str, str] = {}
        while not plan.is_finished():
            # 每轮只取“所有依赖都已完成”的 PENDING 任务。
            ready = plan.ready_tasks()
            if not ready:
                # 计划还没结束却无任务可执行，说明剩余任务被失败依赖阻断。
                self._skip_blocked(plan)
                break

            for task in ready:
                task.status = TaskStatus.RUNNING
                try:
                    # 例如测试中传入 fail，这里就等价于 fail(task, results)。
                    task.result = self.executor(task, dict(results))
                    task.status = TaskStatus.COMPLETED
                    results[task.id] = task.result
                except Exception as exc:
                    # 执行器抛异常时，异常不继续向外抛，而是记录在任务中。
                    task.result = f"{type(exc).__name__}: {exc}"
                    task.status = TaskStatus.FAILED
                    if allow_replan:
                        # 如果 Planner 给出了一个新对象，就执行新计划，但禁止再次重新规划。
                        replacement = self.planner.replan(plan, task)
                        if replacement is not plan:
                            return self.execute(replacement, allow_replan=False)
                    # 没有替代计划时，将依赖失败任务的后续任务全部跳过。
                    self._skip_blocked(plan)
                    return plan
        return plan

    @staticmethod
    def _skip_blocked(plan: ExecutionPlan) -> None:
        # failed 不仅保存真正 FAILED 的任务，后面还会加入“因它们而被阻断”的任务。
        failed = {task.id for task in plan.tasks if task.status is TaskStatus.FAILED}
        changed = True
        # 需要多轮扫描才能传播间接依赖：A 失败 -> B 跳过 -> C 也跳过。
        while changed:
            changed = False
            for task in plan.tasks:
                # intersection 非空，表示当前任务至少依赖了一个已失败/被阻断的任务。
                if task.status is TaskStatus.PENDING and failed.intersection(
                    task.dependencies
                ):
                    task.status = TaskStatus.SKIPPED
                    task.result = "blocked by a failed dependency"
                    failed.add(task.id)
                    changed = True


class StaticPlanner:
    """使用事先写好的任务生成计划，适合示例和可重复的单元测试。"""

    def __init__(self, tasks: Iterable[Task]) -> None:
        self.tasks = list(tasks)

    def create_plan(self, goal: str) -> ExecutionPlan:
        # 重新创建 Task，避免上一次执行留下的状态污染新计划。
        return ExecutionPlan(
            goal,
            [
                Task(task.id, task.description, task.dependencies)
                for task in self.tasks
            ],
        )

    def replan(self, plan: ExecutionPlan, failed_task: Task) -> ExecutionPlan:
        # 返回原对象表示“不重新规划”。
        return plan
