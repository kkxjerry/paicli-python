"""Hard policy, human approval, diff previews, and durable audit records."""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .tool_contracts import ToolErrorType, ToolResult, ToolRisk, ToolSpec
from .tools import ToolRegistry


class RiskLevel(str, Enum):
    SAFE = "safe"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"


class ApprovalMode(str, Enum):
    """Non-interactive behavior and interactive prompt selection."""

    ASK = "ask"
    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    arguments: dict[str, object]
    risk: RiskLevel
    reason: str
    preview: str = ""


@dataclass(frozen=True)
class ApprovalResult:
    decision: ApprovalDecision
    arguments: dict[str, object] | None = None


ApprovalHandler = Callable[[ApprovalRequest], ApprovalResult]


class ConsoleApprovalHandler:
    """Approval handler whose non-interactive behavior is explicit."""

    def __init__(
        self,
        mode: ApprovalMode | str = ApprovalMode.ASK,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self.mode = mode if isinstance(mode, ApprovalMode) else ApprovalMode(mode)
        self.input_fn = input_fn
        self.output_fn = output_fn

    def __call__(self, request: ApprovalRequest) -> ApprovalResult:
        if self.mode is ApprovalMode.ALLOW:
            return ApprovalResult(ApprovalDecision.APPROVE)
        if self.mode is ApprovalMode.DENY:
            return ApprovalResult(ApprovalDecision.DENY)
        self.output_fn(
            f"\nApproval required: {request.tool_name} "
            f"[{request.risk.value}] — {request.reason}"
        )
        if request.preview:
            self.output_fn(request.preview)
        try:
            value = self.input_fn("Allow this tool call? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            self.output_fn("")
            return ApprovalResult(ApprovalDecision.DENY)
        return ApprovalResult(
            ApprovalDecision.APPROVE
            if value in {"y", "yes", "allow", "approve"}
            else ApprovalDecision.DENY
        )


class CommandGuard:
    """Reject high-confidence destructive shell patterns before HITL."""

    BLOCKED_PATTERNS = (
        r"\bsudo\b",
        r"\brm\s+-[a-z]*r[a-z]*f\b",
        r"\bmkfs(?:\.\w+)?\b",
        r"\bdd\b.*\bof=/dev/",
        r":\(\)\s*\{\s*:\|:&\s*\};:",
        r"\b(?:curl|wget)\b[^|]*\|\s*(?:sh|bash)\b",
        r"\b(?:shutdown|reboot|poweroff)\b",
        r"\bchmod\s+-R\s+777\s+/\b",
        r"\bchown\s+-R\b[^\n]*\s+/\s*$",
    )

    @classmethod
    def reject_reason(cls, command: str) -> str | None:
        for pattern in cls.BLOCKED_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                return f"command rejected by policy: {pattern}"
        return None


class ApprovalPolicy:
    """Map tool metadata and arguments to a human-facing risk decision."""

    def assess(
        self,
        tool_name: str,
        arguments: dict[str, object],
        spec: ToolSpec | None = None,
    ) -> ApprovalRequest:
        if tool_name == "execute_command":
            blocked = CommandGuard.reject_reason(str(arguments.get("command", "")))
            return ApprovalRequest(
                tool_name,
                arguments,
                RiskLevel.HIGH,
                blocked or "shell commands can modify the environment",
            )
        if tool_name.startswith("mcp__") and (
            spec is None or spec.risk is ToolRisk.UNKNOWN
        ):
            return ApprovalRequest(
                tool_name,
                arguments,
                RiskLevel.MEDIUM,
                "external MCP tools have undeclared side effects",
            )
        if spec is not None:
            if spec.risk is ToolRisk.HIGH:
                return ApprovalRequest(
                    tool_name,
                    arguments,
                    RiskLevel.HIGH,
                    "tool metadata declares high risk",
                )
            if spec.risk in {ToolRisk.MEDIUM, ToolRisk.UNKNOWN}:
                return ApprovalRequest(
                    tool_name,
                    arguments,
                    RiskLevel.MEDIUM,
                    "tool metadata declares medium or unknown risk",
                )
        return ApprovalRequest(
            tool_name,
            arguments,
            RiskLevel.SAFE,
            "read-only tool",
        )


class AuditLog:
    """Append one redacted JSON record per approval decision."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def record(
        self,
        request: ApprovalRequest,
        *,
        outcome: str,
        approver: str,
    ) -> None:
        from .observability import redact_text

        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / time.strftime("%Y-%m-%d.jsonl")
        event = {
            "timestamp": time.time(),
            **asdict(request),
            "risk": request.risk.value,
            "outcome": outcome,
            "approver": approver,
        }
        # Re-serialize through redact_text so nested arguments cannot leak a key.
        serialized = redact_text(json.dumps(event, ensure_ascii=False, default=str))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(serialized + "\n")


class HitlToolRegistry:
    """Transparent validated policy/approval layer around ``ToolRegistry``."""

    def __init__(
        self,
        registry: ToolRegistry,
        handler: ApprovalHandler,
        *,
        enabled: bool = True,
        policy: ApprovalPolicy | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        self.registry = registry
        self.handler = handler
        self.enabled = enabled
        self.policy = policy or ApprovalPolicy()
        self.audit_log = audit_log

    @property
    def project_root(self) -> Path:
        return self.registry.project_root

    def definitions(self) -> list[dict[str, object]]:
        return self.registry.definitions()

    def names(self) -> list[str]:
        return self.registry.names()

    def spec(self, name: str) -> ToolSpec | None:
        return self.registry.spec(name)

    def validate_arguments(self, name: str, arguments_json: str) -> dict[str, Any]:
        return self.registry.validate_arguments(name, arguments_json)

    def execute(self, name: str, arguments_json: str) -> str:
        return self.execute_result(name, arguments_json).content

    def execute_result(
        self,
        name: str,
        arguments_json: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        try:
            arguments = self.registry.validate_arguments(name, arguments_json)
        except Exception:
            # Preserve the base gateway's precise INVALID_ARGUMENTS/UNKNOWN_TOOL.
            return self.registry.execute_result(name, arguments_json)

        base_request = self.policy.assess(name, arguments, self.registry.spec(name))
        request = ApprovalRequest(
            base_request.tool_name,
            base_request.arguments,
            base_request.risk,
            base_request.reason,
            self._preview(name, arguments),
        )
        hard_denied = (
            request.risk is RiskLevel.HIGH
            and request.reason.startswith("command rejected by policy")
        )
        if hard_denied:
            self._audit(request, "deny", "policy")
            return ToolResult.failure(
                name,
                f"Tool denied: {request.reason}",
                ToolErrorType.POLICY_DENIED,
            )

        approved_arguments = arguments
        if self.enabled and request.risk is not RiskLevel.SAFE:
            result = self.handler(request)
            if result.decision is ApprovalDecision.DENY:
                self._audit(request, "deny", "hitl")
                return ToolResult.failure(
                    name,
                    "Tool denied by user",
                    ToolErrorType.APPROVAL_DENIED,
                )
            approved_arguments = result.arguments or arguments
            self._audit(request, "allow", "hitl")
        else:
            self._audit(request, "allow", "none")

        encoded = json.dumps(approved_arguments, ensure_ascii=False)
        # The approver may narrow/change arguments; the base gateway validates
        # them again. Preserve an explicit batch timeout when supplied.
        if timeout_seconds is None:
            return self.registry.execute_result(name, encoded)
        return self.registry.execute_many_results(
            [(name, encoded)],
            timeout_seconds=timeout_seconds,
        )[0]

    def execute_many(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> list[str]:
        return [
            result.content
            for result in self.execute_many_results(
                calls,
                timeout_seconds=timeout_seconds,
            )
        ]

    def execute_many_results(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> list[ToolResult]:
        # Approval prompts are a serialized UI state machine. The underlying
        # registry still handles conflict-aware parallelism when HITL is absent.
        return [
            self.execute_result(
                name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
            for name, arguments in calls
        ]

    def _preview(self, name: str, arguments: dict[str, object]) -> str:
        if name == "write_file":
            raw_path = str(arguments.get("path", ""))
            try:
                path = (self.project_root / raw_path).resolve()
                if not path.is_relative_to(self.project_root):
                    return "(path escapes project root)"
                before = (
                    path.read_text(encoding="utf-8", errors="replace")
                    if path.is_file()
                    else ""
                )
            except OSError as exc:
                return f"(could not read current file for diff: {exc})"
            after = str(arguments.get("content", ""))
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{raw_path}",
                    tofile=f"b/{raw_path}",
                )
            )
            return diff or "(write produces no textual change)"
        if name == "create_project":
            return (
                f"Create project directory: {arguments.get('name', '')}\n"
                f"Project type: {arguments.get('type', '')}"
            )
        if name == "execute_command":
            return (
                f"Working directory: {self.project_root}\n"
                f"Command: {arguments.get('command', '')}"
            )
        return json.dumps(arguments, ensure_ascii=False, indent=2, default=str)

    def _audit(self, request: ApprovalRequest, outcome: str, approver: str) -> None:
        if self.audit_log is not None:
            self.audit_log.record(request, outcome=outcome, approver=approver)


__all__ = [
    "ApprovalDecision",
    "ApprovalHandler",
    "ApprovalMode",
    "ApprovalPolicy",
    "ApprovalRequest",
    "ApprovalResult",
    "AuditLog",
    "CommandGuard",
    "ConsoleApprovalHandler",
    "HitlToolRegistry",
    "RiskLevel",
]
