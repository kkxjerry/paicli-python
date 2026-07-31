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
from typing import Callable

from .tools import ToolRegistry


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
    """根据工具类型和参数生成风险评估。"""

    def assess(self, tool_name: str, arguments: dict[str, object]) -> ApprovalRequest:
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

    def execute(self, name: str, arguments_json: str) -> str:
        # 先在审批层解析参数，因为策略判断和用户审批都需要查看它。
        try:
            arguments = json.loads(arguments_json or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
        except (json.JSONDecodeError, ValueError) as exc:
            return f"Tool error: {exc}"

        request = self.policy.assess(name, arguments)
        # 本期用 HIGH + reason 前缀区分“必须拒绝”与“可人工审批”的 Shell。
        blocked = (
            request.risk is RiskLevel.HIGH
            and request.reason.startswith("command rejected")
        )
        if blocked:
            # 硬策略优先级高于人工审批，因此不会调用 handler。
            self._audit(request, "deny", "policy")
            return f"Tool denied: {request.reason}"

        if self.enabled and request.risk is not RiskLevel.SAFE:
            result = self.handler(request)
            if result.decision is ApprovalDecision.DENY:
                self._audit(request, "deny", "hitl")
                return "Tool denied by user"
            # 审批器可以在允许的同时缩小参数范围；未提供则使用原参数。
            arguments = result.arguments or arguments
            self._audit(request, "allow", "hitl")
        else:
            self._audit(request, "allow", "none")
        # 只有通过策略和 HITL 后，请求才会到达真正的 ToolRegistry。
        return self.registry.execute(name, json.dumps(arguments, ensure_ascii=False))

    def _audit(self, request: ApprovalRequest, outcome: str, approver: str) -> None:
        if self.audit_log:
            self.audit_log.record(request, outcome=outcome, approver=approver)
