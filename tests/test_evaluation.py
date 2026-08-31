from __future__ import annotations

import json
import subprocess
import sys
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
    GitRevisionCaseExecutor,
    compare_reports,
    load_suite,
    summarize_stability,
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

    def test_stability_summary_preserves_real_model_variance(self) -> None:
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
        successful = EvaluationRunner(FakeExecutor()).run(suite)
        failed = EvaluationRunner(FakeExecutor(write_expected=False)).run(suite)

        summary = summarize_stability([successful, failed])

        self.assertEqual(2, summary["metrics"]["run_count"])  # type: ignore[index]
        self.assertEqual(0.5, summary["metrics"]["run_success_rate"])  # type: ignore[index]
        case = summary["cases"]["case"]  # type: ignore[index]
        self.assertEqual(0.5, case["success_rate"])
        self.assertEqual(1, len(case["failed_runs"]))

    def test_historical_revision_executor_exports_without_a_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            package = repository / "paicli"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "__main__.py").write_text(
                "import argparse\n"
                "from pathlib import Path\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('-p', '--prompt')\n"
                "parser.add_argument('--project-root', type=Path, default=Path.cwd())\n"
                "parser.add_argument('--allow-shell', action='store_true')\n"
                "args = parser.parse_args()\n"
                "print((args.project_root / 'marker.txt').read_text())\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "PaiCLI Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "add", "paicli"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "baseline"],
                check=True,
            )
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "marker.txt").write_text("REVISION_OK", encoding="utf-8")
            executor = GitRevisionCaseExecutor(
                "dashscope",
                "HEAD",
                repository_root=repository,
                environ={
                    "DASHSCOPE_API_KEY": "test-key",
                    "DASHSCOPE_MODEL": "test-model",
                    "DASHSCOPE_BASE_URL": "https://example.invalid/v1",
                },
            )
            try:
                react = executor.execute(
                    EvalTask("react", "read marker", mode="react"),
                    workspace,
                )
                plan = executor.execute(
                    EvalTask("plan", "plan work", mode="plan"),
                    workspace,
                )
            finally:
                executor.close()

        self.assertEqual("succeeded", react.status)
        self.assertIn("REVISION_OK", react.answer)
        self.assertEqual("failed", plan.status)
        self.assertIn("does not expose", plan.error)

    def test_module_entrypoint_avoids_double_import_runtime_warning(self) -> None:
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-W", "error", "-m", "paicli.evaluation", "--help"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("RuntimeWarning", completed.stderr)

    def test_package_level_evaluation_exports_are_lazy_and_compatible(self) -> None:
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import paicli; "
                    "assert 'paicli.evaluation' not in sys.modules; "
                    "assert paicli.EvaluationRunner.__module__ == 'paicli.evaluation'; "
                    "assert 'paicli.evaluation' in sys.modules"
                ),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

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
