from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paicli.__main__ import build_parser
from paicli.context import ContextSettings
from paicli.memory import ConversationHistoryCompactor
from paicli.planning import ExecutionPlan, LlmPlanner, PlanValidationError, PlanValidator
from paicli.tools import ToolErrorType, ToolRegistry


class NoCallClient:
    def chat(self, messages, tools):  # pragma: no cover - not invoked here
        raise AssertionError("unexpected model call")


class JavaPhase35ParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture = Path(__file__).parent / "fixtures" / "java_phase_3_5.json"
        cls.contract = json.loads(fixture.read_text(encoding="utf-8"))

    def test_runtime_schema_gate_matches_recorded_contract(self) -> None:
        self.assertTrue(self.contract["tool_gateway"]["runtime_schema_validation"])
        with tempfile.TemporaryDirectory() as directory:
            result = ToolRegistry(directory).execute_result(
                "read_file",
                '{"path":"README.md","extra":true}',
            )
        self.assertEqual(ToolErrorType.INVALID_ARGUMENTS, result.error_type)

    def test_default_context_contract_is_executable(self) -> None:
        expected = self.contract["context"]
        cli = build_parser().parse_args([])
        settings = ContextSettings.for_model(16_000)
        compactor = ConversationHistoryCompactor()

        self.assertTrue(expected["memory_enabled_by_default"])
        self.assertFalse(cli.no_memory)
        self.assertEqual(
            expected["retain_recent_user_rounds"],
            compactor.retain_recent_rounds,
        )
        self.assertEqual(
            int(settings.token_budget * expected["compression_trigger_fraction_of_input_budget"]),
            settings.compression_trigger_tokens,
        )

    def test_planner_defaults_and_non_empty_contract(self) -> None:
        expected = self.contract["planning"]
        planner = LlmPlanner(NoCallClient())

        self.assertEqual(expected["max_repair_attempts"], planner.max_repair_attempts)
        self.assertTrue(expected["shared_dag_contract"])
        with self.assertRaises(PlanValidationError):
            PlanValidator(require_tasks=True).validate(ExecutionPlan("goal", []))


if __name__ == "__main__":
    unittest.main()
