"""Phase 12: long-context profiles, budgets, and cache visibility."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .memory import MemoryManager, estimate_tokens


class ContextProfile(str, Enum):
    SHORT = "short"
    BALANCED = "balanced"
    LONG = "long"

    @classmethod
    def for_window(cls, context_window: int) -> ContextProfile:
        if context_window >= 100_000:
            return cls.LONG
        if context_window >= 32_000:
            return cls.BALANCED
        return cls.SHORT


@dataclass(frozen=True)
class ContextSettings:
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
        top_k = {
            ContextProfile.SHORT: 5,
            ContextProfile.BALANCED: 10,
            ContextProfile.LONG: 20,
        }[profile]
        return cls(
            context_window,
            profile,
            int(context_window * 0.8),
            top_k,
            supports_prompt_caching,
        )


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class TokenUsageFormatter:
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
    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("budget limit must be positive")
        self.limit = limit

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        return sum(
            estimate_tokens(str(message.get("content", ""))) + 4
            for message in messages
        )

    def ensure_fits(self, messages: list[dict[str, Any]]) -> int:
        used = self.estimate_messages(messages)
        if used > self.limit:
            raise ValueError(
                f"context budget exceeded: estimated {used}, limit {self.limit}"
            )
        return used


@dataclass(frozen=True)
class ResourceIndexEntry:
    server: str
    uri: str
    description: str


class ContextController:
    """Chooses compaction and resource-index behavior by model window."""

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
        if memory and self.settings.profile is not ContextProfile.LONG:
            prepared = memory.prepare(messages)
        else:
            prepared = [dict(message) for message in messages]

        if self.settings.profile is ContextProfile.LONG and self.resources:
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
        self.budget.ensure_fits(prepared)
        return prepared
