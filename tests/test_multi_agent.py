from __future__ import annotations

import unittest

from paicli.multi_agent import AgentOrchestrator, AgentRole, AgentTeam
from paicli.planning import ExecutionPlan, Task, TaskStatus


class MultiAgentTest(unittest.TestCase):
    """验证角色分配、结果传递和缺失 Worker 时的失败处理。"""

    def test_roles_exchange_results_between_tasks(self) -> None:
        """验证 Researcher 的结果会传给后续 Coder。"""

        # Arrange：Researcher 返回调研结果，Coder 把收到的消息记录到 received。
        team = AgentTeam()
        team.register(
            AgentRole.RESEARCHER,
            lambda task, _messages: f"research for {task.description}",
        )
        received: list[str] = []

        def coder(_task: Task, messages: list[object]) -> str:
            received.extend(message.content for message in messages)  # type: ignore[attr-defined]
            return "implementation"

        team.register(AgentRole.CODER, coder)
        # DAG：research -> code，因此 Coder 执行时 Researcher 已完成。
        plan = ExecutionPlan(
            "feature",
            [
                Task("research", "authentication"),
                Task("code", "implement authentication", ("research",)),
            ],
        )

        # Act：按任务 ID 分配角色并执行计划。
        result = AgentOrchestrator(
            team,
            {
                "research": AgentRole.RESEARCHER,
                "code": AgentRole.CODER,
            },
        ).run(plan)

        # Assert：Coder 收到了上一个角色的结果，两个任务都完成。
        self.assertIn("research for authentication", received)
        self.assertTrue(
            all(task.status is TaskStatus.COMPLETED for task in result.tasks)
        )

    def test_missing_role_fails_task(self) -> None:
        """验证任务被分配给未注册角色时，会得到可诊断的 FAILED 结果。"""

        # Arrange：计划要求 REVIEWER，但 AgentTeam 是空的。
        plan = ExecutionPlan("goal", [Task("review", "Review")])

        # Act：Orchestrator 查找 REVIEWER Worker 时得到 None。
        result = AgentOrchestrator(
            AgentTeam(),
            {"review": AgentRole.REVIEWER},
        ).run(plan)

        # Assert：任务失败，且结果说明缺少 Worker。
        self.assertEqual(TaskStatus.FAILED, result.tasks[0].status)
        self.assertIn("no worker", result.tasks[0].result)


if __name__ == "__main__":
    unittest.main()
