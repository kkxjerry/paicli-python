"""PaiCLI Python: a coding agent built across 22 learning phases."""

from .agent import Agent, AgentLoopError
from .llm_client import ChatResponse, OpenAICompatibleClient, ToolCall
from .tools import ToolRegistry

__version__ = "0.22.0"

__all__ = [
    "Agent",
    "AgentLoopError",
    "ChatResponse",
    "OpenAICompatibleClient",
    "ToolCall",
    "ToolRegistry",
    "__version__",
]
