from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable

from paicli.agents.models import FinishReason
from paicli.llm_client import ChatResponse, ToolCall
from paicli.orchestration import (
    DeterministicResultAggregator,
    OrchestrationStatus,
    PlanModeRuntime,
    PlanReviewDecision,
    TeamModeRuntime,
)
from paicli.planning import LlmPlanner, StaticPlanner, Task, TaskStatus, TaskType
from paicli.review import ReviewVerdict, ReviewerAgent
from paicli.subagents import (
    SubAgentFactory,
    TaskCompletionPolicy,
    TaskPacket,
    ToolScope,
)
from paicli.tool_contracts import ToolErrorType, ToolResult
from paicli.tools import ToolRegistry


class RoutingClient:
    model = "routing-fake"
    provider = "test"
    context_window = 64_000
    supports_prompt_caching = False

    def __init__(
        self,
        *,
        plan: dict[str, Any] | list[dict[str, Any]] | None = None,
        worker: Callable[[str, int, list[dict[str, Any]], list[str]], ChatResponse]
        | None = None,
        reviewer: Callable[[str, int, list[dict[str, Any]], list[str]], ChatResponse]
        | None = None,
        aggregator: Callable[[list[dict[str, Any]]], ChatResponse] | None = None,
    ) -> None:
        self.plans = (
            list(plan)
            if isinstance(plan, list)
            else [plan] if plan is not None else []
        )
        self.worker_handler = worker
        self.reviewer_handler = reviewer
        self.aggregator_handler = aggregator
        self.requests: list[tuple[str, list[dict[str, Any]], list[str]]] = []
        self.worker_counts: dict[str, int] = {}
        self.reviewer_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        copied = [dict(message) for message in messages]
        tool_names = [str(item["function"]["name"]) for item in tools]
        system = str(messages[0].get("content", "")) if messages else ""
        if "coding-task planner" in system:
            role = "planner"
        elif "isolated coding-task worker" in system:
            role = "worker"
        elif "independent coding-task reviewer" in system:
            role = "reviewer"
        elif "final result aggregator" in system:
            role = "aggregator"
        else:
            role = "unknown"
        with self._lock:
            self.requests.append((role, copied, tool_names))

        if role == "planner":
            if not self.plans:
                raise AssertionError("planner response was not configured")
            return ChatResponse(
                json.dumps(self.plans.pop(0)),
                input_tokens=3,
                output_tokens=2,
            )
        if role == "worker":
            task_id = _task_id(messages)
            with self._lock:
                count = self.worker_counts.get(task_id, 0) + 1
                self.worker_counts[task_id] = count
            if self.worker_handler is None:
                return ChatResponse(f"{task_id} complete")
            return self.worker_handler(task_id, count, copied, tool_names)
        if role == "reviewer":
            task_id = _task_id(messages)
            with self._lock:
                count = self.reviewer_counts.get(task_id, 0) + 1
                self.reviewer_counts[task_id] = count
            if self.reviewer_handler is None:
                return ChatResponse(_review_json("approved", "verified"))
            return self.reviewer_handler(task_id, count, copied, tool_names)
        if role == "aggregator":
            if self.aggregator_handler is not None:
                return self.aggregator_handler(copied)
            return ChatResponse("aggregated result")
        raise AssertionError(f"unexpected system prompt: {system[:100]}")


class ConcurrentWorkerClient:
    model = "concurrent-fake"
    provider = "test"
    context_window = 64_000
    supports_prompt_caching = False

    def __init__(self, parties: int | None = None) -> None:
        self.barrier = threading.Barrier(parties) if parties else None
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.worker_calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        del tools
        system = str(messages[0].get("content", ""))
        if "isolated coding-task worker" not in system:
            return ChatResponse("aggregate")
        with self._lock:
            self.active += 1
            self.worker_calls += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=1)
            time.sleep(0.05)
            task_type = _task_type(messages)
            if (
                task_type in {"FILE_READ", "VERIFICATION"}
                and messages[-1].get("role") != "tool"
            ):
                return ChatResponse(
                    "",
                    (ToolCall(f"observe-{self.worker_calls}", "list_dir", "{}"),),
                )
            return ChatResponse(f"{_task_id(messages)} complete")
        finally:
            with self._lock:
                self.active -= 1


class OrchestrationTest(unittest.TestCase):
    def test_plan_mode_connects_planner_workers_tools_and_dependency_handoff(self) -> None:
        plan = {
            "summary": "inspect then write",
            "tasks": [
                {
                    "id": "inspect",
                    "description": "Inspect the repository",
                    "type": "FILE_READ",
                    "dependencies": [],
                    "acceptance_criteria": ["Repository evidence is reported"],
                },
                {
                    "id": "write",
                    "description": "Create result.txt",
                    "type": "FILE_WRITE",
                    "dependencies": ["inspect"],
                    "acceptance_criteria": ["result.txt exists"],
                },
            ],
        }

        def worker(
            task_id: str,
            count: int,
            messages: list[dict[str, Any]],
            tool_names: list[str],
        ) -> ChatResponse:
            if task_id == "inspect":
                self.assertNotIn("write_file", tool_names)
                if messages[-1]["role"] == "tool":
                    return ChatResponse("Repository inspected")
                self.assertEqual(1, count)
                return ChatResponse(
                    "",
                    (ToolCall("inspect-1", "list_dir", "{}"),),
                )
            self.assertIn("write_file", tool_names)
            if messages[-1]["role"] == "tool":
                return ChatResponse("Created result.txt from inspected evidence")
            self.assertIn("Repository inspected", str(messages[-1]["content"]))
            return ChatResponse(
                "",
                (
                    ToolCall(
                        "write-1",
                        "write_file",
                        '{"path":"result.txt","content":"done"}',
                    ),
                ),
            )

        with tempfile.TemporaryDirectory() as directory:
            client = RoutingClient(plan=plan, worker=worker)
            tools = ToolRegistry(directory)
            factory = SubAgentFactory(
                client,
                tools,
                directory,
                enable_memory=False,
            )
            runtime = PlanModeRuntime(
                LlmPlanner(client),
                factory,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Inspect the repository and create result.txt")

            self.assertIs(OrchestrationStatus.SUCCEEDED, result.status)
            self.assertTrue(result.succeeded)
            self.assertEqual("done", Path(directory, "result.txt").read_text())
            self.assertEqual(("result.txt",), result.changed_files)
            self.assertEqual(TaskStatus.COMPLETED, result.plan.task("inspect").status)
            self.assertEqual(TaskStatus.COMPLETED, result.plan.task("write").status)
            self.assertEqual(1, len(result.records["inspect"].worker_outcomes))
            self.assertEqual(1, len(result.records["write"].worker_outcomes))

    def test_default_llm_aggregator_produces_final_answer_and_usage(self) -> None:
        planner = StaticPlanner(
            [Task("inspect", "Inspect", task_type=TaskType.ANALYSIS)]
        )
        client = RoutingClient(
            aggregator=lambda _messages: ChatResponse(
                "Final synthesized answer",
                input_tokens=5,
                output_tokens=2,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = PlanModeRuntime(
                planner,  # type: ignore[arg-type]
                factory,
            )

            result = runtime.run("Inspect")

            self.assertEqual("Final synthesized answer", result.answer)
            self.assertIsNotNone(result.aggregation_outcome)
            self.assertEqual(7, result.usage.total_tokens)

    def test_plan_can_be_cancelled_after_validation_before_workers_run(self) -> None:
        planner = StaticPlanner([Task("inspect", "Inspect", task_type=TaskType.FILE_READ)])
        with tempfile.TemporaryDirectory() as directory:
            client = ConcurrentWorkerClient()
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = PlanModeRuntime(
                planner,  # type: ignore[arg-type]
                factory,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Inspect", approval=lambda _plan: False)

            self.assertIs(OrchestrationStatus.CANCELLED, result.status)
            self.assertEqual(0, client.worker_calls)
            self.assertEqual(TaskStatus.PENDING, result.plan.task("inspect").status)

    def test_read_only_plan_tasks_execute_concurrently(self) -> None:
        planner = StaticPlanner(
            [
                Task("a", "Analyze A", task_type=TaskType.ANALYSIS),
                Task("b", "Analyze B", task_type=TaskType.ANALYSIS),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = ConcurrentWorkerClient(parties=2)
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = PlanModeRuntime(
                planner,  # type: ignore[arg-type]
                factory,
                max_workers=2,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Analyze two independent areas")

            self.assertTrue(result.succeeded)
            self.assertEqual(2, client.max_active)

    def test_mutation_capable_plan_tasks_are_serial_even_when_ready_together(self) -> None:
        planner = StaticPlanner(
            [
                Task("a", "Verify A", task_type=TaskType.VERIFICATION),
                Task("b", "Verify B", task_type=TaskType.VERIFICATION),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = ConcurrentWorkerClient()
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = PlanModeRuntime(
                planner,  # type: ignore[arg-type]
                factory,
                max_workers=4,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Edit two files")

            self.assertTrue(result.succeeded)
            self.assertEqual(1, client.max_active)

    def test_task_completion_uses_structured_success_not_result_wording(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = Task("write", "Write a file", task_type=TaskType.FILE_WRITE)
            policy = TaskCompletionPolicy(task, ToolRegistry(directory))

            policy.begin_run()
            policy.observe_tool_results(
                (
                    ToolResult.failure(
                        "write_file",
                        "Wrote deceptive.txt",
                        ToolErrorType.EXECUTION_ERROR,
                    ),
                )
            )
            rejected = policy.evaluate(ChatResponse("Done."), [])

            policy.begin_run()
            policy.observe_tool_results(
                (
                    ToolResult.success(
                        "write_file",
                        "claimed success without an artifact",
                    ),
                )
            )
            missing_artifact = policy.evaluate(ChatResponse("Done."), [])

            policy.begin_run()
            policy.observe_tool_results(
                (
                    ToolResult.success(
                        "write_file",
                        "custom success wording",
                        changed_files=("real.txt",),
                    ),
                )
            )
            approved = policy.evaluate(ChatResponse("Done."), [])

            self.assertFalse(rejected.completed)
            self.assertFalse(missing_artifact.completed)
            self.assertTrue(approved.completed)

    def test_file_write_task_cannot_finish_with_only_a_text_claim(self) -> None:
        planner = StaticPlanner(
            [Task("write", "Write a file", task_type=TaskType.FILE_WRITE)]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = ConcurrentWorkerClient()
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
                max_steps=10,
            )
            runtime = PlanModeRuntime(
                planner,  # type: ignore[arg-type]
                factory,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Write a file")

            self.assertIs(OrchestrationStatus.FAILED, result.status)
            self.assertEqual(TaskStatus.FAILED, result.plan.task("write").status)
            outcome = result.records["write"].worker_outcomes[0]
            self.assertIs(FinishReason.STAGNATION, outcome.finish_reason)
            self.assertIn("no observable progress", outcome.error)

    def test_read_only_worker_cannot_execute_hallucinated_write_tool(self) -> None:
        responses = [
            ChatResponse(
                "",
                (
                    ToolCall(
                        "write-1",
                        "write_file",
                        '{"path":"forbidden.txt","content":"no"}',
                    ),
                ),
            ),
            ChatResponse("Reported that write permission was unavailable."),
        ]

        class HallucinatingClient:
            model = "hallucinating"
            context_window = 64_000

            def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
                if len(responses) == 2:
                    self_outer.assertNotIn(
                        "write_file",
                        [item["function"]["name"] for item in tools],
                    )
                return responses.pop(0)

        self_outer = self
        with tempfile.TemporaryDirectory() as directory:
            factory = SubAgentFactory(
                HallucinatingClient(),  # type: ignore[arg-type]
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            task = Task("inspect", "Inspect", task_type=TaskType.ANALYSIS)
            plan = StaticPlanner([task]).create_plan("Inspect")
            worker = factory.create_worker(plan.task("inspect"))
            packet = TaskPacket.from_task(plan, plan.task("inspect"), {})

            outcome = worker.run_task(packet)

            self.assertTrue(outcome.succeeded)
            self.assertEqual(ToolErrorType.POLICY_DENIED, outcome.tool_results[0].error_type)
            self.assertFalse(Path(directory, "forbidden.txt").exists())

    def test_team_retries_only_rejected_task_and_preserves_isolated_histories(self) -> None:
        plan = {
            "summary": "edit then verify",
            "tasks": [
                {
                    "id": "edit",
                    "description": "Edit implementation",
                    "type": "ANALYSIS",
                    "dependencies": [],
                    "acceptance_criteria": ["implementation analysis is fixed"],
                },
                {
                    "id": "verify",
                    "description": "Verify implementation",
                    "type": "ANALYSIS",
                    "dependencies": ["edit"],
                    "acceptance_criteria": ["verification passes"],
                },
            ],
        }

        def worker(
            task_id: str,
            count: int,
            messages: list[dict[str, Any]],
            _tools: list[str],
        ) -> ChatResponse:
            if task_id == "edit":
                if count == 1:
                    return ChatResponse("draft implementation")
                # The same worker instance keeps its own previous attempt and
                # receives structured reviewer feedback as the next user turn.
                self.assertTrue(
                    any(
                        message.get("role") == "assistant"
                        and message.get("content") == "draft implementation"
                        for message in messages
                    )
                )
                self.assertIn("needs a concrete fix", str(messages[-1]["content"]))
                return ChatResponse("fixed implementation")
            # The dependent worker receives only the approved dependency result,
            # not the edit worker's full conversation history.
            self.assertFalse(
                any(
                    message.get("role") == "assistant"
                    and message.get("content") == "draft implementation"
                    for message in messages
                )
            )
            self.assertIn("fixed implementation", str(messages[-1]["content"]))
            return ChatResponse("verification passes")

        def reviewer(
            task_id: str,
            count: int,
            _messages: list[dict[str, Any]],
            tool_names: list[str],
        ) -> ChatResponse:
            self.assertNotIn("write_file", tool_names)
            if task_id == "edit" and count == 1:
                return ChatResponse(
                    _review_json(
                        "changes_requested",
                        "needs a concrete fix",
                        issues=["implementation is still a draft"],
                        suggestions=["finish the local implementation"],
                    )
                )
            return ChatResponse(_review_json("approved", "evidence is sufficient"))

        with tempfile.TemporaryDirectory() as directory:
            client = RoutingClient(plan=plan, worker=worker, reviewer=reviewer)
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = TeamModeRuntime(
                LlmPlanner(client),
                factory,
                max_review_retries=2,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Edit and verify the implementation")

            self.assertTrue(result.succeeded)
            edit = result.plan.task("edit")
            verify = result.plan.task("verify")
            self.assertEqual(2, edit.execution_attempts)
            self.assertEqual(2, edit.review_attempts)
            self.assertEqual(1, verify.execution_attempts)
            self.assertEqual(1, verify.review_attempts)
            self.assertEqual(2, len(result.records["edit"].worker_outcomes))
            self.assertEqual(1, len(result.records["verify"].worker_outcomes))
            self.assertIs(
                ReviewVerdict.APPROVED,
                result.records["edit"].final_review.verdict,  # type: ignore[union-attr]
            )

    def test_plan_preserves_structured_worker_failure_when_model_call_raises(self) -> None:
        planner = StaticPlanner(
            [Task("inspect", "Inspect", task_type=TaskType.ANALYSIS)]
        )

        class RaisingClient:
            model = "raising"
            context_window = 64_000

            def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
                del messages, tools
                raise RuntimeError("provider unavailable")

        with tempfile.TemporaryDirectory() as directory:
            factory = SubAgentFactory(
                RaisingClient(),  # type: ignore[arg-type]
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = PlanModeRuntime(
                planner,  # type: ignore[arg-type]
                factory,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Inspect")

            self.assertIs(OrchestrationStatus.FAILED, result.status)
            outcome = result.records["inspect"].worker_outcomes[0]
            self.assertIs(FinishReason.INTERNAL_ERROR, outcome.finish_reason)
            self.assertIn("provider unavailable", outcome.error)

    def test_reviewer_model_error_fails_task_instead_of_passing_it(self) -> None:
        plan = {
            "summary": "review one analysis",
            "tasks": [
                {
                    "id": "inspect",
                    "description": "Inspect",
                    "type": "ANALYSIS",
                    "dependencies": [],
                    "acceptance_criteria": ["evidence exists"],
                }
            ],
        }

        def reviewer(
            _task_id_value: str,
            _count: int,
            _messages: list[dict[str, Any]],
            _tools: list[str],
        ) -> ChatResponse:
            raise RuntimeError("review model unavailable")

        with tempfile.TemporaryDirectory() as directory:
            client = RoutingClient(plan=plan, reviewer=reviewer)
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = TeamModeRuntime(
                LlmPlanner(client),
                factory,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Inspect and review")

            self.assertIs(OrchestrationStatus.FAILED, result.status)
            self.assertEqual(TaskStatus.FAILED, result.plan.task("inspect").status)
            review = result.records["inspect"].final_review
            self.assertIsNotNone(review)
            self.assertIs(ReviewVerdict.ERROR, review.verdict)  # type: ignore[union-attr]
            self.assertIn("review model unavailable", result.plan.task("inspect").error)

    def test_team_rejection_blocks_descendants_but_not_independent_branch(self) -> None:
        plan = {
            "summary": "one rejected branch and one independent branch",
            "tasks": [
                {
                    "id": "rejected",
                    "description": "Rejected work",
                    "type": "ANALYSIS",
                    "dependencies": [],
                    "acceptance_criteria": ["must be valid"],
                },
                {
                    "id": "blocked",
                    "description": "Depends on rejected work",
                    "type": "ANALYSIS",
                    "dependencies": ["rejected"],
                    "acceptance_criteria": [],
                },
                {
                    "id": "independent",
                    "description": "Independent work",
                    "type": "ANALYSIS",
                    "dependencies": [],
                    "acceptance_criteria": ["complete independently"],
                },
            ],
        }

        def reviewer(
            task_id: str,
            _count: int,
            _messages: list[dict[str, Any]],
            _tools: list[str],
        ) -> ChatResponse:
            if task_id == "rejected":
                return ChatResponse(
                    _review_json(
                        "rejected",
                        "plan assumption is invalid",
                        issues=["false prerequisite"],
                    )
                )
            return ChatResponse(_review_json("approved", "independent evidence is valid"))

        with tempfile.TemporaryDirectory() as directory:
            client = RoutingClient(plan=plan, reviewer=reviewer)
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = TeamModeRuntime(
                LlmPlanner(client),
                factory,
                max_workers=2,
                max_review_retries=0,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Run independent branches")

            self.assertIs(OrchestrationStatus.PARTIAL, result.status)
            self.assertEqual(TaskStatus.FAILED, result.plan.task("rejected").status)
            self.assertEqual(TaskStatus.SKIPPED, result.plan.task("blocked").status)
            self.assertEqual(
                TaskStatus.COMPLETED,
                result.plan.task("independent").status,
            )
            self.assertEqual(1, client.worker_counts["rejected"])
            self.assertEqual(1, client.worker_counts["independent"])

    def test_team_does_not_silently_pass_exhausted_review_retries(self) -> None:
        plan = {
            "summary": "change then use it",
            "tasks": [
                {
                    "id": "change",
                    "description": "Make a change",
                    "type": "ANALYSIS",
                    "dependencies": [],
                    "acceptance_criteria": ["change analysis is correct"],
                },
                {
                    "id": "downstream",
                    "description": "Use the change",
                    "type": "ANALYSIS",
                    "dependencies": ["change"],
                    "acceptance_criteria": [],
                },
            ],
        }

        def reviewer(
            _task_id: str,
            _count: int,
            _messages: list[dict[str, Any]],
            _tools: list[str],
        ) -> ChatResponse:
            return ChatResponse(
                _review_json(
                    "changes_requested",
                    "still incorrect",
                    issues=["acceptance criterion is unmet"],
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            client = RoutingClient(plan=plan, reviewer=reviewer)
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = TeamModeRuntime(
                LlmPlanner(client),
                factory,
                max_review_retries=2,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Make and use a change")

            self.assertIs(OrchestrationStatus.FAILED, result.status)
            self.assertEqual(TaskStatus.FAILED, result.plan.task("change").status)
            self.assertEqual(TaskStatus.SKIPPED, result.plan.task("downstream").status)
            self.assertEqual(3, result.plan.task("change").execution_attempts)
            self.assertEqual(3, len(result.records["change"].worker_outcomes))
            self.assertIn("not resolved", result.plan.task("change").error)

    def test_reviewer_can_read_the_actual_changed_artifact_before_approval(self) -> None:
        plan = {
            "summary": "write reviewed artifact",
            "tasks": [
                {
                    "id": "write",
                    "description": "Create reviewed.txt",
                    "type": "FILE_WRITE",
                    "dependencies": [],
                    "acceptance_criteria": ["reviewed.txt contains verified"],
                }
            ],
        }

        def worker(
            _task_id_value: str,
            count: int,
            messages: list[dict[str, Any]],
            _tools: list[str],
        ) -> ChatResponse:
            if count == 1:
                return ChatResponse(
                    "",
                    (
                        ToolCall(
                            "write-artifact",
                            "write_file",
                            '{"path":"reviewed.txt","content":"verified"}',
                        ),
                    ),
                )
            self.assertEqual("tool", messages[-1]["role"])
            return ChatResponse("Artifact created for review")

        def reviewer(
            _task_id_value: str,
            count: int,
            messages: list[dict[str, Any]],
            tool_names: list[str],
        ) -> ChatResponse:
            self.assertIn("read_file", tool_names)
            self.assertNotIn("write_file", tool_names)
            if count == 1:
                return ChatResponse(
                    "",
                    (
                        ToolCall(
                            "read-artifact",
                            "read_file",
                            '{"path":"reviewed.txt"}',
                        ),
                    ),
                )
            self.assertEqual("verified", messages[-1]["content"])
            return ChatResponse(
                _review_json("approved", "read_file confirmed the artifact")
            )

        with tempfile.TemporaryDirectory() as directory:
            client = RoutingClient(plan=plan, worker=worker, reviewer=reviewer)
            factory = SubAgentFactory(
                client,
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            runtime = TeamModeRuntime(
                LlmPlanner(client),
                factory,
                aggregator=DeterministicResultAggregator(),
            )

            result = runtime.run("Create a reviewed artifact")

            self.assertTrue(result.succeeded)
            self.assertEqual("verified", Path(directory, "reviewed.txt").read_text())
            record = result.records["write"]
            self.assertEqual(1, len(record.worker_outcomes))
            self.assertEqual(1, len(record.reviews))
            self.assertEqual(2, record.worker_outcomes[0].iterations)
            self.assertEqual(2, record.reviews[0].model_outcomes[0].iterations)
            self.assertEqual(
                "read_file",
                record.reviews[0].model_outcomes[0].tool_results[0].tool_name,
            )

    def test_reviewer_repairs_invalid_json_once(self) -> None:
        responses = [
            ChatResponse("not json", input_tokens=2, output_tokens=1),
            ChatResponse(
                _review_json("approved", "valid after repair"),
                input_tokens=2,
                output_tokens=1,
            ),
        ]

        class ReviewerClient:
            model = "reviewer-fake"
            context_window = 64_000

            def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
                del messages, tools
                return responses.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            factory = SubAgentFactory(
                ReviewerClient(),  # type: ignore[arg-type]
                ToolRegistry(directory),
                directory,
                enable_memory=False,
            )
            plan = StaticPlanner([Task("a", "A")]).create_plan("goal")
            packet = TaskPacket.from_task(plan, plan.task("a"), {})
            worker_outcome = _successful_outcome("worker evidence")
            reviewer = ReviewerAgent(factory.create_reviewer())

            review = reviewer.review(packet, worker_outcome)

            self.assertIs(ReviewVerdict.APPROVED, review.result.verdict)
            self.assertEqual(2, len(review.model_outcomes))
            self.assertEqual(6, sum(item.usage.total_tokens for item in review.model_outcomes))


def _task_type(messages: list[dict[str, Any]]) -> str:
    pattern = re.compile(r'"task"\s*:\s*\{.*?"type"\s*:\s*"([^"]+)"', re.DOTALL)
    for message in reversed(messages):
        match = pattern.search(str(message.get("content", "")))
        if match:
            return match.group(1)
    return ""


def _task_id(messages: list[dict[str, Any]]) -> str:
    # Task packets and review packets both place the current node under
    # ``task.id``. Direct dependency IDs may occur later in the same prompt, so
    # searching for any ID token would route a dependent worker incorrectly.
    pattern = re.compile(r'"task"\s*:\s*\{.*?"id"\s*:\s*"([^"]+)"', re.DOTALL)
    for message in reversed(messages):
        match = pattern.search(str(message.get("content", "")))
        if match:
            return match.group(1)
    return "unknown"


def _review_json(
    verdict: str,
    summary: str,
    *,
    issues: list[str] | None = None,
    suggestions: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "summary": summary,
            "issues": issues or [],
            "suggestions": suggestions or [],
            "evidence": ["worker result"],
        }
    )


def _successful_outcome(content: str):
    from paicli.agents.models import AgentOutcome, RunStatus
    from paicli.context import TokenUsage

    return AgentOutcome(
        "worker-run",
        RunStatus.SUCCEEDED,
        FinishReason.FINAL_ANSWER,
        content,
        TokenUsage(1, 1, 0),
        iterations=1,
    )


if __name__ == "__main__":
    unittest.main()
