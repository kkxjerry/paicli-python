"""Loop budgets shared by ReAct and future Plan/Team agents.

The defaults intentionally match the current Java implementation:

- no hard token limit unless one is configured;
- three identical no-progress rounds trigger stagnation;
- fifty model iterations are the final safety valve.

Python improves the Java signature comparison in two ways: JSON arguments are
canonicalized before comparison, and tool-result hashes are included so a
repeated read whose observation actually changed is not treated as stagnant.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from enum import Enum
from typing import Sequence

from ..context import TokenUsage
from ..llm_client import ChatResponse, ToolCall


class BudgetExitReason(str, Enum):
    WITHIN_BUDGET = "within_budget"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    STAGNATION_DETECTED = "stagnation_detected"
    HARD_ITERATION_LIMIT = "hard_iteration_limit"


class AgentBudget:
    """Track model iterations, provider usage, and repeated no-progress rounds."""

    DEFAULT_STAGNATION_WINDOW = 3
    DEFAULT_HARD_MAX_ITERATIONS = 50

    def __init__(
        self,
        *,
        token_budget: int | None = None,
        stagnation_window: int = DEFAULT_STAGNATION_WINDOW,
        hard_max_iterations: int = DEFAULT_HARD_MAX_ITERATIONS,
    ) -> None:
        if token_budget is not None and token_budget < 1:
            raise ValueError("token_budget must be positive when configured")
        if stagnation_window < 2:
            raise ValueError("stagnation_window must be at least 2")
        if hard_max_iterations < 1:
            raise ValueError("hard_max_iterations must be positive")

        self.token_budget = token_budget
        self.stagnation_window = stagnation_window
        self.hard_max_iterations = hard_max_iterations
        self.iteration = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_input_tokens = 0
        self._recent_no_progress_signatures: deque[str] = deque(maxlen=stagnation_window)
        self._stagnant = False

    @property
    def usage(self) -> TokenUsage:
        return TokenUsage(
            self._input_tokens,
            self._output_tokens,
            self._cached_input_tokens,
        )

    def begin_iteration(self) -> int:
        self.iteration += 1
        return self.iteration

    def record_response(self, response: ChatResponse) -> None:
        self.record_tokens(
            response.input_tokens,
            response.output_tokens,
            response.cached_input_tokens,
        )

    def record_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> None:
        self._input_tokens += max(0, int(input_tokens))
        self._output_tokens += max(0, int(output_tokens))
        self._cached_input_tokens += max(0, int(cached_input_tokens))

    def record_tool_round(
        self,
        tool_calls: Sequence[ToolCall],
        results: Sequence[str] = (),
    ) -> None:
        """Record one observation round for no-progress detection.

        Tool call IDs are deliberately ignored. Calls in the same assistant
        message are currently executed as a batch, so their normalized
        signatures are sorted before hashing. Results remain positionally
        paired with calls before that sort.
        """

        if not tool_calls:
            self._recent_no_progress_signatures.clear()
            self._stagnant = False
            return

        result_values = list(results)
        entries: list[tuple[str, str, str]] = []
        for index, call in enumerate(tool_calls):
            result = result_values[index] if index < len(result_values) else ""
            entries.append(
                (
                    call.name,
                    _canonical_json(call.arguments),
                    hashlib.sha256(result.encode("utf-8", errors="replace")).hexdigest(),
                )
            )
        entries.sort()
        signature = "tools:" + json.dumps(
            entries, ensure_ascii=False, separators=(",", ":")
        )
        self._record_no_progress_signature(signature)

    def record_completion_rejection(self, content: str, feedback: str) -> None:
        """Include repeated rejected final answers in stagnation detection."""

        payload = json.dumps(
            {"content": content, "feedback": feedback},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        signature = "completion:" + hashlib.sha256(
            payload.encode("utf-8", errors="replace")
        ).hexdigest()
        self._record_no_progress_signature(signature)

    def _record_no_progress_signature(self, signature: str) -> None:
        self._recent_no_progress_signatures.append(signature)
        self._stagnant = (
            len(self._recent_no_progress_signatures) == self.stagnation_window
            and len(set(self._recent_no_progress_signatures)) == 1
        )

    def check(self) -> BudgetExitReason:
        # Keep the same precedence as the Java implementation: a specific
        # stagnation diagnosis is more actionable than the generic budget hit.
        if self._stagnant:
            return BudgetExitReason.STAGNATION_DETECTED
        if (
            self.token_budget is not None
            and self.usage.total_tokens >= self.token_budget
        ):
            return BudgetExitReason.TOKEN_BUDGET_EXCEEDED
        if self.iteration >= self.hard_max_iterations:
            return BudgetExitReason.HARD_ITERATION_LIMIT
        return BudgetExitReason.WITHIN_BUDGET

    def describe_exit(self, reason: BudgetExitReason) -> str:
        if reason is BudgetExitReason.STAGNATION_DETECTED:
            return (
                f"detected {self.stagnation_window} identical no-progress "
                "rounds; the agent made no observable progress"
            )
        if reason is BudgetExitReason.TOKEN_BUDGET_EXCEEDED:
            return (
                f"token budget exhausted: {self.usage.total_tokens}/"
                f"{self.token_budget}"
            )
        if reason is BudgetExitReason.HARD_ITERATION_LIMIT:
            return (
                "model did not return an accepted final answer within "
                f"{self.hard_max_iterations} iterations"
            )
        return "within budget"


def _canonical_json(raw: str) -> str:
    """Normalize equivalent JSON argument strings for stable comparison."""

    value = raw or "{}"
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return str(value).strip()
    return json.dumps(
        parsed,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
