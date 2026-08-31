"""PaiCLI Python — a local coding-agent harness with three execution modes."""

from importlib import import_module

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
    ToolObservingCompletionPolicy,
    ToolRuntime,
)
from .bootstrap import (
    ApplicationRuntime,
    ReactRuntime,
    build_application_runtime,
    build_react_runtime,
    default_rag_path,
)
from .execution import CoordinatedRun, RunCoordinator
from .llm_client import (
    ChatResponse,
    LlmClient,
    LlmClientFactory,
    LlmError,
    OpenAICompatibleClient,
    RetryingLlmClient,
    ToolCall,
    unwrap_llm_client,
)
from .managed_memory import (
    ManagedLongTermMemory,
    ManagedMemoryStore,
    MemoryKind,
    MemoryStatus,
)
from .memory import ConversationHistoryCompactor, LongTermMemory, MemoryManager
from .observability import (
    ModelPricing,
    PricingCatalog,
    RunBudget,
    RunBudgetExceeded,
    RunLimits,
    TraceStore,
)
from .orchestration import (
    AggregationResult,
    DeterministicResultAggregator,
    LlmResultAggregator,
    OrchestrationMode,
    OrchestrationResult,
    OrchestrationStatus,
    PlanModeRuntime,
    PlanReviewAction,
    PlanReviewDecision,
    TaskRunRecord,
    TeamModeRuntime,
)
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
    TaskConcurrencyPolicy,
    TaskStatus,
    TaskType,
)
from .rag import (
    CodeChunk,
    CodeChunker,
    CodeIndex,
    EmbeddingClient,
    IndexRefreshingToolGateway,
    OpenAICompatibleEmbeddingClient,
    SearchResult,
    VectorStore,
)
from .review import ReviewResult, ReviewRun, ReviewVerdict, ReviewerAgent
from .safety import RollbackPolicy, WorkspaceRunGuard
from .state import RunStateStore, StoredRun, StoredRunStatus
from .subagents import (
    DependencyResult,
    SubAgent,
    SubAgentFactory,
    SubAgentRole,
    TaskCompletionPolicy,
    TaskPacket,
    ToolScope,
)
from .tools import (
    ConcurrencyPolicy,
    ResourceAccess,
    ResourceMode,
    ScopedToolRuntime,
    ToolErrorType,
    ToolRegistry,
    ToolResult,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)

__version__ = "1.0.0"

_LAZY_EXPORTS = {
    "EvalSuite": (".evaluation", "EvalSuite"),
    "EvalTask": (".evaluation", "EvalTask"),
    "EvaluationReport": (".evaluation", "EvaluationReport"),
    "EvaluationRunner": (".evaluation", "EvaluationRunner"),
    "GitRevisionCaseExecutor": (".evaluation", "GitRevisionCaseExecutor"),
    "compare_reports": (".evaluation", "compare_reports"),
    "load_suite": (".evaluation", "load_suite"),
    "summarize_stability": (".evaluation", "summarize_stability"),
}


def __getattr__(name: str):
    """Load optional command modules only when a public symbol is requested.

    Keeping ``paicli.evaluation`` out of package initialization lets
    ``python -m paicli.evaluation`` execute without runpy's double-import
    warning while preserving the original package-level API.
    """

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "Agent",
    "AgentBudget",
    "AgentLoopEngine",
    "AgentLoopError",
    "AgentOutcome",
    "AggregationResult",
    "ApplicationRuntime",
    "BudgetExitReason",
    "ChatResponse",
    "CodeChunk",
    "CodeChunker",
    "CodeIndex",
    "CompletionDecision",
    "CompletionPolicy",
    "ConcurrencyPolicy",
    "CoordinatedRun",
    "ConversationHistoryCompactor",
    "DagScheduler",
    "DependencyResult",
    "DeterministicResultAggregator",
    "EmbeddingClient",
    "EvalSuite",
    "EvalTask",
    "EvaluationReport",
    "EvaluationRunner",
    "ExecutionPlan",
    "GitRevisionCaseExecutor",
    "FinishReason",
    "IndexRefreshingToolGateway",
    "LlmClient",
    "LlmClientFactory",
    "LlmError",
    "LlmPlanner",
    "LlmResultAggregator",
    "LongTermMemory",
    "ManagedLongTermMemory",
    "ManagedMemoryStore",
    "MemoryKind",
    "MemoryManager",
    "MemoryStatus",
    "ModelPricing",
    "NonEmptyCompletionPolicy",
    "OpenAICompatibleClient",
    "OpenAICompatibleEmbeddingClient",
    "OrchestrationMode",
    "OrchestrationResult",
    "OrchestrationStatus",
    "PlanExecuteAgent",
    "PlanGenerationError",
    "PlanModeRuntime",
    "PlanReviewAction",
    "PlanReviewDecision",
    "PlanValidationError",
    "PlanValidator",
    "PricingCatalog",
    "ReactRuntime",
    "ResourceAccess",
    "ResourceMode",
    "RetryingLlmClient",
    "ReviewResult",
    "ReviewRun",
    "ReviewVerdict",
    "ReviewerAgent",
    "RollbackPolicy",
    "RunBudget",
    "RunBudgetExceeded",
    "RunCoordinator",
    "RunLimits",
    "RunStateStore",
    "RunStatus",
    "ScopedToolRuntime",
    "SearchResult",
    "StaticPlanner",
    "StoredRun",
    "StoredRunStatus",
    "SubAgent",
    "SubAgentFactory",
    "SubAgentRole",
    "Task",
    "TaskCompletionPolicy",
    "TaskConcurrencyPolicy",
    "TaskPacket",
    "TaskRunRecord",
    "TaskStatus",
    "TaskType",
    "TeamModeRuntime",
    "ToolCall",
    "ToolErrorType",
    "ToolObservingCompletionPolicy",
    "ToolResult",
    "ToolRisk",
    "ToolRuntime",
    "ToolRegistry",
    "ToolScope",
    "ToolSideEffect",
    "ToolSpec",
    "TraceStore",
    "VectorStore",
    "WorkspaceRunGuard",
    "build_application_runtime",
    "build_react_runtime",
    "compare_reports",
    "default_rag_path",
    "load_suite",
    "summarize_stability",
    "unwrap_llm_client",
    "__version__",
]
