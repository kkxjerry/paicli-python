from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest

from paicli.policy import ApprovalDecision, ApprovalResult, HitlToolRegistry
from paicli.tools import (
    ConcurrencyPolicy,
    ResourceAccess,
    ResourceMode,
    ToolErrorType,
    ToolRegistry,
    ToolResult,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)


class ToolGatewayTest(unittest.TestCase):
    def test_extension_metadata_fails_closed_by_default(self) -> None:
        spec = ToolSpec(
            "extension",
            "Extension without explicit metadata.",
            {"type": "object"},
            lambda _arguments: "ok",
        )

        self.assertEqual(ToolRisk.UNKNOWN, spec.risk)
        self.assertEqual(ToolSideEffect.UNKNOWN, spec.side_effect)
        self.assertEqual(ConcurrencyPolicy.SERIAL, spec.concurrency)

    def test_runtime_schema_rejects_missing_extra_and_wrong_typed_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            calls: list[dict[str, object]] = []
            registry.register(
                ToolSpec(
                    "strict_tool",
                    "Strict schema test.",
                    registry.object_schema(
                        {
                            "count": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 3,
                            },
                            "labels": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        required=["count"],
                    ),
                    lambda arguments: calls.append(arguments) or "ok",
                )
            )

            missing = registry.execute_result("strict_tool", "{}")
            extra = registry.execute_result(
                "strict_tool", '{"count":1,"unexpected":true}'
            )
            wrong_type = registry.execute_result(
                "strict_tool", '{"count":"1"}'
            )
            out_of_range = registry.execute_result(
                "strict_tool", '{"count":4}'
            )
            nested_wrong_type = registry.execute_result(
                "strict_tool", '{"count":1,"labels":["ok",2]}'
            )

            for result in (
                missing,
                extra,
                wrong_type,
                out_of_range,
                nested_wrong_type,
            ):
                self.assertFalse(result.ok)
                self.assertEqual(ToolErrorType.INVALID_ARGUMENTS, result.error_type)
                self.assertTrue(result.retryable)
            self.assertEqual([], calls, "invalid arguments must not reach the handler")

    def test_validated_handler_receives_native_json_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            received: list[dict[str, object]] = []
            registry.register(
                ToolSpec(
                    "typed_tool",
                    "Native type test.",
                    registry.object_schema(
                        {
                            "enabled": {"type": "boolean"},
                            "count": {"type": "integer"},
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        required=["enabled", "count", "tags"],
                    ),
                    lambda arguments: received.append(arguments) or "ok",
                )
            )

            result = registry.execute_result(
                "typed_tool",
                '{"enabled":true,"count":2,"tags":["a","b"]}',
            )

            self.assertTrue(result.ok)
            self.assertIs(received[0]["enabled"], True)
            self.assertEqual(2, received[0]["count"])
            self.assertEqual(["a", "b"], received[0]["tags"])

    def test_same_resource_writes_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            lock = threading.Lock()
            active = 0
            peak = 0

            def write(arguments: dict[str, object]) -> str:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return str(arguments["value"])

            registry.register(
                ToolSpec(
                    "locked_write",
                    "Write one logical resource.",
                    registry.object_schema(
                        {
                            "path": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        required=["path", "value"],
                    ),
                    write,
                    risk=ToolRisk.MEDIUM,
                    side_effect=ToolSideEffect.FILE_WRITE,
                    concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                    resource_resolver=lambda args: (
                        ResourceAccess(str(args["path"]), ResourceMode.WRITE),
                    ),
                )
            )

            results = registry.execute_many_results(
                [
                    ("locked_write", '{"path":"same.py","value":"first"}'),
                    ("locked_write", '{"path":"same.py","value":"second"}'),
                ]
            )

            self.assertEqual(1, peak)
            self.assertEqual(["first", "second"], [item.content for item in results])
            self.assertEqual(("same.py",), results[0].changed_files)

    def test_independent_resource_writes_can_run_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            barrier = threading.Barrier(2)

            def write(arguments: dict[str, object]) -> str:
                barrier.wait(timeout=1)
                return str(arguments["path"])

            registry.register(
                ToolSpec(
                    "independent_write",
                    "Write independent resources.",
                    registry.object_schema(
                        {"path": {"type": "string"}},
                        required=["path"],
                    ),
                    write,
                    side_effect=ToolSideEffect.FILE_WRITE,
                    concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                    resource_resolver=lambda args: (
                        ResourceAccess(str(args["path"]), ResourceMode.WRITE),
                    ),
                )
            )

            results = registry.execute_many_results(
                [
                    ("independent_write", '{"path":"a.py"}'),
                    ("independent_write", '{"path":"b.py"}'),
                ]
            )

            self.assertEqual(["a.py", "b.py"], [item.content for item in results])

    def test_tool_timeout_applies_to_the_single_call_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            registry.register(
                ToolSpec(
                    "slow_read",
                    "Slow read.",
                    registry.object_schema({}),
                    lambda _args: time.sleep(0.08) or "late",
                    risk=ToolRisk.SAFE,
                    side_effect=ToolSideEffect.READ_ONLY,
                    concurrency=ConcurrencyPolicy.PARALLEL,
                    timeout_seconds=0.01,
                )
            )

            result = registry.execute_result("slow_read", "{}")

            self.assertFalse(result.ok)
            self.assertTrue(result.timed_out)
            self.assertTrue(result.retryable)
            self.assertEqual(ToolErrorType.TIMEOUT, result.error_type)

    def test_parallel_calls_use_independent_submission_deadlines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            registry.register(
                ToolSpec(
                    "long_deadline_read",
                    "Keeps result collection busy while another call expires.",
                    registry.object_schema({}),
                    lambda _args: time.sleep(0.1) or "read complete",
                    risk=ToolRisk.SAFE,
                    side_effect=ToolSideEffect.READ_ONLY,
                    concurrency=ConcurrencyPolicy.PARALLEL,
                    timeout_seconds=1.0,
                )
            )
            registry.register(
                ToolSpec(
                    "short_deadline_write",
                    "Completes after its own deadline.",
                    registry.object_schema(
                        {"path": {"type": "string"}}, required=["path"]
                    ),
                    lambda _args: time.sleep(0.04) or "write complete",
                    risk=ToolRisk.MEDIUM,
                    side_effect=ToolSideEffect.FILE_WRITE,
                    concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                    timeout_seconds=0.02,
                    resource_resolver=lambda args: (
                        ResourceAccess(str(args["path"]), ResourceMode.WRITE),
                    ),
                )
            )

            results = registry.execute_many_results(
                [
                    ("long_deadline_read", "{}"),
                    ("short_deadline_write", '{"path":"late.py"}'),
                ]
            )

            self.assertTrue(results[0].ok)
            self.assertFalse(results[1].ok)
            self.assertTrue(results[1].timed_out)
            self.assertFalse(results[1].retryable)
            self.assertEqual(("late.py",), results[1].changed_files)
            self.assertIn("completed after its deadline", results[1].content)

    def test_timed_out_side_effect_quarantines_conflicting_resource(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            finished = threading.Event()
            quick_calls: list[str] = []

            def slow_write(_arguments: dict[str, object]) -> str:
                time.sleep(0.08)
                finished.set()
                return "late write finished"

            access = lambda args: (
                ResourceAccess(str(args["path"]), ResourceMode.WRITE),
            )
            schema = registry.object_schema(
                {"path": {"type": "string"}}, required=["path"]
            )
            registry.register(
                ToolSpec(
                    "slow_write",
                    "A timed-out write.",
                    schema,
                    slow_write,
                    risk=ToolRisk.MEDIUM,
                    side_effect=ToolSideEffect.FILE_WRITE,
                    concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                    timeout_seconds=0.01,
                    resource_resolver=access,
                )
            )
            registry.register(
                ToolSpec(
                    "quick_write",
                    "A later write to the same resource.",
                    schema,
                    lambda args: quick_calls.append(str(args["path"])) or "done",
                    risk=ToolRisk.MEDIUM,
                    side_effect=ToolSideEffect.FILE_WRITE,
                    concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                    resource_resolver=access,
                )
            )

            timed_out, blocked = registry.execute_many_results(
                [
                    ("slow_write", '{"path":"shared.py"}'),
                    ("quick_write", '{"path":"shared.py"}'),
                ]
            )

            self.assertTrue(timed_out.timed_out)
            self.assertFalse(blocked.ok)
            self.assertEqual(
                ToolErrorType.RESOURCE_CONFLICT,
                blocked.error_type,
            )
            self.assertEqual([], quick_calls)

            self.assertTrue(finished.wait(timeout=1))
            time.sleep(0.01)  # allow the Future to publish its done state
            resumed = registry.execute_result(
                "quick_write", '{"path":"shared.py"}'
            )
            self.assertTrue(resumed.ok)
            self.assertEqual(["shared.py"], quick_calls)

    def test_recursive_directory_claim_conflicts_with_child_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            order: list[str] = []
            registry.register(
                ToolSpec(
                    "directory_read",
                    "Read a directory tree.",
                    registry.object_schema(
                        {"path": {"type": "string"}}, required=["path"]
                    ),
                    lambda _args: order.append("read") or "read",
                    concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                    resource_resolver=lambda args: (
                        ResourceAccess(
                            str(args["path"]),
                            ResourceMode.READ,
                            recursive=True,
                        ),
                    ),
                )
            )
            registry.register(
                ToolSpec(
                    "child_write",
                    "Write a child.",
                    registry.object_schema(
                        {"path": {"type": "string"}}, required=["path"]
                    ),
                    lambda _args: order.append("write") or "write",
                    side_effect=ToolSideEffect.FILE_WRITE,
                    concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                    resource_resolver=lambda args: (
                        ResourceAccess(str(args["path"]), ResourceMode.WRITE),
                    ),
                )
            )

            registry.execute_many_results(
                [
                    ("directory_read", '{"path":"src"}'),
                    ("child_write", '{"path":"src/app.py"}'),
                ]
            )

            self.assertEqual(["read", "write"], order)

    def test_hitl_does_not_prompt_for_invalid_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            approvals = []
            guarded = HitlToolRegistry(
                ToolRegistry(directory),
                lambda request: approvals.append(request)
                or ApprovalResult(ApprovalDecision.APPROVE),
            )

            result = guarded.execute_result(
                "write_file",
                '{"path":"a.txt","content":"x","extra":1}',
            )

            self.assertFalse(result.ok)
            self.assertEqual(ToolErrorType.INVALID_ARGUMENTS, result.error_type)
            self.assertEqual([], approvals)

    def test_hitl_batch_preserves_timeout_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            registry.register(
                ToolSpec(
                    "approved_slow_tool",
                    "Approved but slow.",
                    registry.object_schema({}),
                    lambda _args: time.sleep(0.08) or "late",
                    risk=ToolRisk.MEDIUM,
                )
            )
            guarded = HitlToolRegistry(
                registry,
                lambda _request: ApprovalResult(ApprovalDecision.APPROVE),
            )

            result = guarded.execute_many_results(
                [("approved_slow_tool", "{}")],
                timeout_seconds=0.01,
            )[0]

            self.assertFalse(result.ok)
            self.assertTrue(result.timed_out)
            self.assertFalse(result.retryable)
            self.assertIn("do not retry blindly", result.content)
            self.assertEqual(ToolErrorType.TIMEOUT, result.error_type)

    def test_approver_modified_arguments_are_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guarded = HitlToolRegistry(
                ToolRegistry(directory),
                lambda _request: ApprovalResult(
                    ApprovalDecision.APPROVE,
                    {"path": "a.txt", "content": "x", "extra": True},
                ),
            )

            result = guarded.execute_result(
                "write_file",
                '{"path":"a.txt","content":"x"}',
            )

            self.assertFalse(result.ok)
            self.assertEqual(ToolErrorType.INVALID_ARGUMENTS, result.error_type)

    def test_structured_policy_denial_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = ToolRegistry(directory).execute_result(
                "execute_command",
                json.dumps({"command": "pwd"}),
            )

            self.assertFalse(result.ok)
            self.assertEqual(ToolErrorType.POLICY_DENIED, result.error_type)

    def test_tool_metadata_drives_hitl_for_unknown_risk_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            registry.register(
                ToolSpec(
                    "external_action",
                    "Unknown external side effect.",
                    registry.object_schema({}),
                    lambda _args: "done",
                    risk=ToolRisk.UNKNOWN,
                    side_effect=ToolSideEffect.UNKNOWN,
                    concurrency=ConcurrencyPolicy.SERIAL,
                )
            )
            approvals = []
            guarded = HitlToolRegistry(
                registry,
                lambda request: approvals.append(request)
                or ApprovalResult(ApprovalDecision.APPROVE),
            )

            result = guarded.execute_result("external_action", "{}")

            self.assertTrue(result.ok)
            self.assertEqual(1, len(approvals))

    def test_failed_write_does_not_infer_a_committed_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            registry.register(
                ToolSpec(
                    "failing_write",
                    "Fails before committing.",
                    registry.object_schema(
                        {"path": {"type": "string"}}, required=["path"]
                    ),
                    lambda _args: ToolResult.failure(
                        "failing_write",
                        "write failed",
                        ToolErrorType.EXECUTION_ERROR,
                    ),
                    side_effect=ToolSideEffect.FILE_WRITE,
                    concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                    resource_resolver=lambda args: (
                        ResourceAccess(str(args["path"]), ResourceMode.WRITE),
                    ),
                )
            )

            result = registry.execute_result(
                "failing_write", '{"path":"not-written.py"}'
            )

            self.assertFalse(result.ok)
            self.assertEqual((), result.changed_files)


if __name__ == "__main__":
    unittest.main()
