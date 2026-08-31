"""Real role-scoped LLM sub-agents built on the shared Agent loop.

A sub-agent is not a Python callback. It owns an independent conversation
history, budget, context compactor, and tool capability scope while sharing the
same model client, durable memory store, and validated ToolRegistry.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent import Agent
from .agents.models import (
    AgentOutcome,
    CompletionDecision,
    CompletionPolicy,
    FinishReason,
    NonEmptyCompletionPolicy,
    RunStatus,
)
from .context import ContextController, ContextSettings
from .llm_client import ChatResponse, LlmClient
from .lsp import LspManager
from .memory import ConversationHistoryCompactor, LongTermMemory, MemoryManager
from .observability import TraceStore, current_trace_context, traced_span
from .planning import ExecutionPlan, Task, TaskType
from .runtime import CancellationToken
from .tool_contracts import ToolResult, ToolSideEffect
from .tools import ScopedToolRuntime, ToolGateway, ToolRegistry

EventHandler = Callable[[str, str], None]


class SubAgentRole(str, Enum):
    WORKER = "worker"
    REVIEWER = "reviewer"
    AGGREGATOR = "aggregator"


class ToolScope(str, Enum):
    NONE = "none"
    READ_ONLY = "read_only"
    VERIFICATION = "verification"
    FULL = "full"


@dataclass(frozen=True)
class DependencyResult:
    task_id: str
    result: str
    changed_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskPacket:
    """The bounded handoff from the orchestrator to one worker.

    Only direct dependency outputs are included. Other workers' histories,
    unrelated plan nodes, and the parent conversation are deliberately absent.
    """

    plan_id: str
    goal: str
    task_id: str
    task_type: TaskType
    description: str
    dependencies: tuple[DependencyResult, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    attempt: int = 1
    review_feedback: tuple[str, ...] = ()
    run_id: str = ""

    @classmethod
    def from_task(
        cls,
        plan: ExecutionPlan,
        task: Task,
        completed_results: Mapping[str, str],
        *,
        changed_files: Mapping[str, tuple[str, ...]] | None = None,
        attempt: int = 1,
        review_feedback: tuple[str, ...] = (),
        run_id: str | None = None,
    ) -> TaskPacket:
        file_map = changed_files or {}
        dependencies = tuple(
            DependencyResult(
                dependency,
                str(completed_results[dependency]),
                tuple(file_map.get(dependency, ())),
            )
            for dependency in task.dependencies
            if dependency in completed_results
        )
        return cls(
            plan.plan_id,
            plan.goal,
            task.id,
            task.task_type,
            task.description,
            dependencies,
            task.acceptance_criteria,
            attempt,
            review_feedback,
            current_trace_context().run_id if run_id is None else run_id,
        )

    def render(self) -> str:
        payload = {
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "goal": self.goal,
            "task": {
                "id": self.task_id,
                "type": self.task_type.value,
                "description": self.description,
                "acceptance_criteria": list(self.acceptance_criteria),
                "attempt": self.attempt,
            },
            "direct_dependency_results": [
                {
                    "task_id": dependency.task_id,
                    "result": _truncate(dependency.result, 8_000),
                    "changed_files": list(dependency.changed_files),
                }
                for dependency in self.dependencies
            ],
            "review_feedback": list(self.review_feedback),
        }
        return (
            "Execute exactly the assigned task packet below. Do not redo "
            "unrelated plan nodes. Use tools when evidence or a repository "
            "change is required.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )


class TaskCompletionPolicy:
    """Require observable side effects for mutation and command tasks."""

    def __init__(self, task: Task, tools: ToolGateway) -> None:
        self.task = task
        self.tools = tools
        self.non_empty = NonEmptyCompletionPolicy()
        self._tool_results: list[ToolResult] = []

    def begin_run(self) -> None:
        """Discard evidence from an earlier reviewer-triggered retry."""

        self._tool_results.clear()

    def observe_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        """Receive machine-readable evidence from the shared loop."""

        self._tool_results.extend(results)

    def evaluate(
        self,
        response: ChatResponse,
        history: list[dict[str, Any]],
    ) -> CompletionDecision:
        base = self.non_empty.evaluate(response, history)
        if not base.completed:
            return base

        required_effects: set[ToolSideEffect]
        if self.task.task_type is TaskType.FILE_WRITE:
            required_effects = {
                ToolSideEffect.FILE_WRITE,
                ToolSideEffect.DIRECTORY_WRITE,
            }
        elif self.task.task_type is TaskType.COMMAND:
            required_effects = {ToolSideEffect.PROCESS}
        elif self.task.task_type is TaskType.FILE_READ:
            required_effects = {ToolSideEffect.READ_ONLY}
        elif self.task.task_type is TaskType.VERIFICATION:
            required_effects = {
                ToolSideEffect.READ_ONLY,
                ToolSideEffect.PROCESS,
            }
        else:
            return base

        # AgentLoopEngine calls begin_run() for every worker attempt, so prior
        # reviewer-rejected attempts cannot satisfy this completion gate.
        for result in self._tool_results:
            if not result.ok:
                continue
            spec = self.tools.spec(result.tool_name)
            if spec is None or spec.side_effect not in required_effects:
                continue
            # A FILE_WRITE task must produce an explicit repository artifact.
            # This prevents unrelated persistence tools such as save_memory from
            # satisfying the task merely because they also write to disk.
            if (
                self.task.task_type is TaskType.FILE_WRITE
                and not result.changed_files
            ):
                continue
            return base

        if self.task.task_type is TaskType.FILE_WRITE:
            feedback = (
                "This FILE_WRITE task has no successful file or directory write "
                "in the current attempt. Use an exposed mutation tool and verify "
                "its result before claiming completion."
            )
        elif self.task.task_type is TaskType.COMMAND:
            feedback = (
                "This COMMAND task has no successful process tool result in the "
                "current attempt. Execute the required command and inspect its "
                "exit status before claiming completion."
            )
        elif self.task.task_type is TaskType.FILE_READ:
            feedback = (
                "This FILE_READ task has no successful read-only tool observation "
                "in the current attempt. Inspect the requested source before "
                "claiming completion."
            )
        else:
            feedback = (
                "This VERIFICATION task has no successful read or process evidence "
                "in the current attempt. Perform a concrete check before claiming "
                "completion."
            )
        return CompletionDecision(False, feedback)


@dataclass
class SubAgent:
    """One isolated role instance with its own Agent history."""

    name: str
    role: SubAgentRole
    scope: ToolScope
    agent: Agent
    trace_store: TraceStore | None = None

    def run_task(self, packet: TaskPacket) -> AgentOutcome:
        if self.role is not SubAgentRole.WORKER:
            raise ValueError("run_task is only valid for worker sub-agents")
        with traced_span(
            self.trace_store,
            "agent",
            self.name,
            run_id=packet.run_id,
            task_id=packet.task_id,
            agent_role=self.role.value,
            agent_name=self.name,
            attributes={"tool_scope": self.scope.value},
        ):
            return self._run_safely(packet.render())

    def run_prompt(self, prompt: str) -> AgentOutcome:
        context = current_trace_context()
        with traced_span(
            self.trace_store,
            "agent",
            self.name,
            run_id=context.run_id,
            task_id=context.task_id,
            agent_role=self.role.value,
            agent_name=self.name,
            attributes={"tool_scope": self.scope.value},
        ):
            return self._run_safely(prompt)

    def _run_safely(self, prompt: str) -> AgentOutcome:
        try:
            return self.agent.run_outcome(prompt)
        except Exception as exc:
            # Orchestration state must remain complete even when a provider
            # request fails before AgentLoopEngine can create an outcome.
            return AgentOutcome(
                run_id=uuid.uuid4().hex,
                status=RunStatus.FAILED,
                finish_reason=FinishReason.INTERNAL_ERROR,
                content="",
                error=f"{type(exc).__name__}: {exc}",
            )

    @property
    def history(self) -> list[dict[str, Any]]:
        return self.agent.history


class SubAgentFactory:
    """Create independent sub-agents over shared infrastructure."""

    WORKER_SYSTEM_PROMPT = """You are an isolated coding-task worker.
You receive one structured task packet, not the whole parent conversation.
Stay inside that task and its direct dependency evidence. Use only the tools
exposed to you. A missing tool means the orchestrator intentionally restricted
this task's side effects. When finished, report concrete actions, evidence,
changed files, verification performed, and any unresolved limitation. Never
claim a command, test, or file change that did not occur."""

    REVIEWER_SYSTEM_PROMPT = """You are an independent coding-task reviewer.
Inspect the assigned task, acceptance criteria, worker evidence, tool results,
and changed files. Read-only repository tools may be available so you can
verify the artifact instead of trusting the worker's summary. Never modify the
workspace. Your final response must follow the review JSON schema requested by
the user message and must not contain hidden reasoning."""

    AGGREGATOR_SYSTEM_PROMPT = """You are the final result aggregator for a
coding-agent run. Synthesize only the supplied task states and evidence. Do not
claim failed or skipped work was completed. Clearly disclose unresolved errors.
Return a concise user-facing answer without tool calls."""

    def __init__(
        self,
        client: LlmClient,
        tools: ToolGateway,
        project_root: str | Path,
        *,
        long_term_memory: LongTermMemory | None = None,
        enable_memory: bool = True,
        max_steps: int = 12,
        stagnation_window: int = 3,
        token_budget: int | None = None,
        on_event: EventHandler | None = None,
        cancellation: CancellationToken | None = None,
        trace_store: TraceStore | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("sub-agent max_steps must be positive")
        self.client = client
        self.tools = tools
        self.project_root = Path(project_root).resolve()
        self.long_term_memory = long_term_memory
        self.enable_memory = enable_memory
        self.max_steps = max_steps
        self.stagnation_window = stagnation_window
        self.token_budget = token_budget
        self.on_event = on_event or (lambda _kind, _text: None)
        self.cancellation = cancellation or CancellationToken()
        self.trace_store = trace_store
        self._event_lock = threading.Lock()

    def set_client(self, client: LlmClient) -> None:
        self.client = client

    def create_worker(self, task: Task, *, name: str | None = None) -> SubAgent:
        scope = self.scope_for_task(task)
        return self.create(
            name=name or f"worker-{task.id}",
            role=SubAgentRole.WORKER,
            scope=scope,
            system_prompt=self.WORKER_SYSTEM_PROMPT,
            completion_policy=TaskCompletionPolicy(task, self.tools),
        )

    def create_reviewer(self, *, name: str = "reviewer") -> SubAgent:
        return self.create(
            name=name,
            role=SubAgentRole.REVIEWER,
            scope=ToolScope.READ_ONLY,
            system_prompt=self.REVIEWER_SYSTEM_PROMPT,
        )

    def create_aggregator(self, *, name: str = "aggregator") -> SubAgent:
        return self.create(
            name=name,
            role=SubAgentRole.AGGREGATOR,
            scope=ToolScope.NONE,
            system_prompt=self.AGGREGATOR_SYSTEM_PROMPT,
            enable_memory=False,
        )

    def create(
        self,
        *,
        name: str,
        role: SubAgentRole,
        scope: ToolScope,
        system_prompt: str,
        completion_policy: CompletionPolicy | None = None,
        enable_memory: bool | None = None,
    ) -> SubAgent:
        tool_runtime = self._tool_runtime(scope, name)
        settings = self._settings()
        use_memory = self.enable_memory if enable_memory is None else enable_memory
        memory: MemoryManager | None = None
        if use_memory:
            memory = MemoryManager(
                max_tokens=settings.compression_trigger_tokens,
                long_term=self.long_term_memory,
                history_compactor=ConversationHistoryCompactor(self.client),
                long_term_context_tokens=settings.memory_context_tokens,
            )
        context = ContextController(settings)
        lsp = (
            LspManager(self.project_root)
            if "write_file" in tool_runtime.allowed_names
            else None
        )

        def emit(kind: str, text: str) -> None:
            # Multiple read-only workers may finish at the same time. Serialize
            # renderer callbacks so their terminal output is not interleaved.
            with self._event_lock:
                self.on_event(kind, f"[{name}] {text}")

        agent = Agent(
            self.client,
            tool_runtime,
            max_steps=self.max_steps,
            stagnation_window=self.stagnation_window,
            token_budget=self.token_budget,
            on_event=emit,
            memory=memory,
            context=context,
            lsp=lsp,
            cancellation=self.cancellation,
            completion_policy=completion_policy,
            system_prompt=system_prompt,
        )
        return SubAgent(name, role, scope, agent, self.trace_store)

    def scope_for_task(self, task: Task) -> ToolScope:
        if task.task_type in {TaskType.FILE_READ, TaskType.ANALYSIS}:
            return ToolScope.READ_ONLY
        if task.task_type is TaskType.VERIFICATION:
            return ToolScope.VERIFICATION
        return ToolScope.FULL

    def _tool_runtime(self, scope: ToolScope, name: str) -> ScopedToolRuntime:
        if scope is ToolScope.NONE:
            allowed: set[str] = set()
        elif scope is ToolScope.FULL:
            allowed = set(self.tools.names())
        else:
            allowed = set()
            for tool_name in self.tools.names():
                spec = self.tools.spec(tool_name)
                if spec is not None and spec.side_effect is ToolSideEffect.READ_ONLY:
                    allowed.add(tool_name)
            if scope is ToolScope.VERIFICATION and "execute_command" in self.tools.names():
                allowed.add("execute_command")
        return ScopedToolRuntime(
            self.tools,
            allowed,
            scope_name=f"{name}:{scope.value}",
        )

    def _settings(self) -> ContextSettings:
        raw_window = getattr(self.client, "context_window", 128_000)
        try:
            context_window = max(8_000, int(raw_window))
        except (TypeError, ValueError):
            context_window = 128_000
        return ContextSettings.for_model(
            context_window,
            supports_prompt_caching=bool(
                getattr(self.client, "supports_prompt_caching", False)
            ),
        )


def _truncate(value: str, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(truncated)"


__all__ = [
    "DependencyResult",
    "SubAgent",
    "SubAgentFactory",
    "SubAgentRole",
    "TaskCompletionPolicy",
    "TaskPacket",
    "ToolScope",
]
