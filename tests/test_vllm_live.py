"""A40 vLLM 真实全链路调试测试。

这个文件不用 FakeClient，会真正请求 Qwen3.5-9B。默认跳过，
避免日常单元测试依赖 SSH 隧道和 A40 服务。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from paicli import Agent, ToolRegistry
from paicli.llm_client import LlmClientFactory


RUN_LIVE_TEST = os.getenv("PAICLI_RUN_VLLM_LIVE_TEST", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@unittest.skipUnless(
    RUN_LIVE_TEST,
    "set PAICLI_RUN_VLLM_LIVE_TEST=1 and open the A40 SSH tunnel",
)
class VllmLiveDebugTest(unittest.TestCase):
    def test_real_agent_reads_file_and_feeds_result_back(self) -> None:
        """真实走完：调模型 -> 调工具 -> 回灌 -> 调模型 -> 结束。"""

        # 只需要开启测试开关和 SSH 隧道；地址仍可被环境变量覆盖。
        environment = dict(os.environ)
        environment["VLLM_BASE_URL"] = (
            environment.get("VLLM_BASE_URL", "").strip()
            or "http://127.0.0.1:18000/v1"
        )
        environment["VLLM_MODEL"] = (
            environment.get("VLLM_MODEL", "").strip() or "Qwen/Qwen3.5-9B"
        )
        client = LlmClientFactory.create("vllm", environ=environment)

        # 标记值只存在文件里，模型不调 read_file 就不可能稳定回答。
        marker = "PAICLI_REAL_TOOL_FEEDBACK_OK"
        events: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "probe.txt").write_text(marker, encoding="utf-8")
            tools = ToolRegistry(root)
            agent = Agent(
                client,
                tools,
                max_steps=5,
                on_event=lambda kind, text: events.append((kind, text)),
            )

            # IDE 调试时从这行 Step Into，就能进入 Agent.run 主循环。
            answer = agent.run(
                "必须调用 read_file 读取 probe.txt。"
                "收到工具结果后，最终只回答文件内容，不要猜。"
            )

        # 第一次 assistant 响应必须包含真实模型生成的 read_file tool_call。
        assistant_with_call = next(
            message
            for message in agent.history
            if message["role"] == "assistant" and message.get("tool_calls")
        )
        read_call = next(
            call
            for call in assistant_with_call["tool_calls"]
            if call["function"]["name"] == "read_file"
        )

        # tool_call_id 一致，才证明读文件结果被回灌到了正确的调用。
        tool_message = next(
            message
            for message in agent.history
            if message["role"] == "tool"
            and message["tool_call_id"] == read_call["id"]
        )
        self.assertEqual(marker, tool_message["content"])

        # 第二次模型请求看到回灌内容后才能生成最终答案。
        self.assertIn(marker, answer)
        self.assertEqual("assistant", agent.history[-1]["role"])
        self.assertNotIn("tool_calls", agent.history[-1])
        self.assertTrue(any(kind == "tool" for kind, _text in events))
        self.assertTrue(any(kind == "result" for kind, _text in events))


if __name__ == "__main__":
    unittest.main()
