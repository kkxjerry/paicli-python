from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from paicli.__main__ import (
    build_parser,
    interactive_plan_approval,
    orchestration_exit_code,
    run_selected_mode,
)
from paicli.bootstrap import build_application_runtime
from paicli.images import ImageAttachment
from paicli.interaction import CliCommandParser
from paicli.llm_client import ChatResponse
from paicli.orchestration import (
    OrchestrationMode,
    PlanReviewAction,
    OrchestrationResult,
    OrchestrationStatus,
)
from paicli.planning import ExecutionPlan, Task, TaskStatus, TaskType


class NoCallClient:
    model = "fake"
    provider = "test"
    context_window = 32_000
    supports_prompt_caching = False

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        del messages, tools
        return ChatResponse("unused")


class StubMode:
    def __init__(self, mode: OrchestrationMode) -> None:
        self.mode = mode
        self.calls: list[str] = []
        self.approval_seen = False

    def run(self, goal: str, **kwargs: object) -> OrchestrationResult:
        self.calls.append(goal)
        approval = kwargs.get("approval")
        plan = ExecutionPlan(goal, [Task("task_1", "Do it")])
        if callable(approval):
            self.approval_seen = True
            if not approval(plan):
                return OrchestrationResult(
                    self.mode,
                    OrchestrationStatus.CANCELLED,
                    plan,
                    "cancelled",
                    {},
                )
        plan.task("task_1").mark_completed("done")
        return OrchestrationResult(
            self.mode,
            OrchestrationStatus.SUCCEEDED,
            plan,
            f"{self.mode.value} done",
            {},
        )


class StubApplication:
    def __init__(self) -> None:
        self.plan = StubMode(OrchestrationMode.PLAN)
        self.team = StubMode(OrchestrationMode.TEAM)


class OrchestrationCliTest(unittest.TestCase):
    def test_parser_exposes_plan_team_and_bounded_worker_controls(self) -> None:
        defaults = build_parser().parse_args([])
        configured = build_parser().parse_args(
            [
                "--mode",
                "team",
                "--subagent-max-steps",
                "9",
                "--plan-workers",
                "3",
                "--plan-revisions",
                "1",
                "--team-workers",
                "2",
                "--review-retries",
                "1",
            ]
        )

        self.assertEqual("react", defaults.mode)
        self.assertEqual(4, defaults.plan_workers)
        self.assertEqual(2, defaults.plan_revisions)
        self.assertEqual(2, defaults.team_workers)
        self.assertEqual(2, defaults.review_retries)
        self.assertEqual("team", configured.mode)
        self.assertEqual(9, configured.subagent_max_steps)
        self.assertEqual(3, configured.plan_workers)
        self.assertEqual(1, configured.plan_revisions)
        self.assertEqual(1, configured.review_retries)

    def test_slash_parser_preserves_plan_and_team_goal_words(self) -> None:
        plan = CliCommandParser.parse("/plan inspect then edit")
        team = CliCommandParser.parse("/team implement and review")

        self.assertEqual("plan", plan.name)  # type: ignore[union-attr]
        self.assertEqual(("inspect", "then", "edit"), plan.arguments)  # type: ignore[union-attr]
        self.assertEqual("team", team.name)  # type: ignore[union-attr]

    def test_interactive_plan_review_supports_enter_cancel_and_supplement(self) -> None:
        plan = ExecutionPlan("goal", [Task("task_1", "Do it")])

        with patch("builtins.input", return_value=""):
            execute = interactive_plan_approval(plan)
        with patch("builtins.input", return_value="n"):
            cancel = interactive_plan_approval(plan)
        with patch("builtins.input", side_effect=["e", "Focus on tests"]):
            supplement = interactive_plan_approval(plan)

        self.assertIs(PlanReviewAction.EXECUTE, execute.action)
        self.assertIs(PlanReviewAction.CANCEL, cancel.action)
        self.assertIs(PlanReviewAction.SUPPLEMENT, supplement.action)
        self.assertEqual("Focus on tests", supplement.feedback)

    def test_run_selected_mode_dispatches_plan_with_approval_and_team(self) -> None:
        runtime = StubApplication()
        output = io.StringIO()
        with redirect_stdout(output):
            plan_code = run_selected_mode(  # type: ignore[arg-type]
                runtime,
                "plan",
                "plan goal",
                (),
                plan_approval=lambda _plan: True,
            )
            team_code = run_selected_mode(  # type: ignore[arg-type]
                runtime,
                "team",
                "team goal",
                (),
            )

        self.assertEqual(0, plan_code)
        self.assertEqual(0, team_code)
        self.assertEqual(["plan goal"], runtime.plan.calls)
        self.assertEqual(["team goal"], runtime.team.calls)
        self.assertTrue(runtime.plan.approval_seen)
        self.assertIn("plan done", output.getvalue())
        self.assertIn("team done", output.getvalue())

    def test_plan_and_team_reject_images_before_planner_execution(self) -> None:
        runtime = StubApplication()
        image = ImageAttachment("a.png", "image/png", "data:image/png;base64,AA==")
        output = io.StringIO()

        with redirect_stdout(output):
            code = run_selected_mode(  # type: ignore[arg-type]
                runtime,
                "team",
                "inspect image",
                (image,),
            )

        self.assertEqual(2, code)
        self.assertEqual([], runtime.team.calls)
        self.assertIn("text tasks only", output.getvalue())

    def test_exit_code_distinguishes_partial_failure_and_user_cancellation(self) -> None:
        plan = ExecutionPlan("goal", [Task("a", "A"), Task("b", "B")])
        plan.task("a").mark_completed("done")
        plan.task("b").mark_failed("boom")
        partial = OrchestrationResult(
            OrchestrationMode.PLAN,
            OrchestrationStatus.PARTIAL,
            plan,
            "partial",
            {},
        )
        cancelled = OrchestrationResult(
            OrchestrationMode.PLAN,
            OrchestrationStatus.CANCELLED,
            ExecutionPlan("goal", [Task("a", "A")]),
            "cancelled",
            {},
        )

        self.assertEqual(1, orchestration_exit_code(partial))
        self.assertEqual(0, orchestration_exit_code(cancelled))
        self.assertEqual(TaskStatus.FAILED, partial.plan.task("b").status)

    def test_application_runtime_applies_hitl_to_react_and_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_application_runtime(
                NoCallClient(),
                directory,
                enable_memory=False,
                approval_mode="deny",
            )

            react_result = runtime.react.agent.tools.execute_result(
                "write_file",
                '{"path":"react.txt","content":"blocked"}',
            )
            worker = runtime.subagents.create_worker(
                Task("write", "Write", task_type=TaskType.FILE_WRITE)
            )
            worker_result = worker.agent.tools.execute_result(
                "write_file",
                '{"path":"worker.txt","content":"blocked"}',
            )

            self.assertFalse(react_result.ok)
            self.assertFalse(worker_result.ok)
            self.assertFalse(Path(directory, "react.txt").exists())
            self.assertFalse(Path(directory, "worker.txt").exists())

    def test_application_runtime_wires_all_modes_and_switches_shared_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = NoCallClient()
            runtime = build_application_runtime(
                first,
                directory,
                enable_memory=False,
                plan_workers=3,
                plan_revisions=1,
                team_workers=2,
                review_retries=1,
            )

            second = NoCallClient()
            second.model = "second"  # type: ignore[misc]
            runtime.set_client(second)

            # Trace-enabled runtimes preserve the observed wrapper while
            # replacing its underlying provider client.
            self.assertIs(second, runtime.client)
            self.assertIs(runtime.react.agent.client, runtime.subagents.client)
            self.assertIs(runtime.react.agent.client, runtime.plan.planner.client)
            self.assertIs(runtime.react.agent.client, runtime.team.planner.client)
            self.assertEqual(3, runtime.plan.concurrency.max_workers)
            self.assertEqual(1, runtime.plan.max_plan_revisions)
            self.assertEqual(2, runtime.team.concurrency.max_workers)
            self.assertEqual(1, runtime.team.max_review_retries)
            self.assertEqual(Path(directory).resolve(), runtime.tools.project_root)


if __name__ == "__main__":
    unittest.main()
