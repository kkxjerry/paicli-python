from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paicli.policy import (
    ApprovalDecision,
    ApprovalMode,
    ApprovalRequest,
    ApprovalResult,
    AuditLog,
    ConsoleApprovalHandler,
    HitlToolRegistry,
    PermissionStore,
    RiskLevel,
)
from paicli.tools import ToolRegistry


class PolicyTest(unittest.TestCase):
    def test_write_requires_approval(self) -> None:
        """验证 write_file 会先进入 HITL，获得允许后才真正写文件。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：calls 记录审批器收到的请求，模拟用户点击允许。
            calls = []
            guarded = HitlToolRegistry(
                ToolRegistry(directory),
                lambda request: calls.append(request)
                or ApprovalResult(ApprovalDecision.APPROVE),
            )

            # Act：从审批包装层执行写文件。
            result = guarded.execute(
                "write_file",
                '{"path":"a.txt","content":"ok"}',
            )

            # Assert：文件写入成功，且审批器确实收到 write_file。
            self.assertEqual("Wrote a.txt", result)
            self.assertEqual("write_file", calls[0].tool_name)

    def test_write_approval_contains_unified_diff_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "a.txt").write_text("old\n", encoding="utf-8")
            requests = []
            guarded = HitlToolRegistry(
                ToolRegistry(directory),
                lambda request: requests.append(request)
                or ApprovalResult(ApprovalDecision.DENY),
            )

            guarded.execute(
                "write_file",
                '{"path":"a.txt","content":"new\\n"}',
            )

            self.assertIn("--- a/a.txt", requests[0].preview)
            self.assertIn("+++ b/a.txt", requests[0].preview)
            self.assertIn("-old", requests[0].preview)
            self.assertIn("+new", requests[0].preview)
            self.assertEqual("old\n", Path(directory, "a.txt").read_text())

    def test_non_interactive_approval_modes_are_explicit(self) -> None:
        request = ApprovalRequest(
            "write_file",
            {"path": "a.txt"},
            RiskLevel.MEDIUM,
            "writes",
            "preview",
        )

        self.assertIs(
            ApprovalDecision.APPROVE,
            ConsoleApprovalHandler(ApprovalMode.ALLOW)(request).decision,
        )
        self.assertIs(
            ApprovalDecision.DENY,
            ConsoleApprovalHandler(ApprovalMode.DENY)(request).decision,
        )

    def test_policy_blocks_destructive_command_before_hitl(self) -> None:
        """验证毁灭性命令在进入人工审批之前就被硬策略拦截。"""

        with tempfile.TemporaryDirectory() as directory:
            approvals = []
            guarded = HitlToolRegistry(
                ToolRegistry(directory, allow_shell=True),
                lambda request: approvals.append(request)
                or ApprovalResult(ApprovalDecision.APPROVE),
            )

            # Act：即使 Shell 已开启、模拟审批器会允许，该命令仍应被拒绝。
            result = guarded.execute(
                "execute_command",
                '{"command":"sudo rm -rf /"}',
            )

            # Assert：返回拒绝，并且 approvals 仍为空，说明人工审批没被调用。
            self.assertIn("denied", result)
            self.assertEqual([], approvals)

    def test_always_exact_does_not_turn_literal_glob_chars_into_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PermissionStore(root / ".paicli" / "permissions.json")
            answers = iter(["a", "n"])
            prompts: list[str] = []
            handler = ConsoleApprovalHandler(
                input_fn=lambda prompt: prompts.append(prompt) or next(answers),
                output_fn=lambda _text: None,
            )
            guarded = HitlToolRegistry(
                ToolRegistry(root),
                handler,
                permission_store=store,
            )
            literal_call = json.dumps(
                {"path": "literal[ab]*?.txt", "content": "same"}
            )

            first = guarded.execute_result("write_file", literal_call)
            second = guarded.execute_result("write_file", literal_call)
            broadened = guarded.execute_result(
                "write_file",
                json.dumps({"path": "literalaANYq.txt", "content": "same"}),
            )

            self.assertTrue(first.ok)
            # The second exact call bypasses HITL; write_file itself reports a
            # deterministic no-op because the content is unchanged.
            self.assertNotIn("denied", second.content.lower())
            self.assertFalse(broadened.ok)
            self.assertEqual(2, len(prompts))
            self.assertFalse((root / "literalaANYq.txt").exists())

    def test_denial_is_written_to_audit_log(self) -> None:
        """验证用户拒绝工具后，结果和审批来源会持久化到审计日志。"""

        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory, "audit")
            guarded = HitlToolRegistry(
                ToolRegistry(directory),
                lambda _request: ApprovalResult(ApprovalDecision.DENY),
                audit_log=AuditLog(audit_dir),
            )

            # Act：审批器固定返回 DENY。
            guarded.execute("write_file", '{"path":"a","content":"b"}')

            # Assert：读取当天 JSONL 的唯一事件，确认是 HITL 拒绝。
            event = json.loads(next(audit_dir.iterdir()).read_text(encoding="utf-8"))
            self.assertEqual("deny", event["outcome"])
            self.assertEqual("hitl", event["approver"])

    def test_audit_log_recursively_redacts_nested_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory, "audit")
            AuditLog(audit_dir).record(
                ApprovalRequest(
                    "mcp__remote__call",
                    {
                        "config": {
                            "api_key": "nested-secret",
                            "authorization": "Bearer private-token",
                        }
                    },
                    RiskLevel.MEDIUM,
                    "external tool",
                ),
                outcome="deny",
                approver="hitl",
            )

            raw = next(audit_dir.iterdir()).read_text(encoding="utf-8")
            event = json.loads(raw)
            self.assertNotIn("nested-secret", raw)
            self.assertNotIn("private-token", raw)
            self.assertEqual("[REDACTED]", event["arguments"]["config"]["api_key"])
            self.assertEqual(
                "[REDACTED]",
                event["arguments"]["config"]["authorization"],
            )


if __name__ == "__main__":
    unittest.main()
