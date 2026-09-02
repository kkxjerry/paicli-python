from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from paicli.permissions import PermissionAction, PermissionRule, PermissionStore
from paicli.policy import (
    ApprovalDecision,
    ApprovalResult,
    HitlToolRegistry,
)
from paicli.tool_contracts import (
    ConcurrencyPolicy,
    ToolErrorType,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)
from paicli.tools import ToolRegistry


class HardeningToolsTest(unittest.TestCase):
    def test_base_registry_blocks_destructive_shell_without_hitl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory, allow_shell=True)
            result = registry.execute_result(
                "execute_command",
                json.dumps({"command": "sudo echo should-not-run"}),
            )

            self.assertFalse(result.ok)
            self.assertIs(ToolErrorType.POLICY_DENIED, result.error_type)
            self.assertIn("hard policy", result.content)

    def test_read_file_supports_ranges_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
            registry = ToolRegistry(root)

            result = registry.execute(
                "read_file",
                json.dumps(
                    {
                        "path": "sample.txt",
                        "start_line": 2,
                        "end_line": 3,
                        "include_sha256": True,
                    }
                ),
            )

            first, rest = result.split("\n", 1)
            self.assertRegex(first, r"^SHA256: [0-9a-f]{64}$")
            self.assertEqual("two\nthree\n", rest)

    def test_replace_text_checks_hash_and_emits_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            registry = ToolRegistry(root)
            read = registry.execute(
                "read_file",
                json.dumps({"path": "app.py", "include_sha256": True}),
            )
            digest = read.splitlines()[0].split(": ", 1)[1]

            result = registry.execute_result(
                "replace_text",
                json.dumps(
                    {
                        "path": "app.py",
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                        "expected_sha256": digest,
                    }
                ),
            )

            self.assertTrue(result.ok)
            self.assertEqual(("app.py",), result.changed_files)
            self.assertEqual("VALUE = 2\n", target.read_text(encoding="utf-8"))
            stale = registry.execute_result(
                "replace_text",
                json.dumps(
                    {
                        "path": "app.py",
                        "old_text": "VALUE = 2",
                        "new_text": "VALUE = 3",
                        "expected_sha256": digest,
                    }
                ),
            )
            self.assertFalse(stale.ok)
            self.assertIs(ToolErrorType.INVALID_ARGUMENTS, stale.error_type)
            self.assertEqual("VALUE = 2\n", target.read_text(encoding="utf-8"))

    def test_multi_edit_validates_every_file_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "one.txt"
            two = root / "two.txt"
            one.write_text("old one", encoding="utf-8")
            two.write_text("old two", encoding="utf-8")
            registry = ToolRegistry(root)

            result = registry.execute_result(
                "multi_edit",
                json.dumps(
                    {
                        "edits": [
                            {
                                "path": "one.txt",
                                "old_text": "old one",
                                "new_text": "new one",
                            },
                            {
                                "path": "two.txt",
                                "old_text": "missing",
                                "new_text": "new two",
                            },
                        ]
                    }
                ),
            )

            self.assertFalse(result.ok)
            self.assertEqual("old one", one.read_text(encoding="utf-8"))
            self.assertEqual("old two", two.read_text(encoding="utf-8"))

    def test_apply_patch_updates_multiple_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
            (root / "b.txt").write_text("alpha\n", encoding="utf-8")
            registry = ToolRegistry(root)
            patch = """--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 one
-two
+three
--- a/b.txt
+++ b/b.txt
@@ -1 +1 @@
-alpha
+beta
"""

            result = registry.execute_result(
                "apply_patch",
                json.dumps({"patch": patch}),
            )

            self.assertTrue(result.ok, result.content)
            self.assertEqual({"a.txt", "b.txt"}, set(result.changed_files))
            self.assertEqual("one\nthree\n", (root / "a.txt").read_text())
            self.assertEqual("beta\n", (root / "b.txt").read_text())

    def test_grep_and_glob_do_not_require_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text(
                "def target_function():\n    return 1\n",
                encoding="utf-8",
            )
            registry = ToolRegistry(root, allow_shell=False)

            grep_result = registry.execute(
                "grep",
                json.dumps({"pattern": "target_function", "file_glob": "*.py"}),
            )
            glob_result = registry.execute(
                "glob",
                json.dumps({"pattern": "**/*.py"}),
            )

            self.assertIn("src/app.py:1", grep_result)
            self.assertIn("src/app.py", glob_result)

    def test_persistent_permission_bypasses_second_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PermissionStore(root / ".paicli" / "permissions.json")
            approvals = []

            def approve(request):
                approvals.append(request)
                return ApprovalResult(
                    ApprovalDecision.APPROVE,
                    remember=True,
                    argument_patterns={"path": "generated/*.txt"},
                )

            guarded = HitlToolRegistry(
                ToolRegistry(root),
                approve,
                permission_store=store,
            )
            first = guarded.execute_result(
                "write_file",
                json.dumps({"path": "generated/a.txt", "content": "a"}),
            )
            second = guarded.execute_result(
                "write_file",
                json.dumps({"path": "generated/b.txt", "content": "b"}),
            )

            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            self.assertEqual(1, len(approvals))
            self.assertEqual(1, len(store.rules))
            if os.name == "posix":
                self.assertEqual(0o600, store.path.stat().st_mode & 0o777)

    def test_persistent_deny_rule_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PermissionStore(root / "permissions.json")
            store.add(
                PermissionRule.create(
                    "write_file",
                    PermissionAction.DENY,
                    {"path": "secrets/*"},
                )
            )
            guarded = HitlToolRegistry(
                ToolRegistry(root),
                lambda _request: ApprovalResult(ApprovalDecision.APPROVE),
                permission_store=store,
            )

            result = guarded.execute_result(
                "write_file",
                json.dumps({"path": "secrets/key.txt", "content": "blocked"}),
            )

            self.assertFalse(result.ok)
            self.assertIs(ToolErrorType.POLICY_DENIED, result.error_type)
            self.assertFalse((root / "secrets" / "key.txt").exists())

    def test_hitl_collects_approvals_then_delegates_parallel_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            barrier = threading.Barrier(2)

            def make_handler(name: str):
                def handler(_arguments):
                    barrier.wait(timeout=1)
                    time.sleep(0.03)
                    return name

                return handler

            for name in ("remote_a", "remote_b"):
                registry.register(
                    ToolSpec(
                        name,
                        "External read requiring approval",
                        registry.object_schema({}),
                        make_handler(name),
                        risk=ToolRisk.MEDIUM,
                        side_effect=ToolSideEffect.READ_ONLY,
                        concurrency=ConcurrencyPolicy.PARALLEL,
                    )
                )
            approvals = []
            guarded = HitlToolRegistry(
                registry,
                lambda request: approvals.append(request)
                or ApprovalResult(ApprovalDecision.APPROVE),
            )

            results = guarded.execute_many_results(
                [("remote_a", "{}"), ("remote_b", "{}")]
            )

            self.assertTrue(all(result.ok for result in results))
            self.assertEqual(2, len(approvals))


if __name__ == "__main__":
    unittest.main()
