from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paicli.evaluation import (
    CaseExecution,
    EvalAssertion,
    EvalSuite,
    EvalTask,
    EvaluationReport,
    EvaluationRunner,
    compare_reports,
    load_suite,
)


class FakeExecutor:
    provider = "fake"
    model = "fake-model"

    def __init__(self, *, write_expected: bool = True) -> None:
        self.write_expected = write_expected

    def execute(self, task: EvalTask, workspace: Path) -> CaseExecution:
        if self.write_expected:
            Path(workspace, "answer.txt").write_text("candidate", encoding="utf-8")
        return CaseExecution(
            "run-1",
            "succeeded",
            "marker candidate",
            "",
            ("answer.txt",),
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "model_calls": 2,
                "tool_calls": 1,
                "model_errors": 0,
                "tool_errors": 0,
                "estimated_cost_cny": 0.01,
                "unpriced_model_calls": 0,
            },
        )


class EvaluationTest(unittest.TestCase):
    def test_fixed_suite_runs_assertions_and_aggregates_metrics(self) -> None:
        suite = EvalSuite(
            "fixed",
            "1",
            (
                EvalTask(
                    "case",
                    "do work",
                    files={"seed.txt": "seed"},
                    assertions=(
                        EvalAssertion("answer_contains", value="marker"),
                        EvalAssertion(
                            "file_contains",
                            path="answer.txt",
                            value="candidate",
                        ),
                    ),
                ),
            ),
        )

        report = EvaluationRunner(FakeExecutor()).run(suite)

        self.assertEqual(1.0, report.metrics["success_rate"])
        self.assertEqual(1.0, report.metrics["assertion_pass_rate"])
        self.assertEqual(12, report.metrics["input_tokens"] + report.metrics["output_tokens"])
        self.assertTrue(report.cases[0].success)

    def test_report_round_trip_and_comparison_detect_regression(self) -> None:
        suite = EvalSuite(
            "fixed",
            "1",
            (
                EvalTask(
                    "case",
                    "do work",
                    assertions=(
                        EvalAssertion(
                            "file_contains",
                            path="answer.txt",
                            value="candidate",
                        ),
                    ),
                ),
            ),
        )
        baseline = EvaluationRunner(FakeExecutor()).run(suite)
        candidate = EvaluationRunner(FakeExecutor(write_expected=False)).run(suite)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "report.json")
            baseline.save(path)
            loaded = EvaluationReport.load(path)

        comparison = compare_reports(loaded, candidate)
        self.assertEqual("regressed", comparison["verdict"])
        self.assertIn("case", comparison["case_success_changes"])

    def test_loads_repository_suite_and_rejects_path_escape(self) -> None:
        suite = load_suite("eval/suites/coding-smoke.json")
        self.assertEqual(3, len(suite.tasks))
        malicious = EvalSuite(
            "bad",
            "1",
            (EvalTask("bad", "x", files={"../outside": "bad"}),),
        )
        with self.assertRaisesRegex(ValueError, "escapes workspace"):
            EvaluationRunner(FakeExecutor()).run(malicious)

    def test_command_assertion_uses_argv_without_shell(self) -> None:
        suite = EvalSuite(
            "command",
            "1",
            (
                EvalTask(
                    "command",
                    "noop",
                    assertions=(
                        EvalAssertion(
                            "command",
                            command=("python3", "-c", "print('ok')"),
                            exit_code=0,
                        ),
                    ),
                ),
            ),
        )
        report = EvaluationRunner(FakeExecutor()).run(suite)
        self.assertTrue(report.cases[0].assertions[0].passed)


if __name__ == "__main__":
    unittest.main()
