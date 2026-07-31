"""Phase 12：长上下文档位、Token Budget 和 Prompt Cache 可见性。

根据模型 context window 选择 SHORT/BALANCED/LONG，再决定压缩策略、
RAG 候选数量和可用 token 预算。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .memory import MemoryManager, estimate_tokens


class ContextProfile(str, Enum):
    """将连续的 context window 归类成三个策略档位。"""

    SHORT = "short"
    BALANCED = "balanced"
    LONG = "long"

    @classmethod
    def for_window(cls, context_window: int) -> ContextProfile:
        # 阈值是本项目的策略选择，不是模型协议的固定标准。
        if context_window >= 100_000:
            return cls.LONG
        if context_window >= 32_000:
            return cls.BALANCED
        return cls.SHORT


@dataclass(frozen=True)
class ContextSettings:
    """由模型能力派生出的上下文运行参数。"""

    window: int
    profile: ContextProfile
    token_budget: int
    rag_top_k: int
    prompt_caching: bool

    @classmethod
    def for_model(
        cls,
        context_window: int,
        *,
        supports_prompt_caching: bool = False,
    ) -> ContextSettings:
        profile = ContextProfile.for_window(context_window)
        # 窗口越大，允许 RAG 放入的代码/资源候选越多。
        top_k = {
            ContextProfile.SHORT: 5,
            ContextProfile.BALANCED: 10,
            ContextProfile.LONG: 20,
        }[profile]
        return cls(
            context_window,
            profile,
            # 只使用 80% 作为输入预算，为模型输出和估算误差留余量。
            int(context_window * 0.8),
            top_k,
            supports_prompt_caching,
        )


@dataclass(frozen=True)
class TokenUsage:
    """一次模型调用的输入、输出和命中缓存 token 统计。"""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class TokenUsageFormatter:
    """将 TokenUsage 转为状态栏可显示文本。"""

    @staticmethod
    def format(usage: TokenUsage, settings: ContextSettings) -> str:
        cache = (
            f", cached {usage.cached_input_tokens}"
            if settings.prompt_caching
            else ""
        )
        return (
            f"{usage.total_tokens}/{settings.token_budget} tokens "
            f"(window {settings.window}{cache})"
        )


class AgentBudget:
    """用粗略 token 估算在调模型前执行最后预算门禁。"""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("budget limit must be positive")
        self.limit = limit

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        # 每条额外加 4，粗略补偿 role/分隔符等聊天模板开销。
        return sum(
            estimate_tokens(str(message.get("content", ""))) + 4
            for message in messages
        )

    def ensure_fits(self, messages: list[dict[str, Any]]) -> int:
        # 超预算直接拒绝，避免请求发出后才收到 provider 的上下文超限错误。
        used = self.estimate_messages(messages)
        if used > self.limit:
            raise ValueError(
                f"context budget exceeded: estimated {used}, limit {self.limit}"
            )
        return used


@dataclass(frozen=True)
class ResourceIndexEntry:
    """可注入长上下文的 MCP Resource 目录项，只含索引而非正文。"""

    server: str
    uri: str
    description: str


class ContextController:
    """根据模型窗口决定是否压缩，以及是否注入资源索引。"""

    def __init__(
        self,
        settings: ContextSettings,
        *,
        resources: Iterable[ResourceIndexEntry] = (),
    ) -> None:
        self.settings = settings
        self.resources = list(resources)
        self.budget = AgentBudget(settings.token_budget)

    def prepare(
        self,
        messages: list[dict[str, Any]],
        memory: MemoryManager | None = None,
    ) -> list[dict[str, Any]]:
        # SHORT/BALANCED 窗口优先用 MemoryManager 压缩；LONG 保留完整历史。
        if memory and self.settings.profile is not ContextProfile.LONG:
            prepared = memory.prepare(messages)
        else:
            prepared = [dict(message) for message in messages]

        if self.settings.profile is ContextProfile.LONG and self.resources:
            # 长窗口先注入“可用资源目录”，模型后续可决定读哪个 Resource。
            index = "\n".join(
                f"- {item.server}: {item.uri} ({item.description})"
                for item in self.resources
            )
            prepared.insert(
                1,
                {
                    "role": "system",
                    "content": f"Available MCP resource index:\n{index}",
                },
            )
        # 所有压缩/注入完成后再做最终预算检查。
        self.budget.ensure_fits(prepared)
        return prepared
