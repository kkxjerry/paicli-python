"""PaiCLI Python: a coding agent built across 22 learning phases."""

from .agent import Agent, AgentLoopError
from .agents import (
    AgentBudget,
    AgentLoopEngine,
    AgentOutcome,
    BudgetExitReason,
    CompletionDecision,
    CompletionPolicy,
    FinishReason,
    NonEmptyCompletionPolicy,
    RunStatus,
    ToolRuntime,
)
from .llm_client import ChatResponse, OpenAICompatibleClient, ToolCall
from .tools import ToolRegistry

__version__ = "0.22.0"

__all__ = [
    "Agent",
    "AgentBudget",
    "AgentLoopEngine",
    "AgentLoopError",
    "AgentOutcome",
    "BudgetExitReason",
    "ChatResponse",
    "CompletionDecision",
    "CompletionPolicy",
    "FinishReason",
    "NonEmptyCompletionPolicy",
    "OpenAICompatibleClient",
    "RunStatus",
    "ToolCall",
    "ToolRuntime",
    "ToolRegistry",
    "__version__",
]
