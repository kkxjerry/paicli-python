from __future__ import annotations

import unittest

from paicli.agents.budget import AgentBudget, BudgetExitReason
from paicli.llm_client import ChatResponse, ToolCall


class AgentBudgetTest(unittest.TestCase):
    def test_initially_within_budget(self) -> None:
        budget = AgentBudget(token_budget=1_000)
        self.assertIs(BudgetExitReason.WITHIN_BUDGET, budget.check())

    def test_token_budget_accumulates_provider_usage(self) -> None:
        budget = AgentBudget(token_budget=100)
        budget.record_response(
            ChatResponse("", input_tokens=60, output_tokens=30, cached_input_tokens=20)
        )
        self.assertIs(BudgetExitReason.WITHIN_BUDGET, budget.check())

        budget.record_tokens(10, 0)
        self.assertIs(BudgetExitReason.TOKEN_BUDGET_EXCEEDED, budget.check())
        self.assertEqual(100, budget.usage.total_tokens)
        self.assertEqual(20, budget.usage.cached_input_tokens)

    def test_stagnation_uses_canonical_arguments_and_observations(self) -> None:
        budget = AgentBudget(stagnation_window=3)
        calls = (
            ToolCall("one", "read_file", '{"path":"a.py","start":1}'),
            ToolCall("two", "list_dir", '{"path":"."}'),
        )
        reordered = (
            ToolCall("new-one", "list_dir", '{"path":"."}'),
            ToolCall("new-two", "read_file", '{"start":1,"path":"a.py"}'),
        )

        budget.record_tool_round(calls, ("source", "listing"))
        budget.record_tool_round(reordered, ("listing", "source"))
        self.assertIs(BudgetExitReason.WITHIN_BUDGET, budget.check())
        budget.record_tool_round(calls, ("source", "listing"))

        self.assertIs(BudgetExitReason.STAGNATION_DETECTED, budget.check())

    def test_changed_observation_is_progress(self) -> None:
        budget = AgentBudget(stagnation_window=3)
        call = (ToolCall("call", "read_file", '{"path":"a.py"}'),)

        budget.record_tool_round(call, ("version 1",))
        budget.record_tool_round(call, ("version 1",))
        budget.record_tool_round(call, ("version 2",))

        self.assertIs(BudgetExitReason.WITHIN_BUDGET, budget.check())

    def test_repeated_rejected_completion_is_stagnation(self) -> None:
        budget = AgentBudget(stagnation_window=3)
        for _ in range(3):
            budget.record_completion_rejection("", "answer cannot be empty")

        self.assertIs(BudgetExitReason.STAGNATION_DETECTED, budget.check())

    def test_hard_iteration_limit(self) -> None:
        budget = AgentBudget(hard_max_iterations=3)
        budget.begin_iteration()
        budget.begin_iteration()
        self.assertIs(BudgetExitReason.WITHIN_BUDGET, budget.check())
        budget.begin_iteration()
        self.assertIs(BudgetExitReason.HARD_ITERATION_LIMIT, budget.check())

    def test_stagnation_takes_precedence_over_token_limit(self) -> None:
        budget = AgentBudget(token_budget=1, stagnation_window=2)
        budget.record_tokens(10, 10)
        call = (ToolCall("call", "list_dir", "{}"),)
        budget.record_tool_round(call, ("same",))
        budget.record_tool_round(call, ("same",))

        self.assertIs(BudgetExitReason.STAGNATION_DETECTED, budget.check())

    def test_exit_description_contains_usage_and_limit(self) -> None:
        budget = AgentBudget(token_budget=100)
        budget.record_tokens(80, 40)

        message = budget.describe_exit(BudgetExitReason.TOKEN_BUDGET_EXCEEDED)

        self.assertIn("120/100", message)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AgentBudget(token_budget=0)
        with self.assertRaises(ValueError):
            AgentBudget(stagnation_window=1)
        with self.assertRaises(ValueError):
            AgentBudget(hard_max_iterations=0)

    def test_default_token_budget_is_unlimited(self) -> None:
        budget = AgentBudget()
        budget.record_tokens(10_000_000, 10_000_000)
        self.assertIs(BudgetExitReason.WITHIN_BUDGET, budget.check())


if __name__ == "__main__":
    unittest.main()
