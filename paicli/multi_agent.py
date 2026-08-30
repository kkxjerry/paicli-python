"""Legacy Phase 5 educational callback-based multi-agent example.

The production-facing Phase 7 implementation lives in ``subagents.py``,
``review.py``, and ``orchestration.py``. It uses real isolated LLM Agents and a
structured reviewer gate. The classes below remain unchanged for readers and
older tests that follow the original 22-phase learning history.

    ExecutionPlan(DAG)
            |
            v
    AgentOrchestrator 选择可执行任务
            |
            v
    assignments 决定交给哪个角色
            |
            v
    Worker 执行并通过 MessageBus 广播结果

本期的 Worker 只是 Python 函数，不是多个独立 LLM 实例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .planning import ExecutionPlan, Task, TaskStatus


class AgentRole(str, Enum):
    """团队内预置的职责类型。"""

    PLANNER = "planner"
    RESEARCHER = "researcher"
    CODER = "coder"
    REVIEWER = "reviewer"


@dataclass(frozen=True)
class AgentMessage:
    """角色之间传递的不可变消息。"""

    sender: AgentRole
    recipient: AgentRole
    content: str
    task_id: str = ""


class MessageBus:
    """最小内存消息总线：保留全部消息，按接收角色过滤。"""

    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []

    def send(self, message: AgentMessage) -> None:
        # 本期没有队列/网络，发送只是追加到内存 list。
        self.messages.append(message)

    def for_role(self, role: AgentRole) -> list[AgentMessage]:
        return [message for message in self.messages if message.recipient is role]


# Worker 接收任务和发给本角色的历史消息，返回执行结果。
Worker = Callable[[Task, list[AgentMessage]], str]


@dataclass
class AgentTeam:
    """维护“角色 -> Worker 函数”的映射。"""

    workers: dict[AgentRole, Worker] = field(default_factory=dict)

    def register(self, role: AgentRole, worker: Worker) -> None:
        if role in self.workers:
            raise ValueError(f"worker already registered for {role.value}")
        self.workers[role] = worker


class AgentOrchestrator:
    """把 DAG 任务分配给角色，并通过消息总线共享结果。"""

    def __init__(
        self,
        team: AgentTeam,
        assignments: dict[str, AgentRole],
        *,
        bus: MessageBus | None = None,
    ) -> None:
        self.team = team
        self.assignments = assignments
        self.bus = bus or MessageBus()

    def run(self, plan: ExecutionPlan) -> ExecutionPlan:
        while not plan.is_finished():
            # 复用 Phase 02 的 DAG 逻辑：只处理依赖全部完成的任务。
            ready = plan.ready_tasks()
            if not ready:
                # 仍有 PENDING 却无可执行任务，剩余任务已无法继续。
                self._skip_pending(plan)
                break
            for task in ready:
                self._run_task(task)
                if task.status is TaskStatus.FAILED:
                    self._skip_pending(plan)
                    return plan
        return plan

    def _run_task(self, task: Task) -> None:
        # 未显式分配的任务默认交给 CODER。
        role = self.assignments.get(task.id, AgentRole.CODER)
        worker = self.team.workers.get(role)
        if worker is None:
            # 任务指定了角色，但团队没有为该角色注册执行函数。
            task.status = TaskStatus.FAILED
            task.result = f"no worker registered for {role.value}"
            return

        task.status = TaskStatus.RUNNING
        try:
            # 只把“接收者是当前角色”的消息交给 Worker。
            task.result = worker(task, self.bus.for_role(role))
            task.status = TaskStatus.COMPLETED
            # 任务成功后，将结果发给团队中其他所有角色。
            for recipient in self.team.workers:
                if recipient is not role:
                    self.bus.send(
                        AgentMessage(role, recipient, task.result, task.id)
                    )
        except Exception as exc:
            task.result = f"{type(exc).__name__}: {exc}"
            task.status = TaskStatus.FAILED

    @staticmethod
    def _skip_pending(plan: ExecutionPlan) -> None:
        # 任意任务失败后直接跳过所有剩余任务，比 Phase 02 的精确依赖传播更粗粒度。
        for task in plan.tasks:
            if task.status is TaskStatus.PENDING:
                task.status = TaskStatus.SKIPPED
                task.result = "blocked because the team could not continue"
