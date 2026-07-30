from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from paicli.agent import Agent, AgentLoopError
from paicli.llm_client import ChatResponse, ToolCall
from paicli.tools import ToolRegistry


class FakeClient:
    """用预设响应代替真实模型，使 ReAct 流程测试不依赖网络和 API Key。"""

    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        # 保存每次请求，测试可以检查第二次调用是否已经带上 tool 结果。
        self.requests.append((list(messages), list(tools)))
        return self.responses.pop(0)


class AgentTest(unittest.TestCase):
    def test_tool_result_is_fed_back_before_final_answer(self) -> None:
        """模型先调用 read_file，看到文件内容后再给出最终答案。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 这个文件就是稍后 read_file 工具返回的观察结果。
            (root / "hello.txt").write_text("hello from tool", encoding="utf-8")
            client = FakeClient(
                [
                    # 第一次模型响应：不直接回答，而是要求调用 read_file。
                    ChatResponse(
                        content="",
                        tool_calls=(
                            ToolCall("call-1", "read_file", '{"path":"hello.txt"}'),
                        ),
                    ),
                    # 第二次模型响应：已经看到工具结果，因此返回最终答案。
                    ChatResponse(content="The file says hello from tool."),
                ]
            )
            agent = Agent(client, ToolRegistry(root))

            answer = agent.run("What does hello.txt say?")

            self.assertEqual("The file says hello from tool.", answer)
            # 这是第一期最关键的断言：工具调用和工具结果都进入了消息链。
            self.assertEqual(
                ["system", "user", "assistant", "tool", "assistant"],
                [message["role"] for message in agent.history],
            )
            self.assertEqual("call-1", agent.history[3]["tool_call_id"])
            self.assertEqual("hello from tool", agent.history[3]["content"])
            self.assertEqual(2, len(client.requests))

    def test_loop_limit_stops_repeated_tool_calls(self) -> None:
        """模型一直不结束时，max_steps 会阻止无限循环。"""

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
