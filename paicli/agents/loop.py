"""The shared model -> tool -> observation loop.

Phase 1 moves the execution kernel out of ``paicli.agent.Agent``.  ReAct is the
first caller; Plan workers and role-based sub-agents can reuse the same engine
in later phases without copying cancellation, history, tool-result, budget,
and completion behavior.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Protocol

from ..context import ContextController
from ..llm_client import LlmClient, ToolCall
from ..lsp import LspDiagnosticFormatter, LspManager
from ..memory import MemoryManager
from ..runtime import CancelledError, CancellationToken
from ..tool_contracts import ToolResult
from .budget import AgentBudget, BudgetExitReason
from .models import (
    AgentOutcome,
    CompletionPolicy,
    FinishReason,
    NonEmptyCompletionPolicy,
    RunStatus,
)

EventHandler = Callable[[str, str], None]


class ToolRuntime(Protocol):
    """Minimum tool boundary required by the shared loop."""

    def definitions(self) -> list[dict[str, Any]]:
        """Return model-visible function definitions."""
        ...

    def execute(self, name: str, arguments_json: str) -> str:
        """Execute one call; batch execution remains an optional capability."""
        ...


class AgentLoopEngine:
    """Execute one Agent run against caller-owned history and tools."""

    def __init__(
        self,
        client: LlmClient,
        tools: ToolRuntime,
        history: list[dict[str, Any]],
        *,
        max_iterations: int = AgentBudget.DEFAULT_HARD_MAX_ITERATIONS,
        token_budget: int | None = None,
        stagnation_window: int = AgentBudget.DEFAULT_STAGNATION_WINDOW,
        on_event: EventHandler | None = None,
        memory: MemoryManager | None = None,
        cancellation: CancellationToken | None = None,
        context: ContextController | None = None,
        lsp: LspManager | None = None,
        completion_policy: CompletionPolicy | None = None,
    ) -> None:
        # AgentBudget owns the canonical validation of all limits. Create a
        # throwaway instance here so configuration errors fail during assembly,
        # not only after the user submits a prompt.
        AgentBudget(
            token_budget=token_budget,
            stagnation_window=stagnation_window,
            hard_max_iterations=max_iterations,
        )
        self.client = client
        self.tools = tools
        self.history = history
        self.max_iterations = max_iterations
        self.token_budget = token_budget
        self.stagnation_window = stagnation_window
        self.on_event = on_event or (lambda _kind, _text: None)
        self.memory = memory
        self.cancellation = cancellation or CancellationToken()
        self.context = context
        self.lsp = lsp
        self.completion_policy = completion_policy or NonEmptyCompletionPolicy()

    def run(self, user_message: dict[str, Any]) -> AgentOutcome:
        """Run until completion policy accepts a response or a budget stops it."""

        run_id = uuid.uuid4().hex
        budget = AgentBudget(
            token_budget=self.token_budget,
            stagnation_window=self.stagnation_window,
            hard_max_iterations=self.max_iterations,
        )
        changed_files: list[str] = []
        tool_results: list[ToolResult] = []
        begin_policy_run = getattr(self.completion_policy, "begin_run", None)
        if callable(begin_policy_run):
            begin_policy_run()
        self.history.append(dict(user_message))

        while True:
            # An explicit user cancellation takes precedence over an automatic
            # budget diagnosis if both become visible at the same checkpoint.
            cancelled = self._cancelled_outcome(
                run_id,
                budget,
                changed_files,
                tool_results,
            )
            if cancelled is not None:
                return cancelled

            exit_reason = budget.check()
            if exit_reason is not BudgetExitReason.WITHIN_BUDGET:
                outcome = self._budget_outcome(
                    run_id,
                    budget,
                    exit_reason,
                    changed_files,
                    tool_results,
                )
                return outcome

            budget.begin_iteration()
            messages = self._prepare_messages()
            response = self.client.chat(messages, self.tools.definitions())
            budget.record_response(response)

            # Cancellation may arrive while the provider request is in flight.
            # Check before accepting an answer or executing any requested side
            # effect; do not leave an assistant tool-call message dangling.
            cancelled = self._cancelled_outcome(
                run_id,
                budget,
                changed_files,
                tool_results,
            )
            if cancelled is not None:
                return cancelled

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.content,
            }
            if response.tool_calls:
                assistant_message["tool_calls"] = [
                    call.as_message_dict() for call in response.tool_calls
                ]
            self.history.append(assistant_message)

            if not response.tool_calls:
                decision = self.completion_policy.evaluate(response, self.history)
                if decision.completed:
                    self.on_event("answer", response.content)
                    return AgentOutcome(
                        run_id=run_id,
                        status=RunStatus.SUCCEEDED,
                        finish_reason=FinishReason.FINAL_ANSWER,
                        content=response.content,
                        usage=budget.usage,
                        iterations=budget.iteration,
                        changed_files=tuple(changed_files),
                        tool_results=tuple(tool_results),
                    )

                feedback = decision.feedback.strip() or (
                    "Completion validation failed. Continue working before "
                    "returning a final answer."
                )
                self.on_event("validation", feedback)
                budget.record_completion_rejection(response.content, feedback)
                # This is framework feedback, not a fabricated user request.
                # A system message makes that provenance explicit to the model.
                self.history.append(
                    {
                        "role": "system",
                        "content": f"Completion validation failed: {feedback}",
                    }
                )
                continue

            for call in response.tool_calls:
                self.on_event("tool", f"{call.name} {call.arguments}")

            results = self._execute_tools(response.tool_calls)
            if len(results) != len(response.tool_calls):
                raise RuntimeError(
                    "tool executor returned a different number of results "
                    f"({len(results)}) than calls ({len(response.tool_calls)})"
                )

            tool_results.extend(results)
            observe_results = getattr(
                self.completion_policy,
                "observe_tool_results",
                None,
            )
            if callable(observe_results):
                observe_results(tuple(results))
            for call, result in zip(response.tool_calls, results, strict=True):
                self.on_event("result", result.content)
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result.content,
                    }
                )
                for changed_path in result.changed_files:
                    if changed_path not in changed_files:
                        changed_files.append(changed_path)
                if result.ok and result.changed_files:
                    self._report_diagnostics(call.name, call.arguments)

            budget.record_tool_round(
                response.tool_calls,
                [result.content for result in results],
            )
            # If cancellation arrived during the batch, preserve the complete
            # assistant/tool protocol in history, then stop before a new model
            # request is issued.
            cancelled = self._cancelled_outcome(
                run_id,
                budget,
                changed_files,
                tool_results,
            )
            if cancelled is not None:
                return cancelled

    def _cancelled_outcome(
        self,
        run_id: str,
        budget: AgentBudget,
        changed_files: list[str],
        tool_results: list[ToolResult],
    ) -> AgentOutcome | None:
        try:
            self.cancellation.check()
        except CancelledError as exc:
            return AgentOutcome(
                run_id=run_id,
                status=RunStatus.CANCELLED,
                finish_reason=FinishReason.CANCELLED,
                content="",
                usage=budget.usage,
                iterations=budget.iteration,
                error=str(exc),
                changed_files=tuple(changed_files),
                tool_results=tuple(tool_results),
            )
        return None

    def _prepare_messages(self) -> list[dict[str, Any]]:
        if self.context:
            return self.context.prepare(self.history, self.memory)
        if self.memory:
            return self.memory.prepare(self.history)
        return self.history

    def _execute_tools(self, calls: tuple[ToolCall, ...]) -> list[ToolResult]:
        serialized = [(call.name, call.arguments) for call in calls]

        execute_many_results = getattr(self.tools, "execute_many_results", None)
        if callable(execute_many_results):
            raw_results = list(execute_many_results(serialized))
            return _coerce_batch_results(calls, raw_results)

        execute_many = getattr(self.tools, "execute_many", None)
        if callable(execute_many):
            raw_results = list(execute_many(serialized))
            return _coerce_batch_results(calls, raw_results)

        execute_result = getattr(self.tools, "execute_result", None)
        if callable(execute_result):
            return [
                _coerce_tool_result(
                    call.name,
                    execute_result(call.name, call.arguments),
                ).with_call_id(call.id)
                for call in calls
            ]
        return [
            ToolResult.success(
                call.name,
                self.tools.execute(call.name, call.arguments),
            ).with_call_id(call.id)
            for call in calls
        ]

    def _report_diagnostics(self, tool_name: str, arguments_json: str) -> None:
        if self.lsp is None or tool_name != "write_file":
            return
        try:
            path = str(json.loads(arguments_json)["path"])
            report = self.lsp.diagnostics_for(path)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return
        if report.diagnostics:
            self.on_event("diagnostics", LspDiagnosticFormatter.format(report))

    @staticmethod
    def _budget_outcome(
        run_id: str,
        budget: AgentBudget,
        reason: BudgetExitReason,
        changed_files: list[str],
        tool_results: list[ToolResult],
    ) -> AgentOutcome:
        finish_reason = {
            BudgetExitReason.HARD_ITERATION_LIMIT: FinishReason.MAX_ITERATIONS,
            BudgetExitReason.STAGNATION_DETECTED: FinishReason.STAGNATION,
            BudgetExitReason.TOKEN_BUDGET_EXCEEDED: FinishReason.TOKEN_BUDGET,
        }[reason]
        return AgentOutcome(
            run_id=run_id,
            status=RunStatus.STOPPED,
            finish_reason=finish_reason,
            content="",
            usage=budget.usage,
            iterations=budget.iteration,
            error=budget.describe_exit(reason),
            changed_files=tuple(changed_files),
            tool_results=tuple(tool_results),
        )


def _coerce_batch_results(
    calls: tuple[ToolCall, ...],
    values: list[object],
) -> list[ToolResult]:
    if len(values) != len(calls):
        raise RuntimeError(
            "tool executor returned a different number of results "
            f"({len(values)}) than calls ({len(calls)})"
        )
    return [
        _coerce_tool_result(call.name, value).with_call_id(call.id)
        for call, value in zip(calls, values, strict=True)
    ]


def _coerce_tool_result(tool_name: str, value: object) -> ToolResult:
    """Adapt legacy string runtimes to the structured gateway contract."""

    if isinstance(value, ToolResult):
        return value
    return ToolResult.success(tool_name, str(value))
