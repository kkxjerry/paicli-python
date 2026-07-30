from __future__ import annotations

import unittest

from paicli.planning import (
    ExecutionPlan,
    PlanExecuteAgent,
    StaticPlanner,
    Task,
    TaskStatus,
)


class PlanningTest(unittest.TestCase):
    def test_executes_dependency_graph_in_order(self) -> None:
        seen: list[str] = []
        planner = StaticPlanner(
            [
                Task("inspect", "Inspect repository"),
                Task("edit", "Edit code", ("inspect",)),
                Task("test", "Run tests", ("edit",)),
            ]
        )

        plan = PlanExecuteAgent(
            planner,
            lambda task, _results: seen.append(task.id) or f"{task.id} done",
        ).run("Implement feature")

        self.assertEqual(["inspect", "edit", "test"], seen)
        self.assertTrue(plan.is_finished())
        self.assertTrue(
            all(task.status is TaskStatus.COMPLETED for task in plan.tasks)
        )

    def test_rejects_cycles(self) -> None:
        with self.assertRaisesRegex(ValueError, "cycle"):
            ExecutionPlan(
                "bad",
                [
                    Task("a", "A", ("b",)),
                    Task("b", "B", ("a",)),
                ],
            )

    def test_failure_skips_dependent_tasks(self) -> None:
        planner = StaticPlanner(
            [
                Task("a", "Fails"),
                Task("b", "Blocked", ("a",)),
                Task("c", "Also blocked", ("b",)),
            ]
        )

        def fail(_task: Task, _results: dict[str, str]) -> str:
            raise RuntimeError("boom")

        plan = PlanExecuteAgent(planner, fail).run("fail")

        self.assertEqual(TaskStatus.FAILED, plan.tasks[0].status)
        self.assertEqual(TaskStatus.SKIPPED, plan.tasks[1].status)
        self.assertEqual(TaskStatus.SKIPPED, plan.tasks[2].status)


if __name__ == "__main__":
    unittest.main()
