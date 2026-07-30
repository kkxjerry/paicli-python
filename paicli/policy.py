"""Phase 6: Human-in-the-Loop approval, policy guards, and audit."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .tools import ToolRegistry


class RiskLevel(str, Enum):
    SAFE = "safe"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    arguments: dict[str, object]
    risk: RiskLevel
    reason: str


@dataclass(frozen=True)
class ApprovalResult:
    decision: ApprovalDecision
    arguments: dict[str, object] | None = None


ApprovalHandler = Callable[[ApprovalRequest], ApprovalResult]


class CommandGuard:
    BLOCKED_PATTERNS = (
        r"\bsudo\b",
        r"\brm\s+-[a-z]*r[a-z]*f\b",
        r"\bmkfs(\.\w+)?\b",
        r"\bdd\b.*\bof=/dev/",
        r":\(\)\s*\{\s*:\|:&\s*\};:",
        r"\b(?:curl|wget)\b[^|]*\|\s*(?:sh|bash)\b",
        r"\b(?:shutdown|reboot|poweroff)\b",
    )

    @classmethod
    def reject_reason(cls, command: str) -> str | None:
        for pattern in cls.BLOCKED_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                return f"command rejected by policy: {pattern}"
        return None


class ApprovalPolicy:
    def assess(self, tool_name: str, arguments: dict[str, object]) -> ApprovalRequest:
        if tool_name == "execute_command":
            command = str(arguments.get("command", ""))
            blocked = CommandGuard.reject_reason(command)
            if blocked:
                return ApprovalRequest(tool_name, arguments, RiskLevel.HIGH, blocked)
            return ApprovalRequest(
                tool_name,
                arguments,
                RiskLevel.HIGH,
                "shell commands can modify the environment",
            )
        if tool_name in {"write_file", "create_project"}:
            return ApprovalRequest(
                tool_name,
                arguments,
                RiskLevel.MEDIUM,
                "tool writes to the project",
            )
        return ApprovalRequest(tool_name, arguments, RiskLevel.SAFE, "read-only tool")


class AuditLog:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def record(
        self,
        request: ApprovalRequest,
        *,
        outcome: str,
        approver: str,
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / time.strftime("%Y-%m-%d.jsonl")
        event = {
            "timestamp": time.time(),
            **asdict(request),
            "risk": request.risk.value,
            "outcome": outcome,
            "approver": approver,
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")


class HitlToolRegistry:
    """Transparent approval layer around an existing ToolRegistry."""

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

    def definitions(self) -> list[dict[str, object]]:
        return self.registry.definitions()

    def names(self) -> list[str]:
        return self.registry.names()

    def execute(self, name: str, arguments_json: str) -> str:
        try:
            arguments = json.loads(arguments_json or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
        except (json.JSONDecodeError, ValueError) as exc:
            return f"Tool error: {exc}"

        request = self.policy.assess(name, arguments)
        blocked = (
            request.risk is RiskLevel.HIGH
            and request.reason.startswith("command rejected")
        )
        if blocked:
            self._audit(request, "deny", "policy")
            return f"Tool denied: {request.reason}"

        if self.enabled and request.risk is not RiskLevel.SAFE:
            result = self.handler(request)
            if result.decision is ApprovalDecision.DENY:
                self._audit(request, "deny", "hitl")
                return "Tool denied by user"
            arguments = result.arguments or arguments
            self._audit(request, "allow", "hitl")
        else:
            self._audit(request, "allow", "none")
        return self.registry.execute(name, json.dumps(arguments, ensure_ascii=False))

    def _audit(self, request: ApprovalRequest, outcome: str, approver: str) -> None:
        if self.audit_log:
            self.audit_log.record(request, outcome=outcome, approver=approver)
