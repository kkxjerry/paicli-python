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
        # DAG：inspect -> edit -> test。seen 用来记录真实执行顺序。
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
            # append 返回 None（假值），所以 or 右边的字符串会成为任务结果。
            lambda task, _results: seen.append(task.id) or f"{task.id} done",
        ).run("Implement feature")

        self.assertEqual(["inspect", "edit", "test"], seen)
        self.assertTrue(plan.is_finished())
        self.assertTrue(
            all(task.status is TaskStatus.COMPLETED for task in plan.tasks)
        )

    def test_rejects_cycles(self) -> None:
        # a 等 b，b 又等 a，没有任务能先执行，因此计划必须拒绝这个环。
        with self.assertRaisesRegex(ValueError, "cycle"):
            ExecutionPlan(
                "bad",
                [
                    Task("a", "A", ("b",)),
                    Task("b", "B", ("a",)),
                ],
            )

    def test_failure_skips_dependent_tasks(self) -> None:
        # 失败会沿依赖链传播：a FAILED -> b SKIPPED -> c SKIPPED。
        planner = StaticPlanner(
            [
                Task("a", "Fails"),
                Task("b", "Blocked", ("a",)),
                Task("c", "Also blocked", ("b",)),
            ]
        )

        def fail(_task: Task, _results: dict[str, str]) -> str:
            # 这个函数被作为 executor；第一个可执行任务 a 会在这里主动失败。
            raise RuntimeError("boom")

        plan = PlanExecuteAgent(planner, fail).run("fail")

        self.assertEqual(TaskStatus.FAILED, plan.tasks[0].status)
        self.assertEqual(TaskStatus.SKIPPED, plan.tasks[1].status)
        self.assertEqual(TaskStatus.SKIPPED, plan.tasks[2].status)


if __name__ == "__main__":
    unittest.main()
