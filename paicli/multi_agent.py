"""Phase 5: role-based multi-agent coordination."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .planning import ExecutionPlan, Task, TaskStatus


class AgentRole(str, Enum):
    PLANNER = "planner"
    RESEARCHER = "researcher"
    CODER = "coder"
    REVIEWER = "reviewer"


@dataclass(frozen=True)
class AgentMessage:
    sender: AgentRole
    recipient: AgentRole
    content: str
    task_id: str = ""


class MessageBus:
    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []

    def send(self, message: AgentMessage) -> None:
        self.messages.append(message)

    def for_role(self, role: AgentRole) -> list[AgentMessage]:
        return [message for message in self.messages if message.recipient is role]


Worker = Callable[[Task, list[AgentMessage]], str]


@dataclass
class AgentTeam:
    workers: dict[AgentRole, Worker] = field(default_factory=dict)

    def register(self, role: AgentRole, worker: Worker) -> None:
        if role in self.workers:
            raise ValueError(f"worker already registered for {role.value}")
        self.workers[role] = worker


class AgentOrchestrator:
    """Assigns DAG tasks to roles and shares results through a message bus."""

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
            ready = plan.ready_tasks()
            if not ready:
                self._skip_pending(plan)
                break
            for task in ready:
                self._run_task(task)
                if task.status is TaskStatus.FAILED:
                    self._skip_pending(plan)
                    return plan
        return plan

    def _run_task(self, task: Task) -> None:
        role = self.assignments.get(task.id, AgentRole.CODER)
        worker = self.team.workers.get(role)
        if worker is None:
            task.status = TaskStatus.FAILED
            task.result = f"no worker registered for {role.value}"
            return

        task.status = TaskStatus.RUNNING
        try:
            task.result = worker(task, self.bus.for_role(role))
            task.status = TaskStatus.COMPLETED
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
        for task in plan.tasks:
            if task.status is TaskStatus.PENDING:
                task.status = TaskStatus.SKIPPED
                task.result = "blocked because the team could not continue"
