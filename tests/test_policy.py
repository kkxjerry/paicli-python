from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paicli.policy import (
    ApprovalDecision,
    ApprovalResult,
    AuditLog,
    HitlToolRegistry,
)
from paicli.tools import ToolRegistry


class PolicyTest(unittest.TestCase):
    def test_write_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            guarded = HitlToolRegistry(
                ToolRegistry(directory),
                lambda request: calls.append(request)
                or ApprovalResult(ApprovalDecision.APPROVE),
            )

            result = guarded.execute(
                "write_file",
                '{"path":"a.txt","content":"ok"}',
            )

            self.assertEqual("Wrote a.txt", result)
            self.assertEqual("write_file", calls[0].tool_name)

    def test_policy_blocks_destructive_command_before_hitl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            approvals = []
            guarded = HitlToolRegistry(
                ToolRegistry(directory, allow_shell=True),
                lambda request: approvals.append(request)
                or ApprovalResult(ApprovalDecision.APPROVE),
            )

            result = guarded.execute(
                "execute_command",
                '{"command":"sudo rm -rf /"}',
            )

            self.assertIn("denied", result)
            self.assertEqual([], approvals)

    def test_denial_is_written_to_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory, "audit")
            guarded = HitlToolRegistry(
                ToolRegistry(directory),
                lambda _request: ApprovalResult(ApprovalDecision.DENY),
                audit_log=AuditLog(audit_dir),
            )

            guarded.execute("write_file", '{"path":"a","content":"b"}')

            event = json.loads(next(audit_dir.iterdir()).read_text(encoding="utf-8"))
            self.assertEqual("deny", event["outcome"])
            self.assertEqual("hitl", event["approver"])


if __name__ == "__main__":
    unittest.main()
