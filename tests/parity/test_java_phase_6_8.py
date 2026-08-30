from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paicli.__main__ import build_parser
from paicli.planning import Task, TaskConcurrencyPolicy, TaskType
from paicli.review import ReviewVerdict, ReviewerAgent
from paicli.tools import ScopedToolRuntime, ToolRegistry


class JavaPhase68ParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture = Path(__file__).parent / "fixtures" / "java_phase_6_8.json"
        cls.contract = json.loads(fixture.read_text(encoding="utf-8"))

    def test_cli_and_orchestration_defaults_match_recorded_contract(self) -> None:
        args = build_parser().parse_args([])

        self.assertTrue(self.contract["plan"]["cli_mode"])
        self.assertEqual(
            self.contract["plan"]["max_ready_workers"],
            args.plan_workers,
        )
        self.assertEqual(
            self.contract["plan"]["max_revisions"],
            args.plan_revisions,
        )
        self.assertEqual(
            self.contract["team"]["max_ready_workers"],
            args.team_workers,
        )
        self.assertEqual(
            self.contract["team"]["review_retries"],
            args.review_retries,
        )

    def test_python_parallelism_is_bounded_to_read_scoped_task_types(self) -> None:
        policy = TaskConcurrencyPolicy(max_workers=4)
        tasks = [
            Task("read", "Read", task_type=TaskType.FILE_READ),
            Task("analyze", "Analyze", task_type=TaskType.ANALYSIS),
            Task("write", "Write", task_type=TaskType.FILE_WRITE),
            Task("verify", "Verify", task_type=TaskType.VERIFICATION),
        ]

        waves = policy.execution_waves(tasks)

        self.assertEqual(
            sorted(
                self.contract["intentional_python_improvements"][
                    "parallel_task_types"
                ]
            ),
            sorted(item.value for item in policy.parallel_task_types),
        )
        self.assertEqual(
            [["read", "analyze"], ["write"], ["verify"]],
            [[task.id for task in wave] for wave in waves],
        )

    def test_reviewer_contract_and_hidden_tool_boundary_are_executable(self) -> None:
        review = ReviewerAgent.parse(
            json.dumps(
                {
                    "verdict": "changes_requested",
                    "summary": "fix locally",
                    "issues": ["missing evidence"],
                    "suggestions": ["inspect the artifact"],
                    "evidence": [],
                }
            )
        )
        self.assertIs(ReviewVerdict.CHANGES_REQUESTED, review.verdict)
        self.assertEqual(
            "fail_task",
            self.contract["intentional_python_improvements"][
                "review_retry_exhausted"
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            scoped = ScopedToolRuntime.read_only(registry)
            result = scoped.execute_result(
                "write_file",
                '{"path":"blocked.txt","content":"no"}',
            )

            self.assertFalse(result.ok)
            self.assertEqual(
                "policy_denied",
                self.contract["intentional_python_improvements"][
                    "hidden_tool_call"
                ],
            )
            self.assertFalse(Path(directory, "blocked.txt").exists())


if __name__ == "__main__":
    unittest.main()
