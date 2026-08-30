from __future__ import annotations

import json
import unittest
from pathlib import Path

from paicli.__main__ import build_parser
from paicli.agents.budget import AgentBudget
from paicli.agents.models import NonEmptyCompletionPolicy
from paicli.llm_client import ChatResponse


class JavaPhase02ParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture = Path(__file__).parent / "fixtures" / "java_phase_0_2.json"
        cls.contract = json.loads(fixture.read_text(encoding="utf-8"))

    def test_budget_and_cli_defaults_match_recorded_java_contract(self) -> None:
        expected = self.contract["react"]
        budget = AgentBudget()
        cli = build_parser().parse_args([])

        self.assertEqual(expected["hard_max_iterations"], budget.hard_max_iterations)
        self.assertEqual(expected["stagnation_window"], budget.stagnation_window)
        self.assertEqual(expected["token_budget"], budget.token_budget)
        self.assertEqual(expected["hard_max_iterations"], cli.max_steps)
        self.assertEqual(expected["stagnation_window"], cli.stagnation_window)
        self.assertEqual(expected["token_budget"], cli.token_budget)

    def test_recorded_empty_answer_divergence_is_executable(self) -> None:
        expected = self.contract["intentional_python_improvements"]
        decision = NonEmptyCompletionPolicy().evaluate(ChatResponse(""), [])

        self.assertEqual("reject", expected["empty_final_answer"])
        self.assertFalse(decision.completed)

    def test_usage_extension_remains_backwards_compatible(self) -> None:
        response = ChatResponse("answer")

        self.assertEqual(0, response.input_tokens)
        self.assertEqual(0, response.output_tokens)
        self.assertEqual(0, response.cached_input_tokens)


if __name__ == "__main__":
    unittest.main()
