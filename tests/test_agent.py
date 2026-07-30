from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from paicli.agent import Agent, AgentLoopError
from paicli.llm_client import ChatResponse, ToolCall
from paicli.tools import ToolRegistry


class FakeClient:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        self.requests.append((list(messages), list(tools)))
        return self.responses.pop(0)


class AgentTest(unittest.TestCase):
    def test_tool_result_is_fed_back_before_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hello.txt").write_text("hello from tool", encoding="utf-8")
            client = FakeClient(
                [
                    ChatResponse(
                        content="",
                        tool_calls=(
                            ToolCall("call-1", "read_file", '{"path":"hello.txt"}'),
                        ),
                    ),
                    ChatResponse(content="The file says hello from tool."),
                ]
            )
            agent = Agent(client, ToolRegistry(root))

            answer = agent.run("What does hello.txt say?")

            self.assertEqual("The file says hello from tool.", answer)
            self.assertEqual(
                ["system", "user", "assistant", "tool", "assistant"],
                [message["role"] for message in agent.history],
            )
            self.assertEqual("call-1", agent.history[3]["tool_call_id"])
            self.assertEqual("hello from tool", agent.history[3]["content"])
            self.assertEqual(2, len(client.requests))

    def test_loop_limit_stops_repeated_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(
                [
                    ChatResponse(
                        content="",
                        tool_calls=(ToolCall("call-1", "list_dir", "{}"),),
                    )
                ]
            )
            agent = Agent(client, ToolRegistry(root), max_steps=1)

            with self.assertRaises(AgentLoopError):
                agent.run("Keep looking")


if __name__ == "__main__":
    unittest.main()

