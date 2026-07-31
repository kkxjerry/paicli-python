from __future__ import annotations

import unittest
from typing import Any

from paicli.llm_client import ChatResponse, ToolCall
from paicli.model_probe import probe_model


class ProbeClient:
    """只用来单测 probe 的判断逻辑；真实请求由 CLI 显式发起。"""

    def __init__(self, response: ChatResponse) -> None:
        self.response = response
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        self.requests.append((messages, tools))
        return self.response


class ModelProbeTest(unittest.TestCase):
    def test_chat_probe_accepts_non_empty_model_answer(self) -> None:
        """基础检查只要收到非空回答就说明 HTTP 与聊天链路可用。"""

        result = probe_model(ProbeClient(ChatResponse("PAICLI_OK")), "chat")

        self.assertTrue(result.ok)
        self.assertIn("PAICLI_OK", result.detail)

    def test_tool_probe_requires_expected_structured_call(self) -> None:
        """工具检查必须收到 probe_echo，普通文本回答不算通过。"""

        passed = probe_model(
            ProbeClient(
                ChatResponse(
                    "",
                    (ToolCall("probe-1", "probe_echo", '{"text":"PAICLI_TOOL_OK"}'),),
                )
            ),
            "tools",
        )
        failed = probe_model(ProbeClient(ChatResponse("PAICLI_TOOL_OK")), "tools")
        wrong_arguments = probe_model(
            ProbeClient(
                ChatResponse(
                    "",
                    (ToolCall("probe-2", "probe_echo", '{"text":"wrong"}'),),
                )
            ),
            "tools",
        )

        self.assertTrue(passed.ok)
        self.assertFalse(failed.ok)
        self.assertIn("without a tool call", failed.detail)
        self.assertFalse(wrong_arguments.ok)
        self.assertIn("unexpected tool arguments", wrong_arguments.detail)
