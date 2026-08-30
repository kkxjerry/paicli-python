"""模型层：将不同 Provider 统一为 Agent 可依赖的 ChatResponse。"""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.parse
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
    """Agent 真正需要的标准化模型响应与用量。"""

    # 模型只调工具时，content 通常为空。
    content: str
    # 一个回复可同时请求多个工具。
    tool_calls: tuple[ToolCall, ...] = ()
    # OpenAI-compatible usage；Provider 不返回时保持 0。
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


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
        # 本地 vLLM 常经过 SSH 隧道访问。如果机器设置了 HTTP_PROXY，
        # urllib 可能把 127.0.0.1 也发给代理；回环地址应始终直连。
        self._loopback_opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if _is_loopback_url(self.base_url)
            else None
        )

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
            open_request = (
                self._loopback_opener.open
                if self._loopback_opener is not None
                else urllib.request.urlopen
            )
            with open_request(request, timeout=self.timeout_seconds) as response:
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
        usage = root.get("usage") or {}
        input_tokens = _usage_int(usage, "prompt_tokens", "input_tokens")
        output_tokens = _usage_int(usage, "completion_tokens", "output_tokens")
        cached_input_tokens = _cached_input_tokens(usage)
        return ChatResponse(
            content=message.get("content") or "",
            tool_calls=calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
        )


def _usage_int(usage: object, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _cached_input_tokens(usage: object) -> int:
    if not isinstance(usage, dict):
        return 0
    direct = _usage_int(
        usage,
        "cached_tokens",
        "cached_input_tokens",
        "prompt_cache_hit_tokens",
        "input_cache_hit_tokens",
    )
    if direct:
        return direct
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key)
        cached = _usage_int(details, "cached_tokens", "cached_input_tokens")
        if cached:
            return cached
    return 0


def _is_loopback_url(url: str) -> bool:
    """判断 API 地址是否指向本机，用于避免 SSH 隧道被代理劫持。"""

    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # 普通域名仍按用户的 HTTP(S)_PROXY 配置访问。
        return False


@dataclass(frozen=True)
class ProviderConfig:
    """一个模型服务商的默认连接信息和能力元数据。"""

    name: str
    api_key_env: str
    default_model: str
    default_base_url: str
    context_window: int
    supports_prompt_caching: bool = False
    # 云端 API 必须有密钥；vLLM 内网服务可以不启用鉴权。
    requires_api_key: bool = True


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
        "vllm": ProviderConfig(
            "vllm",
            "VLLM_API_KEY",
            "",
            "",
            32_000,
            False,
            False,
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
        if config.requires_api_key and not api_key:
            raise ValueError(f"{config.api_key_env} is missing")
        # vLLM 未启用 --api-key 时也接受这个占位值；它不是真实密钥。
        api_key = api_key or "EMPTY"
        model = environment.get(f"{prefix}_MODEL", config.default_model).strip()
        base_url = environment.get(
            f"{prefix}_BASE_URL",
            config.default_base_url,
        ).strip()
        if not model:
            raise ValueError(f"{prefix}_MODEL is missing")
        if not base_url:
            raise ValueError(f"{prefix}_BASE_URL is missing")
        client = OpenAICompatibleClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
        )
        # 这些元数据不影响 HTTP 调用，但后续 ContextManager 可据此选择上下文策略。
        client.provider = config.name
        client.context_window = config.context_window
        client.supports_prompt_caching = config.supports_prompt_caching
        return client
