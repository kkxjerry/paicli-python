"""Default dependency wiring for the ReAct runtime.

Before Phase 4, the CLI instantiated only ``Agent`` and ``ToolRegistry`` even
though context, memory, LSP, and the ``save_memory`` tool already existed. This
module provides one testable assembly point so future Plan and Team modes can
reuse the same infrastructure instead of rebuilding it inconsistently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .agent import Agent
from .context import ContextController, ContextSettings
from .llm_client import LlmClient
from .lsp import LspManager
from .memory import (
    ConversationHistoryCompactor,
    LongTermMemory,
    MemoryManager,
    register_memory_tool,
)
from .tools import ToolRegistry

EventHandler = Callable[[str, str], None]


@dataclass(frozen=True)
class ReactRuntime:
    """All services participating in the default ReAct execution path."""

    agent: Agent
    tools: ToolRegistry
    context: ContextController
    memory: MemoryManager | None
    long_term_memory: LongTermMemory | None

    @property
    def settings(self) -> ContextSettings:
        # ContextController is reconfigured when /model switches providers, so
        # expose its current settings rather than a stale construction snapshot.
        return self.context.settings


def default_memory_path() -> Path:
    configured = os.getenv("PAICLI_MEMORY_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".paicli" / "memory.jsonl"


def build_react_runtime(
    client: LlmClient,
    project_root: str | Path,
    *,
    allow_shell: bool = False,
    enable_memory: bool = True,
    memory_path: str | Path | None = None,
    max_steps: int = 50,
    stagnation_window: int = 3,
    token_budget: int | None = None,
    on_event: EventHandler | None = None,
) -> ReactRuntime:
    """Build the production-facing ReAct path from shared services."""

    root = Path(project_root).resolve()
    settings = ContextSettings.for_model(
        _context_window(client),
        supports_prompt_caching=bool(
            getattr(client, "supports_prompt_caching", False)
        ),
    )
    tools = ToolRegistry(root, allow_shell=allow_shell)

    memory: MemoryManager | None = None
    long_term: LongTermMemory | None = None
    if enable_memory:
        resolved_memory_path = Path(memory_path or default_memory_path()).expanduser()
        long_term = LongTermMemory(resolved_memory_path)
        history_compactor = ConversationHistoryCompactor(client)
        memory = MemoryManager(
            max_tokens=settings.compression_trigger_tokens,
            long_term=long_term,
            history_compactor=history_compactor,
            long_term_context_tokens=settings.memory_context_tokens,
        )
        register_memory_tool(tools, long_term)

    context = ContextController(settings)
    agent = Agent(
        client,
        tools,
        max_steps=max_steps,
        stagnation_window=stagnation_window,
        token_budget=token_budget,
        on_event=on_event,
        memory=memory,
        context=context,
        lsp=LspManager(root),
    )
    return ReactRuntime(
        agent=agent,
        tools=tools,
        context=context,
        memory=memory,
        long_term_memory=long_term,
    )


def _context_window(client: LlmClient) -> int:
    raw = getattr(client, "context_window", 128_000)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 128_000
    return max(8_000, value)
