"""Provider-neutral types and a small OpenAI-compatible HTTP client."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class LlmError(RuntimeError):
    """Raised when the model API cannot produce a usable response."""


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: str

    def as_message_dict(self) -> dict[str, Any]:
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
    """The normalized part of a chat-completion response used by Agent."""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()


class LlmClient(Protocol):
    """The only model capability required by the Phase 1 agent."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        """Return the next assistant message."""


class OpenAICompatibleClient:
    """Calls an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 120,
    ) -> None:
        if not api_key:
            raise ValueError("api_key cannot be empty")
        if not model:
            raise ValueError("model cannot be empty")
        if not base_url:
            raise ValueError("base_url cannot be empty")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> OpenAICompatibleClient:
        """Build a client from the three PAICLI environment variables."""

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
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

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
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LlmError(f"model API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LlmError(f"model API request failed: {exc.reason}") from exc

        try:
            root = json.loads(raw)
            message = root["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"invalid model response: {raw[:500]}") from exc

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
    name: str
    api_key_env: str
    default_model: str
    default_base_url: str
    context_window: int
    supports_prompt_caching: bool = False


class LlmClientFactory:
    """Phase 8 provider strategy for OpenAI-compatible model APIs."""

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
        environment = os.environ if environ is None else environ
        name = provider.strip().lower()
        if name not in cls.PROVIDERS:
            supported = ", ".join(sorted(cls.PROVIDERS))
            raise ValueError(f"unknown provider {provider!r}; choose: {supported}")
        config = cls.PROVIDERS[name]
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
        client.provider = config.name
        client.context_window = config.context_window
        client.supports_prompt_caching = config.supports_prompt_caching
        return client
