from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.bootstrap import build_application_runtime
from paicli.llm_client import ChatResponse, unwrap_llm_client
from paicli.subagents import SubAgentRole


class _Client:
    context_window = 32_000
    supports_prompt_caching = False

    def __init__(self, name: str) -> None:
        self.model = name
        self.provider = "test"

    def chat(self, messages, tools):  # type: ignore[no-untyped-def]
        return ChatResponse("done")


class RoleModelTest(unittest.TestCase):
    def test_each_role_can_use_a_distinct_client_and_project_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text(
                "PROJECT_CONTRACT: run deterministic tests before completion.\n",
                encoding="utf-8",
            )
            react = _Client("react")
            planner = _Client("planner")
            worker = _Client("worker")
            reviewer = _Client("reviewer")
            aggregator = _Client("aggregator")
            runtime = build_application_runtime(
                react,
                root,
                enable_memory=False,
                enable_rag=False,
                enable_trace=False,
                enable_hitl=False,
                role_clients={
                    "planner": planner,
                    "worker": worker,
                    "reviewer": reviewer,
                    "aggregator": aggregator,
                },
            )
            try:
                self.assertIs(react, unwrap_llm_client(runtime.role_clients["react"]))
                self.assertIs(planner, unwrap_llm_client(runtime.role_clients["planner"]))
                self.assertIs(
                    worker,
                    unwrap_llm_client(runtime.subagents.client_for(SubAgentRole.WORKER)),
                )
                self.assertIs(
                    reviewer,
                    unwrap_llm_client(runtime.subagents.client_for(SubAgentRole.REVIEWER)),
                )
                self.assertIs(
                    aggregator,
                    unwrap_llm_client(runtime.subagents.client_for(SubAgentRole.AGGREGATOR)),
                )

                self.assertIn(
                    "PROJECT_CONTRACT",
                    str(runtime.react.agent.history[0]["content"]),
                )
                self.assertIn("PROJECT_CONTRACT", runtime.plan.planner.system_prompt)
                for role in SubAgentRole:
                    self.assertIn(
                        "PROJECT_CONTRACT",
                        runtime.subagents.system_prompts[role],
                    )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
