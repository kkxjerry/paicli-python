"""Phase 6：HITL 人工审批、安全策略与审计日志。

    工具请求 -> ApprovalPolicy 评估风险
        |                        |
        | 命中禁止规则       | 中/高风险
        v                        v
    直接拒绝                 请求用户审批
                                 |
                          允许后执行工具

所有决策可选写入 JSONL 审计日志。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .tool_contracts import ToolErrorType, ToolResult, ToolRisk
from .tools import ToolRegistry, ToolSpec


class RiskLevel(str, Enum):
    """工具副作用等级。"""

    SAFE = "safe"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"


@dataclass(frozen=True)
class ApprovalRequest:
    """交给审批器的完整上下文。"""

    tool_name: str
    arguments: dict[str, object]
    risk: RiskLevel
    reason: str


@dataclass(frozen=True)
class ApprovalResult:
    """审批结果；arguments 可用于在允许前修改参数。"""

    decision: ApprovalDecision
    arguments: dict[str, object] | None = None


ApprovalHandler = Callable[[ApprovalRequest], ApprovalResult]


class CommandGuard:
    """使用正则表达式拦截明显高危命令。"""

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
        # 命中第一条禁止模式就结束；None 表示没命中，不代表命令绝对安全。
        for pattern in cls.BLOCKED_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                return f"command rejected by policy: {pattern}"
        return None


class ApprovalPolicy:
    """根据工具类型、参数和 ToolSpec 元数据生成风险评估。"""

    def assess(
        self,
        tool_name: str,
        arguments: dict[str, object],
        spec: ToolSpec | None = None,
    ) -> ApprovalRequest:
        if tool_name == "execute_command":
            command = str(arguments.get("command", ""))
            blocked = CommandGuard.reject_reason(command)
            if blocked:
                # 禁止模式仍使用 HIGH，但 reason 会标记为 policy rejection。
                return ApprovalRequest(tool_name, arguments, RiskLevel.HIGH, blocked)
            return ApprovalRequest(
                tool_name,
                arguments,
                RiskLevel.HIGH,
                "shell commands can modify the environment",
            )
        if tool_name in {"write_file", "create_project"}:
            # 写磁盘需人工审批，但不像 Shell 一样直接定为高风险。
            return ApprovalRequest(
                tool_name,
                arguments,
                RiskLevel.MEDIUM,
                "tool writes to the project",
            )
        if tool_name.startswith("mcp__"):
            return ApprovalRequest(
                tool_name,
                arguments,
                RiskLevel.MEDIUM,
                "external MCP tools have undeclared side effects",
            )
        if spec is not None and spec.risk in {ToolRisk.MEDIUM, ToolRisk.UNKNOWN}:
            return ApprovalRequest(
                tool_name,
                arguments,
                RiskLevel.MEDIUM,
                "tool metadata declares medium or unknown risk",
            )
        if spec is not None and spec.risk is ToolRisk.HIGH:
            return ApprovalRequest(
                tool_name,
                arguments,
                RiskLevel.HIGH,
                "tool metadata declares high risk",
            )
        return ApprovalRequest(tool_name, arguments, RiskLevel.SAFE, "read-only tool")


class AuditLog:
    """按日期追加 JSONL 审计事件。"""

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
        # 每天一个文件，每个审批事件占一行。
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
    """包装现有 ToolRegistry 的透明审批层。"""

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
        # 对 Agent 保持与 ToolRegistry 相同的外观，模型不需知道中间多了审批层。
        return self.registry.definitions()

    def names(self) -> list[str]:
        return self.registry.names()

    def spec(self, name: str) -> ToolSpec | None:
        return self.registry.spec(name)

    def validate_arguments(self, name: str, arguments_json: str) -> dict[str, Any]:
        return self.registry.validate_arguments(name, arguments_json)

    def execute(self, name: str, arguments_json: str) -> str:
        return self.execute_result(name, arguments_json).content

    def execute_result(self, name: str, arguments_json: str) -> ToolResult:
        return self._execute_result(name, arguments_json, timeout_seconds=None)

    def _execute_result(
        self,
        name: str,
        arguments_json: str,
        *,
        timeout_seconds: float | None,
    ) -> ToolResult:
        # 运行时 Schema 校验必须先于策略和人工审批，避免让用户审批一个
        # 缺字段、类型错误或带多余字段的请求。
        try:
            arguments = self.registry.validate_arguments(name, arguments_json)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return self.registry.execute_many_results(
                [(name, arguments_json)],
                timeout_seconds=timeout_seconds,
            )[0]

        request = self.policy.assess(name, arguments, self.registry.spec(name))
        blocked = (
            request.risk is RiskLevel.HIGH
            and request.reason.startswith("command rejected")
        )
        if blocked:
            self._audit(request, "deny", "policy")
            return ToolResult.failure(
                name,
                f"Tool denied: {request.reason}",
                ToolErrorType.POLICY_DENIED,
            )

        if self.enabled and request.risk is not RiskLevel.SAFE:
            result = self.handler(request)
            if result.decision is ApprovalDecision.DENY:
                self._audit(request, "deny", "hitl")
                return ToolResult.failure(
                    name,
                    "Tool denied by user",
                    ToolErrorType.APPROVAL_DENIED,
                )
            arguments = result.arguments or arguments
            self._audit(request, "allow", "hitl")
        else:
            self._audit(request, "allow", "none")

        # 审批器可能修改参数；重新序列化并再次经过底层 Schema 校验。
        return self.registry.execute_many_results(
            [(name, json.dumps(arguments, ensure_ascii=False))],
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
        # 审批 UI 是交互式状态机，多个弹窗不能在线程中并发竞争终端。
        return [
            self._execute_result(
                name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
            for name, arguments in calls
        ]

    def _audit(self, request: ApprovalRequest, outcome: str, approver: str) -> None:
        if self.audit_log:
            self.audit_log.record(request, outcome=outcome, approver=approver)
