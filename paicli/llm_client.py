"""第一期模型层：统一响应结构，并调用 OpenAI-compatible HTTP 接口。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class LlmError(RuntimeError):
    """模型接口请求失败，或者返回了无法解析的数据。"""


@dataclass(frozen=True)
class ToolCall:
    """模型要求 Agent 执行的一次工具调用。"""

    # id 用于把后续 tool 结果和本次调用对应起来。
    id: str
    # name 必须能在 ToolRegistry 中找到。
    name: str
    # OpenAI-compatible 协议中的 arguments 是 JSON 字符串，不是字典。
    arguments: str

    def as_message_dict(self) -> dict[str, Any]:
        """转换成 assistant 消息中 tool_calls 所需的协议格式。"""

        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }


@dataclass(frozen=True)
class ChatResponse:
    """Agent 真正需要的标准化模型响应。"""

    # 普通文本答案；模型只调用工具时通常为空字符串。
    content: str
    # 一个响应可以同时要求执行多个工具。
    tool_calls: tuple[ToolCall, ...] = ()


class LlmClient(Protocol):
    """Agent 对模型的最小依赖协议，方便测试时替换成 FakeClient。"""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        """根据消息历史和工具定义返回下一条 assistant 消息。"""


class OpenAICompatibleClient:
    """调用兼容 OpenAI ``/chat/completions`` 协议的模型服务。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 120,
    ) -> None:
        # 尽早校验配置，避免真正请求时才暴露低级配置错误。
        if not api_key:
            raise ValueError("api_key cannot be empty")
        if not model:
            raise ValueError("model cannot be empty")
        if not base_url:
            raise ValueError("base_url cannot be empty")
        self.api_key = api_key
        self.model = model
        # 去掉末尾斜杠，后面拼接接口路径时不会出现双斜杠。
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> OpenAICompatibleClient:
        """读取三个环境变量构造客户端。"""

        api_key = os.getenv("PAICLI_API_KEY", "").strip()
        model = os.getenv("PAICLI_MODEL", "glm-4-flash").strip()
        base_url = os.getenv(
            "PAICLI_BASE_URL",
            "https://open.bigmodel.cn/api/paas/v4",
        ).strip()
        if not api_key:
            raise ValueError(
                "PAICLI_API_KEY is missing; copy .env.example to .env first"
            )
        return cls(api_key=api_key, model=model, base_url=base_url)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        # messages 保存完整对话；tools 是 ToolRegistry 生成的 JSON Schema。
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            # tool_choice=auto 让模型自己决定回答文本还是调用工具。
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # ensure_ascii=False 保留中文，最终统一编码成 UTF-8 请求体。
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            # urllib 是 Python 标准库，因此第一期不需要安装第三方 HTTP 包。
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # HTTPError 仍然可能携带模型服务返回的详细错误正文。
            detail = exc.read().decode("utf-8", errors="replace")
            raise LlmError(f"model API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LlmError(f"model API request failed: {exc.reason}") from exc

        try:
            # OpenAI-compatible 响应的核心内容位于 choices[0].message。
            root = json.loads(raw)
            message = root["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"invalid model response: {raw[:500]}") from exc

        # 把供应商返回的字典转换成 Agent 使用的 ToolCall。
        # 从这一层出去后，Agent 不再需要了解原始 HTTP 响应结构。
        calls = tuple(
            ToolCall(
                id=item["id"],
                name=item["function"]["name"],
                arguments=item["function"].get("arguments", "{}"),
            )
            for item in message.get("tool_calls") or []
        )
        return ChatResponse(
            content=message.get("content") or "",
            tool_calls=calls,
        )
