"""模型层：将不同 Provider 统一为 Agent 可依赖的 ChatResponse。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class LlmError(RuntimeError):
    """模型 API 请求失败，或者响应无法解析。"""


@dataclass(frozen=True)
class ToolCall:
    """模型请求 Agent 执行的一次工具调用。"""

    # id 用于把后续 tool 结果和本次调用对应起来。
    id: str
    # name 必须能在 ToolRegistry 中找到。
    name: str
    # 协议中 arguments 是 JSON 字符串，不是已解析的 dict。
    arguments: str

    def as_message_dict(self) -> dict[str, Any]:
        """转成 assistant.tool_calls 需要的 OpenAI-compatible 字典结构。"""

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

    # 模型只调工具时，content 通常为空。
    content: str
    # 一个回复可同时请求多个工具。
    tool_calls: tuple[ToolCall, ...] = ()


class LlmClient(Protocol):
    """Agent 对模型的最小依赖，方便测试用 FakeClient 替换。"""

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
        # 早失败：配置错误在真正网络请求前就暴露。
        if not api_key:
            raise ValueError("api_key cannot be empty")
        if not model:
            raise ValueError("model cannot be empty")
        if not base_url:
            raise ValueError("base_url cannot be empty")
        self.api_key = api_key
        self.model = model
        # 去掉末尾 /，后面拼接路径时不会出现双斜杠。
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> OpenAICompatibleClient:
        """从三个 PAICLI 环境变量创建通用客户端。"""

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
        # messages 是完整消息链；tools 是可发给模型的 JSON Schema。
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            # auto 让模型自己决定直接回答还是发起工具调用。
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # ensure_ascii=False 保留中文，最后统一编码为 UTF-8 请求体。
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
            # urllib 是标准库，学习项目无需引入第三方 HTTP 库。
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # HTTPError 仍可能携带服务端返回的详细错误正文。
            detail = exc.read().decode("utf-8", errors="replace")
            raise LlmError(f"model API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LlmError(f"model API request failed: {exc.reason}") from exc

        try:
            # OpenAI-compatible 协议的核心消息位于 choices[0].message。
            root = json.loads(raw)
            message = root["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"invalid model response: {raw[:500]}") from exc

        # 从这一层出去后，Agent 不再需要理解供应商的原始 HTTP 字典。
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


@dataclass(frozen=True)
class ProviderConfig:
    """一个模型服务商的默认连接信息和能力元数据。"""

    name: str
    api_key_env: str
    default_model: str
    default_base_url: str
    context_window: int
    supports_prompt_caching: bool = False


class LlmClientFactory:
    """Phase 8：根据 provider 名称创建 OpenAI-compatible 客户端。

    工厂屏蔽不同服务商的 API Key 环境变量、默认模型和 base URL。
    它们仍必须提供兼容 OpenAI chat/completions 的 HTTP 接口。
    """

    PROVIDERS = {
        "glm": ProviderConfig(
            "glm",
            "GLM_API_KEY",
            "glm-4-flash",
            "https://open.bigmodel.cn/api/paas/v4",
            128_000,
        ),
        "deepseek": ProviderConfig(
            "deepseek",
            "DEEPSEEK_API_KEY",
            "deepseek-chat",
            "https://api.deepseek.com",
            128_000,
            True,
        ),
        "stepfun": ProviderConfig(
            "stepfun",
            "STEPFUN_API_KEY",
            "step-2-16k",
            "https://api.stepfun.com/v1",
            16_000,
        ),
        "kimi": ProviderConfig(
            "kimi",
            "KIMI_API_KEY",
            "kimi-k2",
            "https://api.moonshot.cn/v1",
            256_000,
            True,
        ),
    }

    @classmethod
    def create(
        cls,
        provider: str,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> OpenAICompatibleClient:
        # 测试可传入独立 environ，避免修改进程的真实 os.environ。
        environment = os.environ if environ is None else environ
        # 允许用户输入大小写混合和前后空格。
        name = provider.strip().lower()
        if name not in cls.PROVIDERS:
            supported = ", ".join(sorted(cls.PROVIDERS))
            raise ValueError(f"unknown provider {provider!r}; choose: {supported}")
        config = cls.PROVIDERS[name]
        # provider=deepseek 对应可选覆盖变量 DEEPSEEK_MODEL/DEEPSEEK_BASE_URL。
        prefix = name.upper()
        api_key = environment.get(config.api_key_env, "").strip()
        if not api_key:
            raise ValueError(f"{config.api_key_env} is missing")
        client = OpenAICompatibleClient(
            api_key=api_key,
            model=environment.get(f"{prefix}_MODEL", config.default_model),
            base_url=environment.get(
                f"{prefix}_BASE_URL",
                config.default_base_url,
            ),
        )
        # 这些元数据不影响 HTTP 调用，但后续 ContextManager 可据此选择上下文策略。
        client.provider = config.name
        client.context_window = config.context_window
        client.supports_prompt_caching = config.supports_prompt_caching
        return client
