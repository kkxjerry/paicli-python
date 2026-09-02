"""Public ReAct Agent facade backed by the shared Agent loop engine.

The observable protocol remains model -> tool execution -> tool-result
feedback -> model.  Phase 1 centralizes that protocol in ``AgentLoopEngine`` so
future Plan workers and role-based sub-agents can reuse exactly the same loop.
"""

from __future__ import annotations

from typing import Any, Callable

from .agents.budget import AgentBudget
from .agents.loop import AgentLoopEngine, ToolRuntime
from .agents.models import (
    AgentOutcome,
    CompletionPolicy,
    DiagnosticsCompletionPolicy,
    NonEmptyCompletionPolicy,
    RunStatus,
)
from .context import ContextController, ContextSettings
from .images import ImageAttachment, multimodal_user_message
from .llm_client import LlmClient
from .lsp import LspManager
from .memory import MemoryManager
from .runtime import CancelledError, CancellationToken

EventHandler = Callable[[str, str], None]

SYSTEM_PROMPT = """You are a coding agent working inside one project directory.
Use tools when you need to inspect or modify the project.
After a tool result arrives, continue reasoning from that result.
When the task is complete, answer the user directly without calling a tool."""


class AgentLoopError(RuntimeError):
    """The loop stopped through a safety valve instead of normal completion."""

    def __init__(self, message: str, outcome: AgentOutcome | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome


class Agent:
    """Compatibility facade for the default ReAct execution mode.

    ``run()`` keeps the original string-returning API.  New orchestration code
    should prefer ``run_outcome()`` so termination reason, token usage, changed
    files, and run ID remain available as structured data.
    """

    def __init__(
        self,
        client: LlmClient,
        tools: ToolRuntime,
        *,
        max_steps: int = AgentBudget.DEFAULT_HARD_MAX_ITERATIONS,
        token_budget: int | None = None,
        stagnation_window: int = AgentBudget.DEFAULT_STAGNATION_WINDOW,
        on_event: EventHandler | None = None,
        memory: MemoryManager | None = None,
        cancellation: CancellationToken | None = None,
        context: ContextController | None = None,
        lsp: LspManager | None = None,
        completion_policy: CompletionPolicy | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        # AgentBudget performs canonical validation here so bad CLI/library
        # configuration fails before the first prompt. The real budget is still
        # recreated for every run, preventing state leakage between turns.
        self.client = client
        self.tools = tools
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.stagnation_window = stagnation_window
        self.on_event = on_event or (lambda _kind, _text: None)
        self.memory = memory
        self.cancellation = cancellation or CancellationToken()
        self.context = context
        self.lsp = lsp
        base_completion = completion_policy or NonEmptyCompletionPolicy()
        self.completion_policy = (
            base_completion
            if isinstance(base_completion, DiagnosticsCompletionPolicy)
            else DiagnosticsCompletionPolicy(base_completion)
        )
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        self.last_outcome: AgentOutcome | None = None
        AgentBudget(
            token_budget=self.token_budget,
            stagnation_window=self.stagnation_window,
            hard_max_iterations=self.max_steps,
        )

    def run(
        self,
        user_input: str,
        *,
        images: tuple[ImageAttachment, ...] = (),
    ) -> str:
        """Run ReAct and preserve the original ``str`` result API."""

        outcome = self.run_outcome(user_input, images=images)
        if outcome.succeeded:
            return outcome.content
        if outcome.status is RunStatus.CANCELLED:
            raise CancelledError(outcome.error or "operation cancelled")
        raise AgentLoopError(outcome.error or outcome.finish_reason.value, outcome)

    def run_outcome(
        self,
        user_input: str,
        *,
        images: tuple[ImageAttachment, ...] = (),
    ) -> AgentOutcome:
        """Run ReAct and return termination, usage, and artifact metadata."""

        if not user_input.strip() and not images:
            raise ValueError("user_input and images cannot both be empty")
        self.last_outcome = None
        outcome = self._new_engine().run(
            multimodal_user_message(user_input, images)
        )
        self.last_outcome = outcome
        return outcome

    def clear_history(self) -> None:
        """Start a new conversation while retaining the system instruction."""

        self.history = [self.history[0]]
        self.last_outcome = None

    def set_client(self, client: LlmClient) -> None:
        """Switch models while keeping context policy and summarizer coherent."""

        self.client = client
        if self.memory is not None:
            self.memory.set_summary_client(client)
        if self.context is not None:
            raw_window = getattr(client, "context_window", 128_000)
            try:
                context_window = max(8_000, int(raw_window))
            except (TypeError, ValueError):
                context_window = 128_000
            settings = ContextSettings.for_model(
                context_window,
                supports_prompt_caching=bool(
                    getattr(client, "supports_prompt_caching", False)
                ),
            )
            self.context.update_settings(settings)
            if self.memory is not None:
                self.memory.set_max_tokens(settings.compression_trigger_tokens)
                self.memory.set_long_term_context_tokens(
                    settings.memory_context_tokens
                )

    def _new_engine(self) -> AgentLoopEngine:
        return AgentLoopEngine(
            self.client,
            self.tools,
            self.history,
            max_iterations=self.max_steps,
            token_budget=self.token_budget,
            stagnation_window=self.stagnation_window,
            on_event=self.on_event,
            memory=self.memory,
            cancellation=self.cancellation,
            context=self.context,
            lsp=self.lsp,
            completion_policy=self.completion_policy,
        )
