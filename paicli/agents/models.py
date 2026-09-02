"""Shared contracts for every Agent execution mode.

Phase 1 introduces these contracts before Plan/Team are wired into the Python
CLI.  ReAct, future plan workers, and future sub-agents can therefore return
the same structured outcome instead of inventing mode-specific strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ..context import TokenUsage
from ..llm_client import ChatResponse
from ..tool_contracts import ToolResult


class RunStatus(str, Enum):
    """High-level state of one Agent run."""

    SUCCEEDED = "succeeded"
    STOPPED = "stopped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FinishReason(str, Enum):
    """Why the loop returned control to its caller.

    Some values are reserved for later phases so all execution modes can share
    one stable protocol without another breaking data-model migration.
    """

    FINAL_ANSWER = "final_answer"
    MAX_ITERATIONS = "max_iterations"
    STAGNATION = "stagnation"
    TOKEN_BUDGET = "token_budget"
    CANCELLED = "cancelled"
    TOOL_FAILURE = "tool_failure"
    VALIDATION_FAILED = "validation_failed"
    REVIEW_REJECTED = "review_rejected"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class AgentOutcome:
    """Structured result produced by :class:`AgentLoopEngine`."""

    run_id: str
    status: RunStatus
    finish_reason: FinishReason
    content: str
    usage: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0, 0))
    iterations: int = 0
    error: str = ""
    changed_files: tuple[str, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    streamed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.SUCCEEDED


@dataclass(frozen=True)
class CompletionDecision:
    """A deterministic gate applied after the model stops calling tools."""

    completed: bool
    feedback: str = ""


class CompletionPolicy(Protocol):
    """Decide whether a no-tool assistant response may finish the run."""

    def evaluate(
        self,
        response: ChatResponse,
        history: list[dict[str, Any]],
    ) -> CompletionDecision:
        """Return a decision and optional feedback for another model turn."""
        ...


class ToolObservingCompletionPolicy(CompletionPolicy, Protocol):
    """Optional lifecycle hooks for policies that need tool evidence."""

    def begin_run(self) -> None:
        """Reset evidence that must not leak across Agent runs."""
        ...

    def observe_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        """Receive machine-readable results from the current run."""
        ...


class DiagnosticsCompletionPolicy:
    """Prevent completion while post-edit diagnostics still contain errors."""

    def __init__(self, delegate: CompletionPolicy) -> None:
        self.delegate = delegate
        self._reports: dict[str, Any] = {}

    def begin_run(self) -> None:
        self._reports.clear()
        begin = getattr(self.delegate, "begin_run", None)
        if callable(begin):
            begin()

    def observe_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        observe = getattr(self.delegate, "observe_tool_results", None)
        if callable(observe):
            observe(results)

    def observe_diagnostics(self, reports: tuple[Any, ...]) -> None:
        for report in reports:
            path = str(getattr(report, "path", ""))
            if path:
                self._reports[path] = report
        observe = getattr(self.delegate, "observe_diagnostics", None)
        if callable(observe):
            observe(reports)

    def evaluate(
        self,
        response: ChatResponse,
        history: list[dict[str, Any]],
    ) -> CompletionDecision:
        decision = self.delegate.evaluate(response, history)
        if not decision.completed:
            return decision
        errors: list[str] = []
        for path, report in self._reports.items():
            if not bool(getattr(report, "has_errors", False)):
                continue
            diagnostics = getattr(report, "diagnostics", ())
            details = "; ".join(
                f"line {getattr(item, 'line', '?')}:{getattr(item, 'column', '?')} "
                f"{getattr(item, 'message', item)}"
                for item in diagnostics
            )
            errors.append(f"{path}: {details}")
        if not errors:
            return decision
        return CompletionDecision(
            False,
            "Post-edit diagnostics still contain errors. Fix them before "
            "finishing:\n" + "\n".join(f"- {item}" for item in errors),
        )


class NonEmptyCompletionPolicy:
    """Minimum completion gate: an empty answer is not a completed task.

    This intentionally improves on the Java behavior, which accepts any
    no-tool response.  Task-specific verification (tests, diffs, reviewer
    verdicts) belongs to later phases and can be supplied through the same
    ``CompletionPolicy`` interface.
    """

    def evaluate(
        self,
        response: ChatResponse,
        history: list[dict[str, Any]],
    ) -> CompletionDecision:
        del history  # The minimum policy only needs the current response.
        if response.content.strip():
            return CompletionDecision(True)
        return CompletionDecision(
            False,
            "Your last response was empty. Continue the task and return a "
            "non-empty final answer when it is complete.",
        )
