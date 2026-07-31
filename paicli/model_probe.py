"""真实模型连通性与 Tool Calling 能力检查。

单元测试使用 FakeClient 是为了稳定验证 Agent 逻辑，本模块则专门用于
在用户明确执行 ``--check-model`` 时请求真实云端 API 或 vLLM 服务。
检查只要求模型生成一次工具调用，不会真正执行 Shell 或写文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from .llm_client import LlmClient

ProbeMode = Literal["chat", "tools"]


@dataclass(frozen=True)
class ModelProbeResult:
    """一次真实模型检查的结构化结果。"""

    ok: bool
    detail: str


def probe_model(client: LlmClient, mode: ProbeMode) -> ModelProbeResult:
    """检查基础对话，或检查模型能否返回结构化 tool_call。"""

    if mode == "chat":
        response = client.chat(
            [
                {
                    "role": "system",
                    "content": "You are a connectivity probe.",
                },
                {
                    "role": "user",
                    "content": "Reply with exactly PAICLI_OK.",
                },
            ],
            [],
        )
        content = response.content.strip()
        if not content:
            return ModelProbeResult(False, "model returned an empty response")
        return ModelProbeResult(True, f"chat response: {content[:200]}")

    if mode != "tools":
        raise ValueError(f"unknown model probe mode: {mode}")

    # 工具故意无副作用：只检查模型是否生成正确的 function call。
    tools = [
        {
            "type": "function",
            "function": {
                "name": "probe_echo",
                "description": "Return the supplied text unchanged.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    response = client.chat(
        [
            {
                "role": "system",
                "content": "Always use the requested tool when one is provided.",
            },
            {
                "role": "user",
                "content": "Call probe_echo with text PAICLI_TOOL_OK.",
            },
        ],
        tools,
    )
    if not response.tool_calls:
        return ModelProbeResult(
            False,
            "model answered without a tool call; check model/tool-parser support",
        )
    call = response.tool_calls[0]
    if call.name != "probe_echo":
        return ModelProbeResult(False, f"unexpected tool call: {call.name}")
    try:
        arguments = json.loads(call.arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        return ModelProbeResult(False, f"tool arguments are not valid JSON: {exc}")
    if not isinstance(arguments, dict) or arguments.get("text") != "PAICLI_TOOL_OK":
        return ModelProbeResult(False, f"unexpected tool arguments: {call.arguments}")
    return ModelProbeResult(True, f"tool call: {call.name} {call.arguments}")
