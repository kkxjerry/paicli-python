"""Structured contracts for the tool gateway.

The public ``ToolRegistry.execute()`` string API remains available for the
learning phases that predate this module.  New Agent infrastructure uses
``ToolResult`` so it can distinguish invalid arguments, policy denial,
timeouts, and execution errors without parsing human-readable text.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable


class ToolRisk(str, Enum):
    """Static risk classification used by policy and UI layers."""

    SAFE = "safe"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class ToolSideEffect(str, Enum):
    """The broad kind of state a tool may change."""

    READ_ONLY = "read_only"
    FILE_WRITE = "file_write"
    DIRECTORY_WRITE = "directory_write"
    PROCESS = "process"
    UNKNOWN = "unknown"


class ConcurrencyPolicy(str, Enum):
    """How calls to a tool may be scheduled within one model response."""

    PARALLEL = "parallel"
    RESOURCE_LOCKED = "resource_locked"
    SERIAL = "serial"


class ResourceMode(str, Enum):
    READ = "read"
    WRITE = "write"


class ToolErrorType(str, Enum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    POLICY_DENIED = "policy_denied"
    APPROVAL_DENIED = "approval_denied"
    TIMEOUT = "timeout"
    RESOURCE_CONFLICT = "resource_conflict"
    EXECUTION_ERROR = "execution_error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ResourceAccess:
    """One read or write claim used by the conflict-aware scheduler.

    ``resource`` is a normalized path relative to the project root. ``*`` is a
    process-wide/global claim.  A recursive claim conflicts with descendants,
    which is useful for directory creation and directory listing.
    """

    resource: str
    mode: ResourceMode
    recursive: bool = False


@dataclass(frozen=True)
class ToolResult:
    """Machine-readable result of one tool invocation."""

    tool_name: str
    ok: bool
    content: str
    error_type: ToolErrorType | None = None
    retryable: bool = False
    timed_out: bool = False
    elapsed_ms: int = 0
    changed_files: tuple[str, ...] = ()
    accesses: tuple[ResourceAccess, ...] = ()
    call_id: str = ""

    @classmethod
    def success(
        cls,
        tool_name: str,
        content: str,
        *,
        elapsed_ms: int = 0,
        changed_files: tuple[str, ...] = (),
        accesses: tuple[ResourceAccess, ...] = (),
    ) -> ToolResult:
        return cls(
            tool_name=tool_name,
            ok=True,
            content=content,
            elapsed_ms=elapsed_ms,
            changed_files=changed_files,
            accesses=accesses,
        )

    @classmethod
    def failure(
        cls,
        tool_name: str,
        content: str,
        error_type: ToolErrorType,
        *,
        retryable: bool = False,
        timed_out: bool = False,
        elapsed_ms: int = 0,
        accesses: tuple[ResourceAccess, ...] = (),
    ) -> ToolResult:
        return cls(
            tool_name=tool_name,
            ok=False,
            content=content,
            error_type=error_type,
            retryable=retryable,
            timed_out=timed_out,
            elapsed_ms=elapsed_ms,
            accesses=accesses,
        )

    def with_call_id(self, call_id: str) -> ToolResult:
        return replace(self, call_id=str(call_id))

    def with_runtime_metadata(
        self,
        *,
        elapsed_ms: int,
        accesses: tuple[ResourceAccess, ...],
        changed_files: tuple[str, ...] | None = None,
    ) -> ToolResult:
        return replace(
            self,
            elapsed_ms=elapsed_ms,
            accesses=accesses,
            changed_files=self.changed_files if changed_files is None else changed_files,
        )


ToolHandler = Callable[[dict[str, Any]], str | ToolResult]
ResourceResolver = Callable[[dict[str, Any]], tuple[ResourceAccess, ...]]
ToolPreviewer = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    """Model-visible schema plus runtime execution and scheduling metadata.

    The first four fields preserve the original positional constructor used by
    older phases and third-party extensions.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    # Extensions must opt into a weaker classification. Failing closed avoids
    # a newly registered side-effecting tool being treated as read-only merely
    # because its author forgot to provide scheduling/policy metadata.
    risk: ToolRisk = ToolRisk.UNKNOWN
    side_effect: ToolSideEffect = ToolSideEffect.UNKNOWN
    concurrency: ConcurrencyPolicy = ConcurrencyPolicy.SERIAL
    timeout_seconds: float = 60.0
    resource_resolver: ResourceResolver | None = None
    previewer: ToolPreviewer | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("tool timeout_seconds must be positive")

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolExecutionFailure(RuntimeError):
    """A handler-raised failure that keeps an explicit error category."""

    def __init__(
        self,
        message: str,
        error_type: ToolErrorType = ToolErrorType.EXECUTION_ERROR,
        *,
        retryable: bool = False,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.timed_out = timed_out
