from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from paicli.agents.models import AgentOutcome, FinishReason, RunStatus
from paicli.bootstrap import build_application_runtime
from paicli.context import TokenUsage
from paicli.execution import RunCheckpointObserver, RunCoordinator
from paicli.llm_client import ChatResponse, ToolCall
from paicli.observability import RunLimits
from paicli.orchestration import TaskRunRecord
from paicli.planning import ExecutionPlan, Task, TaskStatus, TaskType
from paicli.snapshot import SnapshotPhase, SnapshotService
from paicli.state import RunStateStore, StoredRunStatus
from paicli.subagents import ToolScope


class SequenceClient:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.model = "fake-model"
        self.provider = "fake"
        self.context_window = 32_000
        self.supports_prompt_caching = False

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        del messages, tools
        if not self.responses:
            raise AssertionError("unexpected model call")
        return self.responses.pop(0)


class StateRecoveryTest(unittest.TestCase):
    def test_run_state_round_trips_plan_records_and_structured_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStateStore(Path(directory, "runs.db"))
            try:
                plan = ExecutionPlan(
                    "goal",
                    [Task("inspect", "Inspect", task_type=TaskType.FILE_READ)],
                )
                plan.task("inspect").mark_completed("evidence")
                outcome = AgentOutcome(
                    "agent-run",
                    RunStatus.SUCCEEDED,
                    FinishReason.FINAL_ANSWER,
                    "evidence",
                    TokenUsage(11, 3, 2),
                    1,
                )
                records = {
                    "inspect": TaskRunRecord(
                        "inspect",
                        "worker-inspect",
                        ToolScope.READ_ONLY,
                        [outcome],
                        [],
                    )
                }
                store.create(
                    run_id="run-1",
                    mode="plan",
                    goal="goal",
                    prompt="goal",
                )
                store.checkpoint(
                    "run-1",
                    phase="after_task",
                    plan=plan,
                    records=records,
                )

                loaded = store.load("run-1")

                self.assertEqual(TaskStatus.COMPLETED, loaded.plan.task("inspect").status)  # type: ignore[union-attr]
                self.assertEqual(
                    14,
                    loaded.records["inspect"].worker_outcomes[0].usage.total_tokens,
                )
                self.assertGreaterEqual(store.checkpoint_count("run-1"), 2)
            finally:
                store.close()

    def test_stopped_react_run_keeps_workspace_and_is_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "value.txt"
            target.write_text("before", encoding="utf-8")
            client = SequenceClient(
                [
                    ChatResponse(
                        "",
                        (
                            ToolCall(
                                "write-1",
                                "write_file",
                                json.dumps({"path": "value.txt", "content": "after"}),
                            ),
                        ),
                    )
                ]
            )
            runtime = build_application_runtime(
                client,
                root,
                enable_memory=False,
                enable_trace=True,
                enable_hitl=False,
                max_steps=1,
            )
            store = RunStateStore(root / ".paicli" / "test-runs.db")
            coordinator = RunCoordinator(runtime, root, state_store=store)
            try:
                result = coordinator.execute("react", "change value.txt")

                self.assertEqual(StoredRunStatus.STOPPED, result.status)
                self.assertFalse(result.rolled_back)
                self.assertEqual("after", target.read_text(encoding="utf-8"))
                stored = store.load(result.run_id)
                self.assertEqual(StoredRunStatus.STOPPED, stored.status)
                self.assertFalse(stored.metadata["rolled_back"])
                self.assertIn(stored.run_id, {item.run_id for item in store.resumable_runs()})
                summary = runtime.trace_store.run_summary(result.run_id)  # type: ignore[union-attr]
                self.assertEqual("stopped", summary["status"])
            finally:
                coordinator.close()

    def test_global_model_call_budget_stops_before_second_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = SequenceClient(
                [
                    ChatResponse(
                        "",
                        (ToolCall("read-1", "list_dir", "{}"),),
                        input_tokens=5,
                        output_tokens=1,
                    ),
                    ChatResponse("should never be reached"),
                ]
            )
            runtime = build_application_runtime(
                client,
                root,
                enable_memory=False,
                enable_trace=True,
            )
            coordinator = RunCoordinator(
                runtime,
                root,
                limits=RunLimits(max_model_calls=1),
            )
            try:
                result = coordinator.execute("react", "inspect twice")

                self.assertEqual(StoredRunStatus.FAILED, result.status)
                self.assertIn("model-call budget", result.error)
                self.assertEqual(1, result.budget["model_calls"])
                summary = runtime.trace_store.run_summary(result.run_id)  # type: ignore[union-attr]
                self.assertEqual(1, summary["model_calls"])
            finally:
                coordinator.close()

    def test_interrupted_plan_restores_current_task_snapshot_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "value.py"
            target.write_text("VALUE = 'before'\n", encoding="utf-8")
            snapshots = SnapshotService(root)
            store = RunStateStore(root / ".paicli" / "runs.db")
            before = snapshots.capture_tree(SnapshotPhase.BEFORE)
            plan = ExecutionPlan(
                "update value",
                [
                    Task("inspect", "Inspect value.py", task_type=TaskType.FILE_READ),
                    Task(
                        "edit",
                        "Set VALUE to recovered",
                        ("inspect",),
                        task_type=TaskType.FILE_WRITE,
                    ),
                ],
            )
            plan.task("inspect").mark_completed("VALUE is before")
            store.create(
                run_id="recover-me",
                mode="plan",
                goal="update value",
                prompt="update value",
                before_snapshot_id=before.id,
            )
            observer = RunCheckpointObserver(
                "recover-me",
                store,
                snapshots,
            )
            observer.plan_ready("plan", plan, {})
            observer.before_task("plan", plan, plan.task("edit"), {})
            # Simulate a process dying after the side effect but before the
            # after-task checkpoint.
            target.write_text("VALUE = 'uncertain'\n", encoding="utf-8")
            plan.task("edit").mark_running()

            client = SequenceClient(
                [
                    ChatResponse(
                        "",
                        (
                            ToolCall(
                                "write-recovery",
                                "write_file",
                                json.dumps(
                                    {
                                        "path": "value.py",
                                        "content": "VALUE = 'recovered'\n",
                                    }
                                ),
                            ),
                        ),
                    ),
                    ChatResponse("Updated value.py after recovery."),
                    ChatResponse("Recovered run completed."),
                ]
            )
            runtime = build_application_runtime(
                client,
                root,
                enable_memory=False,
                enable_trace=False,
                enable_hitl=False,
                subagent_max_steps=4,
            )
            coordinator = RunCoordinator(
                runtime,
                root,
                state_store=store,
                snapshot_service=snapshots,
            )
            try:
                result = coordinator.resume("recover-me")

                self.assertEqual(StoredRunStatus.SUCCEEDED, result.status)
                self.assertTrue(result.resumed)
                self.assertEqual(
                    "VALUE = 'recovered'\n",
                    target.read_text(encoding="utf-8"),
                )
                recovered = store.load("recover-me")
                self.assertEqual(StoredRunStatus.SUCCEEDED, recovered.status)
                self.assertEqual(TaskStatus.COMPLETED, recovered.plan.task("inspect").status)  # type: ignore[union-attr]
                self.assertEqual(TaskStatus.COMPLETED, recovered.plan.task("edit").status)  # type: ignore[union-attr]
            finally:
                coordinator.close()


if __name__ == "__main__":
    unittest.main()
