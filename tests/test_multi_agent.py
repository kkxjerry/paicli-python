from __future__ import annotations

import unittest

from paicli.multi_agent import AgentOrchestrator, AgentRole, AgentTeam
from paicli.planning import ExecutionPlan, Task, TaskStatus


class MultiAgentTest(unittest.TestCase):
    def test_roles_exchange_results_between_tasks(self) -> None:
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
        plan = ExecutionPlan(
            "feature",
            [
                Task("research", "authentication"),
                Task("code", "implement authentication", ("research",)),
            ],
        )

        result = AgentOrchestrator(
            team,
            {
                "research": AgentRole.RESEARCHER,
                "code": AgentRole.CODER,
            },
        ).run(plan)

        self.assertIn("research for authentication", received)
        self.assertTrue(
            all(task.status is TaskStatus.COMPLETED for task in result.tasks)
        )

    def test_missing_role_fails_task(self) -> None:
        plan = ExecutionPlan("goal", [Task("review", "Review")])

        result = AgentOrchestrator(
            AgentTeam(),
            {"review": AgentRole.REVIEWER},
        ).run(plan)

        self.assertEqual(TaskStatus.FAILED, result.tasks[0].status)
        self.assertIn("no worker", result.tasks[0].result)


if __name__ == "__main__":
    unittest.main()
