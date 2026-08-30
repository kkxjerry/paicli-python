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
from .bootstrap import ReactRuntime, build_react_runtime
from .llm_client import ChatResponse, OpenAICompatibleClient, ToolCall
from .memory import ConversationHistoryCompactor, LongTermMemory, MemoryManager
from .planning import (
    DagScheduler,
    ExecutionPlan,
    LlmPlanner,
    PlanExecuteAgent,
    PlanGenerationError,
    PlanValidationError,
    PlanValidator,
    StaticPlanner,
    Task,
    TaskStatus,
    TaskType,
)
from .tools import (
    ConcurrencyPolicy,
    ResourceAccess,
    ResourceMode,
    ToolErrorType,
    ToolRegistry,
    ToolResult,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)

__version__ = "0.22.0"

__all__ = [
    "Agent",
    "AgentBudget",
    "AgentLoopEngine",
    "AgentLoopError",
    "AgentOutcome",
    "BudgetExitReason",
    "ChatResponse",
    "ConcurrencyPolicy",
    "ConversationHistoryCompactor",
    "DagScheduler",
    "CompletionDecision",
    "CompletionPolicy",
    "ExecutionPlan",
    "FinishReason",
    "LlmPlanner",
    "LongTermMemory",
    "MemoryManager",
    "NonEmptyCompletionPolicy",
    "OpenAICompatibleClient",
    "PlanExecuteAgent",
    "PlanGenerationError",
    "PlanValidationError",
    "PlanValidator",
    "ReactRuntime",
    "ResourceAccess",
    "ResourceMode",
    "RunStatus",
    "StaticPlanner",
    "Task",
    "TaskStatus",
    "TaskType",
    "ToolCall",
    "ToolErrorType",
    "ToolResult",
    "ToolRisk",
    "ToolRuntime",
    "ToolRegistry",
    "ToolSideEffect",
    "ToolSpec",
    "build_react_runtime",
    "__version__",
]
