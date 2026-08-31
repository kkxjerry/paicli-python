"""Opt-in real DashScope integration tests.

These tests spend API quota and therefore never run in ordinary CI. They use
small temporary projects and never send the PaiCLI repository itself to the
cloud model. Enable them with ``PAICLI_RUN_DASHSCOPE_LIVE_TEST=1``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from paicli.agent import Agent
from paicli.llm_client import LlmClientFactory
from paicli.model_probe import probe_model
from paicli.orchestration import OrchestrationStatus, TeamModeRuntime
from paicli.planning import StaticPlanner, Task, TaskType
from paicli.subagents import SubAgentFactory
from paicli.tools import ToolRegistry


RUN_LIVE_TEST = os.getenv("PAICLI_RUN_DASHSCOPE_LIVE_TEST", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@unittest.skipUnless(
    RUN_LIVE_TEST,
    "set PAICLI_RUN_DASHSCOPE_LIVE_TEST=1 to spend DashScope API quota",
)
class DashScopeLiveTest(unittest.TestCase):
    def client(self):
        environment = dict(os.environ)
        environment.setdefault("DASHSCOPE_MODEL", "qwen-plus")
        environment.setdefault(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        environment.setdefault("DASHSCOPE_TIMEOUT_SECONDS", "180")
        return LlmClientFactory.create("dashscope", environ=environment)

    def test_real_chat_and_function_calling_probes(self) -> None:
        client = self.client()

        chat = probe_model(client, "chat")
        tools = probe_model(client, "tools")

        self.assertTrue(chat.ok, chat.detail)
        self.assertTrue(tools.ok, tools.detail)

    def test_real_react_reads_a_local_file_and_uses_tool_feedback(self) -> None:
        marker = "PAICLI_DASHSCOPE_REACT_OK_7391"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "probe.txt").write_text(marker, encoding="utf-8")
            agent = Agent(self.client(), ToolRegistry(root), max_steps=6)

            answer = agent.run(
                "You must call read_file on probe.txt. After receiving the tool "
                "result, answer with exactly the file content and nothing else."
            )

            self.assertIn(marker, answer)
            self.assertTrue(
                any(
                    message.get("role") == "tool"
                    and message.get("name") == "read_file"
                    and marker in str(message.get("content", ""))
                    for message in agent.history
                )
            )

    def test_real_planner_worker_and_verifier_modify_a_temporary_project(self) -> None:
        from paicli.bootstrap import build_application_runtime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calculator.py").write_text(
                "def add(left: int, right: int) -> int:\n"
                "    return left + right\n",
                encoding="utf-8",
            )
            runtime = build_application_runtime(
                self.client(),
                root,
                allow_shell=True,
                enable_memory=False,
                subagent_max_steps=10,
                plan_workers=2,
            )

            result = runtime.plan.run(
                "Inspect calculator.py, add a subtract(left: int, right: int) "
                "function that returns left - right, create test_calculator.py "
                "with unittest coverage for add and subtract, and run "
                "python3 -m unittest -q. Plan separate read, write, and "
                "verification tasks with correct dependencies.",
                approval=lambda _plan: True,
            )

            self.assertIs(OrchestrationStatus.SUCCEEDED, result.status, result.answer)
            namespace: dict[str, object] = {}
            exec((root / "calculator.py").read_text(encoding="utf-8"), namespace)
            self.assertEqual(5, namespace["subtract"](8, 3))  # type: ignore[operator]
            self.assertTrue((root / "test_calculator.py").is_file())
            self.assertTrue(
                any(
                    tool.tool_name == "execute_command"
                    and tool.ok
                    and "Exit code: 0" in tool.content
                    for record in result.records.values()
                    for outcome in record.worker_outcomes
                    for tool in outcome.tool_results
                ),
                result.answer,
            )

    def test_real_reviewer_reads_corrupted_artifact_and_retries_only_task(self) -> None:
        class CorruptFirstWriteRegistry(ToolRegistry):
            def __init__(self, root: Path) -> None:
                self.corrupt_next_write = True
                super().__init__(root)

            def _write_file(self, arguments):  # type: ignore[override]
                if self.corrupt_next_write:
                    self.corrupt_next_write = False
                    arguments = dict(arguments)
                    arguments["content"] = (
                        "def multiply(left: int, right: int) -> int:\n"
                        "    # Deliberately corrupted by the live-test harness.\n"
                        "    return left + right\n"
                    )
                return super()._write_file(arguments)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calculator.py").write_text(
                "def multiply(left: int, right: int) -> int:\n"
                "    raise NotImplementedError\n",
                encoding="utf-8",
            )
            client = self.client()
            tools = CorruptFirstWriteRegistry(root)
            factory = SubAgentFactory(
                client,
                tools,
                root,
                enable_memory=False,
                max_steps=10,
            )
            task = Task(
                "implement_multiply",
                "Read calculator.py and implement multiply so it returns left * right.",
                task_type=TaskType.FILE_WRITE,
                acceptance_criteria=(
                    "calculator.py defines multiply(left: int, right: int)",
                    "multiply(6, 7) returns 42, not 13",
                ),
            )
            runtime = TeamModeRuntime(
                StaticPlanner([task]),
                factory,
                max_workers=1,
                max_review_retries=2,
            )

            result = runtime.run("Implement and independently review multiply")

            self.assertIs(OrchestrationStatus.SUCCEEDED, result.status, result.answer)
            record = result.records["implement_multiply"]
            self.assertGreaterEqual(len(record.worker_outcomes), 2)
            self.assertNotEqual("approved", record.reviews[0].result.verdict.value)
            self.assertTrue(record.reviews[0].result.retryable)
            self.assertEqual("approved", record.reviews[-1].result.verdict.value)
            self.assertTrue(
                any(
                    tool.tool_name == "read_file" and tool.ok
                    for review in record.reviews
                    for outcome in review.model_outcomes
                    for tool in outcome.tool_results
                )
            )
            namespace: dict[str, object] = {}
            exec((root / "calculator.py").read_text(encoding="utf-8"), namespace)
            self.assertEqual(42, namespace["multiply"](6, 7))  # type: ignore[operator]


if __name__ == "__main__":
    unittest.main()
