"""Hard policy, human approval, diff previews, and durable audit records."""

from __future__ import annotations

import difflib
import json
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .command_policy import CommandGuard
from .permissions import PermissionAction, PermissionRule, PermissionStore
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
    remember: bool = False
    argument_patterns: dict[str, str] | None = None


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
            value = self.input_fn(
                "Allow this tool call? [y/N/a=always exact/p=pattern]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            self.output_fn("")
            return ApprovalResult(ApprovalDecision.DENY)
        if value in {"a", "always"}:
            return ApprovalResult(
                ApprovalDecision.APPROVE,
                remember=True,
                argument_patterns={
                    key: str(item) for key, item in request.arguments.items()
                },
            )
        if value in {"p", "pattern"}:
            primary = _primary_permission_argument(request)
            try:
                pattern = self.input_fn(
                    f"Persistent glob for {primary!r} (empty cancels): "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                self.output_fn("")
                return ApprovalResult(ApprovalDecision.DENY)
            if not pattern:
                return ApprovalResult(ApprovalDecision.DENY)
            return ApprovalResult(
                ApprovalDecision.APPROVE,
                remember=True,
                argument_patterns={primary: pattern},
            )
        return ApprovalResult(
            ApprovalDecision.APPROVE
            if value in {"y", "yes", "allow", "approve"}
            else ApprovalDecision.DENY
        )


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
        permission_store: PermissionStore | None = None,
    ) -> None:
        self.registry = registry
        self.handler = handler
        self.enabled = enabled
        self.policy = policy or ApprovalPolicy()
        self.audit_log = audit_log
        self.permission_store = permission_store

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
        authorized = self._authorize(name, arguments_json)
        if isinstance(authorized, ToolResult):
            return authorized
        if timeout_seconds is None:
            return self.registry.execute_result(name, authorized)
        return self.registry.execute_many_results(
            [(name, authorized)],
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
        """Serialize approval, then restore conflict-aware batch execution.

        Terminal prompts are a single-user state machine, so authorization is
        collected in input order.  All approved calls are then delegated in one
        batch; the underlying registry can again form read/read parallel waves
        while separating conflicting writes and process tools.
        """

        if not calls:
            return []
        results: list[ToolResult | None] = [None] * len(calls)
        delegated: list[tuple[str, str]] = []
        positions: list[int] = []
        for index, (name, arguments_json) in enumerate(calls):
            authorized = self._authorize(name, arguments_json)
            if isinstance(authorized, ToolResult):
                results[index] = authorized
                continue
            positions.append(index)
            delegated.append((name, authorized))
        if delegated:
            delegated_results = self.registry.execute_many_results(
                delegated,
                timeout_seconds=timeout_seconds,
            )
            for position, result in zip(positions, delegated_results, strict=True):
                results[position] = result
        return [
            result
            if result is not None
            else ToolResult.failure(
                calls[index][0],
                "Tool error: authorization produced no result",
                ToolErrorType.EXECUTION_ERROR,
            )
            for index, result in enumerate(results)
        ]

    def _authorize(self, name: str, arguments_json: str) -> str | ToolResult:
        try:
            arguments = self.registry.validate_arguments(name, arguments_json)
        except Exception:
            # Preserve precise INVALID_ARGUMENTS / UNKNOWN_TOOL classification.
            return self.registry.execute_result(name, arguments_json)

        base_request = self.policy.assess(name, arguments, self.registry.spec(name))
        request = ApprovalRequest(
            base_request.tool_name,
            base_request.arguments,
            base_request.risk,
            base_request.reason,
            self._preview(name, arguments),
        )
        if request.reason.startswith("command rejected by hard policy"):
            self._audit(request, "deny", "hard-policy")
            return ToolResult.failure(
                name,
                f"Tool denied: {request.reason}",
                ToolErrorType.POLICY_DENIED,
            )

        approved_arguments = arguments
        if self.permission_store is not None:
            permission, rule = self.permission_store.resolve(name, arguments)
            if permission is PermissionAction.DENY:
                self._audit(request, "deny", f"permission:{rule.id if rule else ''}")
                return ToolResult.failure(
                    name,
                    "Tool denied by persistent permission rule",
                    ToolErrorType.POLICY_DENIED,
                )
            if permission is PermissionAction.ALLOW:
                self._audit(request, "allow", f"permission:{rule.id if rule else ''}")
                return json.dumps(arguments, ensure_ascii=False)

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
            if result.remember and self.permission_store is not None:
                patterns = result.argument_patterns or {
                    key: str(value) for key, value in approved_arguments.items()
                }
                self.permission_store.add(
                    PermissionRule.create(
                        name,
                        PermissionAction.ALLOW,
                        patterns,
                        description="remembered from HITL approval",
                    )
                )
            self._audit(request, "allow", "hitl")
        else:
            self._audit(request, "allow", "none")
        return json.dumps(approved_arguments, ensure_ascii=False)

    def _preview(self, name: str, arguments: dict[str, object]) -> str:
        spec = self.registry.spec(name)
        if spec is not None and spec.previewer is not None:
            try:
                return spec.previewer(dict(arguments))
            except Exception as exc:
                return f"(could not build preview: {type(exc).__name__}: {exc})"
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


def _primary_permission_argument(request: ApprovalRequest) -> str:
    preferred = {
        "execute_command": "command",
        "write_file": "path",
        "replace_text": "path",
        "apply_patch": "patch",
        "multi_edit": "edits",
        "create_project": "name",
        "web_fetch": "url",
        "web_search": "query",
    }.get(request.tool_name)
    if preferred and preferred in request.arguments:
        return preferred
    return next(iter(request.arguments), "*")


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
    "PermissionAction",
    "PermissionRule",
    "PermissionStore",
    "RiskLevel",
]
