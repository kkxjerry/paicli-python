"""Provider-neutral LLM contracts and OpenAI-compatible HTTP clients.

The module intentionally keeps the public surface small:

- ``ChatResponse`` is the normalized response consumed by every Agent loop;
- ``OpenAICompatibleClient`` owns the HTTP/protocol boundary;
- ``RetryingLlmClient`` retries only failures explicitly classified transient;
- ``LlmClientFactory`` maps provider-specific environment variables to the
  same OpenAI-compatible client, including Alibaba Cloud DashScope.
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


class LlmError(RuntimeError):
    """Model transport/protocol failure with deterministic retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class ToolCall:
    """One model-requested function call."""

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
    """Normalized model response plus provider-reported token usage."""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning: str = ""
    streamed: bool = False


StreamHandler = Callable[[str, str], None]


class LlmClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        """Return the next assistant message."""
        ...


class OpenAICompatibleClient:
    """Call a provider implementing the OpenAI chat-completions protocol."""

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
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.provider = "custom"
        self.context_window = 128_000
        self.supports_prompt_caching = False
        # SSH-tunnel traffic must not accidentally pass through a host proxy.
        self._loopback_opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if _is_loopback_url(self.base_url)
            else None
        )

    @classmethod
    def from_env(cls) -> OpenAICompatibleClient:
        api_key = os.getenv("PAICLI_API_KEY", "").strip()
        model = os.getenv("PAICLI_MODEL", "glm-4-flash").strip()
        base_url = os.getenv(
            "PAICLI_BASE_URL",
            "https://open.bigmodel.cn/api/paas/v4",
        ).strip()
        timeout_raw = os.getenv("PAICLI_TIMEOUT_SECONDS", "120").strip()
        if not api_key:
            raise ValueError(
                "PAICLI_API_KEY is missing; copy .env.example to .env first"
            )
        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise ValueError("PAICLI_TIMEOUT_SECONDS must be a number") from exc
        client = cls(api_key, model, base_url, timeout)
        context_raw = os.getenv("PAICLI_CONTEXT_WINDOW", "128000").strip()
        try:
            client.context_window = max(8_000, int(context_raw))
        except ValueError as exc:
            raise ValueError("PAICLI_CONTEXT_WINDOW must be an integer") from exc
        return client

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

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            opener = (
                self._loopback_opener.open
                if self._loopback_opener is not None
                else urllib.request.urlopen
            )
            with opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in {408, 409, 425, 429, 500, 502, 503, 504}
            raise LlmError(
                f"model API returned HTTP {exc.code}: {detail}",
                status_code=exc.code,
                retryable=retryable,
                retry_after_seconds=_retry_after_seconds(
                    exc.headers.get("Retry-After")
                ),
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise LlmError(
                f"model API request failed: {reason}",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise LlmError(
                f"model API request failed: {exc}",
                retryable=True,
            ) from exc

        try:
            root = json.loads(raw)
            message = root["choices"][0]["message"]
            raw_calls = message.get("tool_calls") or []
            calls = tuple(
                ToolCall(
                    id=str(item["id"]),
                    name=str(item["function"]["name"]),
                    arguments=str(item["function"].get("arguments", "{}")),
                )
                for item in raw_calls
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LlmError(
                f"invalid model response: {raw[:500]}",
                retryable=False,
            ) from exc

        usage = root.get("usage") or {}
        return ChatResponse(
            content=_message_text(message.get("content")),
            tool_calls=calls,
            input_tokens=_usage_int(usage, "prompt_tokens", "input_tokens"),
            output_tokens=_usage_int(
                usage,
                "completion_tokens",
                "output_tokens",
            ),
            cached_input_tokens=_cached_input_tokens(usage),
            reasoning=_reasoning_text(message),
        )

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_event: StreamHandler | None = None,
    ) -> ChatResponse:
        """Consume an OpenAI-compatible SSE chat stream.

        The method reconstructs fragmented content, reasoning, and function
        arguments into the same ``ChatResponse`` contract used by blocking
        providers.  Callers that do not expose streaming remain compatible with
        ``chat`` through the optional protocol check in ``AgentLoopEngine``.
        """

        emit = on_event or (lambda _kind, _text: None)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            opener = (
                self._loopback_opener.open
                if self._loopback_opener is not None
                else urllib.request.urlopen
            )
            with opener(request, timeout=self.timeout_seconds) as response:
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                tool_parts: dict[int, dict[str, str]] = {}
                usage: dict[str, Any] = {}
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise LlmError(
                            f"invalid streaming model chunk: {data[:500]}",
                            retryable=False,
                        ) from exc
                    if isinstance(chunk.get("usage"), dict):
                        usage.update(chunk["usage"])
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = _message_text(delta.get("content"))
                    if content:
                        content_parts.append(content)
                        emit("content_delta", content)
                    reasoning = _reasoning_text(delta)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                        emit("reasoning_delta", reasoning)
                    for raw_call in delta.get("tool_calls") or []:
                        index = int(raw_call.get("index", 0))
                        state = tool_parts.setdefault(
                            index,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        if raw_call.get("id"):
                            state["id"] += str(raw_call["id"])
                        function = raw_call.get("function") or {}
                        if function.get("name"):
                            state["name"] += str(function["name"])
                        if function.get("arguments"):
                            state["arguments"] += str(function["arguments"])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in {408, 409, 425, 429, 500, 502, 503, 504}
            raise LlmError(
                f"model API returned HTTP {exc.code}: {detail}",
                status_code=exc.code,
                retryable=retryable,
                retry_after_seconds=_retry_after_seconds(
                    exc.headers.get("Retry-After")
                ),
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise LlmError(
                f"model API request failed: {reason}",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise LlmError(
                f"model API request failed: {exc}",
                retryable=True,
            ) from exc

        calls = tuple(
            ToolCall(
                value["id"] or f"stream-call-{index}",
                value["name"],
                value["arguments"] or "{}",
            )
            for index, value in sorted(tool_parts.items())
            if value["name"]
        )
        return ChatResponse(
            content="".join(content_parts),
            tool_calls=calls,
            input_tokens=_usage_int(usage, "prompt_tokens", "input_tokens"),
            output_tokens=_usage_int(
                usage,
                "completion_tokens",
                "output_tokens",
            ),
            cached_input_tokens=_cached_input_tokens(usage),
            reasoning="".join(reasoning_parts),
            streamed=True,
        )


class RetryingLlmClient:
    """Retry only transient failures, with a deterministic bounded policy."""

    def __init__(
        self,
        client: LlmClient,
        *,
        max_attempts: int = 3,
        base_delay_seconds: float = 0.25,
        max_delay_seconds: float = 4.0,
        max_retry_after_seconds: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if base_delay_seconds < 0 or max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("max retry delay cannot be smaller than base delay")
        if max_retry_after_seconds <= 0:
            raise ValueError("max_retry_after_seconds must be positive")
        self.client = client
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.max_retry_after_seconds = float(max_retry_after_seconds)
        self.sleep = sleep
        self.last_attempts = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempts = attempt
            try:
                return self.client.chat(messages, tools)
            except LlmError as exc:
                if not exc.retryable or attempt >= self.max_attempts:
                    raise
                delay = self._retry_delay(exc, attempt)
                if delay > 0:
                    self.sleep(delay)
        raise AssertionError("retry loop exhausted without returning or raising")

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_event: StreamHandler | None = None,
    ) -> ChatResponse:
        stream = getattr(self.client, "chat_stream", None)
        if not callable(stream):
            return self.chat(messages, tools)
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempts = attempt
            try:
                return stream(messages, tools, on_event)
            except LlmError as exc:
                if not exc.retryable or attempt >= self.max_attempts:
                    raise
                delay = self._retry_delay(exc, attempt)
                if delay > 0:
                    self.sleep(delay)
        raise AssertionError("stream retry loop exhausted without returning or raising")

    def _retry_delay(self, exc: LlmError, attempt: int) -> float:
        retry_after = exc.retry_after_seconds
        if retry_after is not None:
            if retry_after > self.max_retry_after_seconds:
                raise LlmError(
                    (
                        f"{exc}; provider requested Retry-After={retry_after:g}s, "
                        "which exceeds PaiCLI's local wait limit of "
                        f"{self.max_retry_after_seconds:g}s. Retry the command "
                        "after the provider window instead of blocking the Agent."
                    ),
                    status_code=exc.status_code,
                    retryable=False,
                    retry_after_seconds=retry_after,
                ) from exc
            return retry_after
        return min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** (attempt - 1)),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


def unwrap_llm_client(client: LlmClient) -> LlmClient:
    """Return the innermost provider behind transparent client decorators."""

    current: Any = client
    seen: set[int] = set()
    while hasattr(current, "client") and id(current) not in seen:
        seen.add(id(current))
        candidate = getattr(current, "client")
        if candidate is current:
            break
        current = candidate
    return current


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key_env: str
    default_model: str
    default_base_url: str
    context_window: int
    supports_prompt_caching: bool = False
    requires_api_key: bool = True


class LlmClientFactory:
    """Create configured OpenAI-compatible clients without network I/O."""

    PROVIDERS: Mapping[str, ProviderConfig] = {
        "dashscope": ProviderConfig(
            "dashscope",
            "DASHSCOPE_API_KEY",
            "qwen-plus",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            131_072,
            True,
        ),
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
        environment = os.environ if environ is None else environ
        name = provider.strip().lower()
        if name not in cls.PROVIDERS:
            supported = ", ".join(sorted(cls.PROVIDERS))
            raise ValueError(f"unknown provider {provider!r}; choose: {supported}")
        config = cls.PROVIDERS[name]
        prefix = name.upper()
        api_key = environment.get(config.api_key_env, "").strip()
        if config.requires_api_key and not api_key:
            raise ValueError(f"{config.api_key_env} is missing")
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

        timeout_raw = environment.get(f"{prefix}_TIMEOUT_SECONDS", "120").strip()
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError as exc:
            raise ValueError(f"{prefix}_TIMEOUT_SECONDS must be a number") from exc
        if timeout_seconds <= 0:
            raise ValueError(f"{prefix}_TIMEOUT_SECONDS must be positive")

        context_raw = environment.get(
            f"{prefix}_CONTEXT_WINDOW",
            str(config.context_window),
        ).strip()
        try:
            context_window = int(context_raw)
        except ValueError as exc:
            raise ValueError(f"{prefix}_CONTEXT_WINDOW must be an integer") from exc
        if context_window < 8_000:
            raise ValueError(f"{prefix}_CONTEXT_WINDOW must be at least 8000")

        client = OpenAICompatibleClient(
            api_key,
            model,
            base_url,
            timeout_seconds,
        )
        client.provider = config.name
        client.context_window = context_window
        client.supports_prompt_caching = config.supports_prompt_caching
        return client


def _message_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in {
                "text",
                "output_text",
            }:
                text = item.get("text", "")
                if isinstance(text, dict):
                    text = text.get("value", "")
                parts.append(str(text))
        return "".join(parts)
    return str(value)


def _reasoning_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    details = message.get("reasoning_details")
    if isinstance(details, str):
        return details
    if isinstance(details, list):
        parts: list[str] = []
        for item in details:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content") or item.get("reasoning")
                if value:
                    parts.append(str(value))
        return "".join(parts)
    return ""


def _retry_after_seconds(value: object) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except ValueError:
        return None


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
        cached = _usage_int(
            usage.get(key),
            "cached_tokens",
            "cached_input_tokens",
        )
        if cached:
            return cached
    return 0


def _is_loopback_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


__all__ = [
    "ChatResponse",
    "LlmClient",
    "LlmClientFactory",
    "LlmError",
    "OpenAICompatibleClient",
    "ProviderConfig",
    "RetryingLlmClient",
    "StreamHandler",
    "ToolCall",
    "unwrap_llm_client",
]
