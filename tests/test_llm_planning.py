from __future__ import annotations

import unittest
from typing import Any

from paicli.llm_client import ChatResponse
from paicli.planning import (
    DagScheduler,
    ExecutionPlan,
    LlmPlanner,
    PlanExecuteAgent,
    PlanGenerationError,
    PlanValidationError,
    PlanValidator,
    Task,
    TaskStatus,
    TaskType,
)


class PlannerClient:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        self.requests.append(([dict(message) for message in messages], list(tools)))
        if not self.responses:
            raise AssertionError("planner made more model calls than expected")
        return self.responses.pop(0)


VALID_PLAN = """
{
  "summary": "inspect, edit, and verify",
  "tasks": [
    {
      "id": "inspect",
      "description": "Inspect the current implementation",
      "type": "FILE_READ",
      "dependencies": [],
      "acceptance_criteria": ["Relevant code is identified"]
    },
    {
      "id": "edit",
      "description": "Implement the requested change",
      "type": "FILE_WRITE",
      "dependencies": ["inspect"],
      "acceptance_criteria": ["The code contains the requested behavior"]
    },
    {
      "id": "test",
      "description": "Run focused tests",
      "type": "VERIFICATION",
      "dependencies": ["edit"],
      "acceptance_criteria": ["Focused tests pass"]
    }
  ]
}
"""


class LlmPlanningTest(unittest.TestCase):
    def test_scheduler_returns_stable_topological_order_and_batches(self) -> None:
        plan = ExecutionPlan(
            "build and verify",
            [
                Task("inspect_a", "Inspect A", task_type=TaskType.FILE_READ),
                Task("inspect_b", "Inspect B", task_type=TaskType.FILE_READ),
                Task(
                    "edit",
                    "Edit",
                    ("inspect_a", "inspect_b"),
                    task_type=TaskType.FILE_WRITE,
                ),
                Task(
                    "test",
                    "Test",
                    ("edit",),
                    task_type=TaskType.VERIFICATION,
                ),
            ],
        )

        order = [task.id for task in plan.topological_order()]
        batches = [[task.id for task in batch] for batch in plan.execution_batches()]

        self.assertEqual(["inspect_a", "inspect_b", "edit", "test"], order)
        self.assertEqual(
            [["inspect_a", "inspect_b"], ["edit"], ["test"]],
            batches,
        )

    def test_validator_rejects_unknown_self_duplicate_and_cycle_dependencies(self) -> None:
        with self.assertRaisesRegex(PlanValidationError, "unknown dependencies"):
            ExecutionPlan("goal", [Task("a", "A", ("missing",))])
        with self.assertRaisesRegex(PlanValidationError, "depend on itself"):
            ExecutionPlan("goal", [Task("a", "A", ("a",))])
        with self.assertRaisesRegex(PlanValidationError, "ids must be unique"):
            ExecutionPlan("goal", [Task("a", "A"), Task("a", "B")])
        with self.assertRaisesRegex(PlanValidationError, "cycle"):
            ExecutionPlan(
                "goal",
                [Task("a", "A", ("b",)), Task("b", "B", ("a",))],
            )
        with self.assertRaisesRegex(PlanValidationError, "at least one"):
            PlanValidator(require_tasks=True).validate(ExecutionPlan("goal", []))

    def test_simple_goal_uses_deterministic_single_task_fast_path(self) -> None:
        client = PlannerClient([])
        planner = LlmPlanner(client)

        plan = planner.create_plan("Read README.md")

        self.assertEqual([], client.requests)
        self.assertEqual(["task_1"], [task.id for task in plan.tasks])
        self.assertEqual(TaskType.FILE_READ, plan.tasks[0].task_type)
        self.assertEqual("simple_rule", plan.metadata["source"])

    def test_complex_goal_is_generated_by_llm_and_validated(self) -> None:
        client = PlannerClient(
            [ChatResponse(VALID_PLAN, input_tokens=100, output_tokens=40)]
        )
        planner = LlmPlanner(client)

        plan = planner.create_plan("Inspect the code, then edit it and run tests")

        self.assertEqual(1, len(client.requests))
        self.assertEqual([], client.requests[0][1])
        self.assertEqual(["inspect", "edit", "test"], [task.id for task in plan.tasks])
        self.assertEqual(TaskType.FILE_WRITE, plan.task("edit").task_type)
        self.assertEqual(
            ("The code contains the requested behavior",),
            plan.task("edit").acceptance_criteria,
        )
        self.assertEqual(140, planner.last_usage.total_tokens)
        self.assertEqual("llm", plan.metadata["source"])
        self.assertEqual(0, plan.metadata["repair_attempts"])

    def test_invalid_plan_is_repaired_once_with_the_validation_error(self) -> None:
        invalid = """
        {"tasks": [
          {"id":"a","description":"A","type":"ANALYSIS","dependencies":["b"]},
          {"id":"b","description":"B","type":"ANALYSIS","dependencies":["a"]}
        ]}
        """
        client = PlannerClient(
            [
                ChatResponse(invalid, input_tokens=20, output_tokens=10),
                ChatResponse(VALID_PLAN, input_tokens=30, output_tokens=15),
            ]
        )
        planner = LlmPlanner(client, max_repair_attempts=1)

        plan = planner.create_plan("Inspect, edit, and test")

        self.assertEqual(2, len(client.requests))
        repair_request = client.requests[1][0][-1]["content"]
        self.assertIn("cycle", repair_request)
        self.assertEqual(1, plan.metadata["repair_attempts"])
        self.assertEqual(75, planner.last_usage.total_tokens)

    def test_planner_fails_after_the_bounded_repair_budget(self) -> None:
        invalid = '{"tasks":[{"id":"a","description":"A","dependencies":["x"]}]}'
        client = PlannerClient([ChatResponse(invalid), ChatResponse(invalid)])
        planner = LlmPlanner(client, max_repair_attempts=1)

        with self.assertRaisesRegex(PlanGenerationError, "2 attempt"):
            planner.create_plan("Do A and then B")

        self.assertEqual(2, len(client.requests))
        self.assertIn("unknown dependencies", planner.last_error)

    def test_parser_accepts_markdown_fences_but_rejects_unknown_task_type(self) -> None:
        plan = LlmPlanner.parse_plan("goal", "```json\n" + VALID_PLAN + "\n```")
        self.assertEqual(3, len(plan.tasks))

        bad_type = """
        {"tasks":[{
          "id":"a","description":"A","type":"MAGIC","dependencies":[]
        }]}
        """
        with self.assertRaisesRegex(PlanValidationError, "must be one of"):
            LlmPlanner.parse_plan("goal", bad_type)

    def test_failed_branch_does_not_prevent_independent_ready_work(self) -> None:
        class NoReplan:
            def create_plan(self, goal: str) -> ExecutionPlan:
                return ExecutionPlan(
                    goal,
                    [
                        Task("fails", "Fails"),
                        Task("independent", "Independent"),
                        Task("blocked", "Blocked", ("fails",)),
                    ],
                )

            def replan(self, plan: ExecutionPlan, failed_task: Task) -> ExecutionPlan:
                return plan

        seen: list[str] = []

        def execute(task: Task, _results: dict[str, str]) -> str:
            seen.append(task.id)
            if task.id == "fails":
                raise RuntimeError("boom")
            return "done"

        plan = PlanExecuteAgent(NoReplan(), execute).run("do independent work")

        self.assertEqual(["fails", "independent"], seen)
        self.assertEqual(TaskStatus.FAILED, plan.task("fails").status)
        self.assertEqual(TaskStatus.COMPLETED, plan.task("independent").status)
        self.assertEqual(TaskStatus.SKIPPED, plan.task("blocked").status)
        self.assertTrue(plan.is_finished())

    def test_replacement_plan_reuses_matching_completed_tasks(self) -> None:
        class ReplacingPlanner:
            def create_plan(self, goal: str) -> ExecutionPlan:
                return ExecutionPlan(
                    goal,
                    [
                        Task("inspect", "Inspect"),
                        Task("edit", "Edit", ("inspect",)),
                    ],
                )

            def replan(self, plan: ExecutionPlan, failed_task: Task) -> ExecutionPlan:
                return ExecutionPlan(
                    plan.goal,
                    [
                        Task("inspect", "Inspect"),
                        Task("alternate", "Alternate edit", ("inspect",)),
                    ],
                )

        calls: list[str] = []

        def execute(task: Task, results: dict[str, str]) -> str:
            calls.append(task.id)
            if task.id == "edit":
                raise RuntimeError("edit failed")
            if task.id == "alternate":
                self.assertEqual("inspection", results["inspect"])
                return "alternate complete"
            return "inspection"

        plan = PlanExecuteAgent(ReplacingPlanner(), execute).run("change code")

        self.assertEqual(["inspect", "edit", "alternate"], calls)
        self.assertEqual(TaskStatus.COMPLETED, plan.task("inspect").status)
        self.assertEqual("inspection", plan.task("inspect").result)
        self.assertEqual(TaskStatus.COMPLETED, plan.task("alternate").status)

    def test_completed_task_is_not_reused_when_dependencies_changed(self) -> None:
        previous = ExecutionPlan(
            "goal",
            [Task("inspect", "Inspect", task_type=TaskType.FILE_READ)],
        )
        previous.task("inspect").mark_running()
        previous.task("inspect").mark_completed("old inspection")
        replacement = ExecutionPlan(
            "goal",
            [
                Task("setup", "Prepare", task_type=TaskType.COMMAND),
                Task(
                    "inspect",
                    "Inspect",
                    ("setup",),
                    task_type=TaskType.FILE_READ,
                ),
            ],
        )

        replacement.inherit_completed_from(previous)

        self.assertEqual(TaskStatus.PENDING, replacement.task("inspect").status)
        self.assertEqual("", replacement.task("inspect").result)

    def test_replan_prompt_contains_failure_and_completed_evidence(self) -> None:
        replacement = """
        {"summary":"replacement","tasks":[
          {"id":"inspect","description":"Inspect","type":"FILE_READ","dependencies":[]},
          {"id":"fix","description":"Fix another way","type":"FILE_WRITE","dependencies":["inspect"]}
        ]}
        """
        client = PlannerClient([ChatResponse(replacement)])
        planner = LlmPlanner(client, simple_goal_detector=lambda _goal: False)
        previous = ExecutionPlan(
            "change code",
            [Task("inspect", "Inspect"), Task("edit", "Edit", ("inspect",))],
        )
        previous.task("inspect").mark_running()
        previous.task("inspect").mark_completed("found service.py")
        previous.task("edit").mark_running()
        previous.task("edit").mark_failed("test failed")

        plan = planner.replan(previous, previous.task("edit"))

        prompt = client.requests[0][0][-1]["content"]
        self.assertIn("test failed", prompt)
        self.assertIn("found service.py", prompt)
        self.assertEqual(["inspect", "fix"], [task.id for task in plan.tasks])


if __name__ == "__main__":
    unittest.main()
