from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from paicli.agent import Agent, AgentLoopError
from paicli.agents.loop import AgentLoopEngine
from paicli.agents.models import CompletionDecision, FinishReason, RunStatus
from paicli.llm_client import ChatResponse, ToolCall
from paicli.lsp import LspManager
from paicli.runtime import CancellationToken
from paicli.tools import ToolErrorType, ToolRegistry


class FakeClient:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        del tools
        self.requests.append([dict(message) for message in messages])
        if not self.responses:
            raise AssertionError("loop requested an unexpected extra model response")
        return self.responses.pop(0)


class AgentLoopEngineTest(unittest.TestCase):
    def test_engine_can_be_used_without_react_facade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history: list[dict[str, Any]] = [
                {"role": "system", "content": "role-specific prompt"}
            ]
            engine = AgentLoopEngine(
                FakeClient([ChatResponse("role result")]),
                ToolRegistry(directory),
                history,
            )

            outcome = engine.run({"role": "user", "content": "assigned task"})

            self.assertTrue(outcome.succeeded)
            self.assertEqual("role result", outcome.content)
            self.assertEqual(
                ["system", "user", "assistant"],
                [message["role"] for message in history],
            )

    def test_structured_outcome_accumulates_usage_and_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    ChatResponse(
                        "",
                        (
                            ToolCall(
                                "call-1",
                                "write_file",
                                '{"path":"result.txt","content":"done"}',
                            ),
                        ),
                        input_tokens=10,
                        output_tokens=2,
                        cached_input_tokens=3,
                    ),
                    ChatResponse(
                        "Implemented.",
                        input_tokens=20,
                        output_tokens=4,
                        cached_input_tokens=5,
                    ),
                ]
            )
            agent = Agent(client, ToolRegistry(directory))

            outcome = agent.run_outcome("Create result.txt")

            self.assertIs(RunStatus.SUCCEEDED, outcome.status)
            self.assertIs(FinishReason.FINAL_ANSWER, outcome.finish_reason)
            self.assertEqual("Implemented.", outcome.content)
            self.assertEqual(30, outcome.usage.input_tokens)
            self.assertEqual(6, outcome.usage.output_tokens)
            self.assertEqual(8, outcome.usage.cached_input_tokens)
            self.assertEqual(("result.txt",), outcome.changed_files)
            self.assertEqual(1, len(outcome.tool_results))
            self.assertTrue(outcome.tool_results[0].ok)
            self.assertEqual("call-1", outcome.tool_results[0].call_id)
            self.assertEqual("write_file", outcome.tool_results[0].tool_name)
            self.assertEqual(
                ("result.txt",), outcome.tool_results[0].changed_files
            )
            self.assertEqual("done", Path(directory, "result.txt").read_text())
            self.assertIs(outcome, agent.last_outcome)

    def test_structured_tool_failure_survives_into_run_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    ChatResponse(
                        "",
                        (
                            ToolCall(
                                "call-1",
                                "read_file",
                                '{"path":"missing.txt","extra":true}',
                            ),
                        ),
                    ),
                    ChatResponse("I could not read the invalid request."),
                ]
            )
            agent = Agent(client, ToolRegistry(directory))

            outcome = agent.run_outcome("Try an invalid tool call")

            self.assertTrue(outcome.succeeded)
            self.assertEqual(1, len(outcome.tool_results))
            result = outcome.tool_results[0]
            self.assertFalse(result.ok)
            self.assertEqual("call-1", result.call_id)
            self.assertEqual(ToolErrorType.INVALID_ARGUMENTS, result.error_type)
            self.assertTrue(result.retryable)

    def test_successful_write_still_emits_lsp_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    ChatResponse(
                        "",
                        (
                            ToolCall(
                                "call-1",
                                "write_file",
                                json.dumps(
                                    {
                                        "path": "bad.py",
                                        "content": "def broken(:\n    pass\n",
                                    }
                                ),
                            ),
                        ),
                    ),
                    ChatResponse("Reported the syntax problem."),
                    ChatResponse(
                        "",
                        (
                            ToolCall(
                                "call-2",
                                "write_file",
                                json.dumps(
                                    {
                                        "path": "bad.py",
                                        "content": "def fixed():\n    pass\n",
                                    }
                                ),
                            ),
                        ),
                    ),
                    ChatResponse("Fixed the syntax problem."),
                ]
            )
            events: list[tuple[str, str]] = []
            agent = Agent(
                client,
                ToolRegistry(directory),
                lsp=LspManager(directory),
                on_event=lambda kind, text: events.append((kind, text)),
            )

            agent.run_outcome("Write bad.py")

            diagnostics = [text for kind, text in events if kind == "diagnostics"]
            self.assertEqual(1, len(diagnostics))
            self.assertIn("bad.py:1", diagnostics[0])
            self.assertIn(
                "Post-edit diagnostics",
                str(client.requests[2][-1]["content"]),
            )
            self.assertEqual(
                "def fixed():\n    pass\n",
                Path(directory, "bad.py").read_text(encoding="utf-8"),
            )

    def test_custom_completion_policy_can_request_another_turn(self) -> None:
        class RejectDraftPolicy:
            def evaluate(
                self,
                response: ChatResponse,
                history: list[dict[str, Any]],
            ) -> CompletionDecision:
                del history
                return CompletionDecision(
                    response.content == "verified",
                    "Run deterministic verification before finishing.",
                )

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient([ChatResponse("draft"), ChatResponse("verified")])
            agent = Agent(
                client,
                ToolRegistry(directory),
                completion_policy=RejectDraftPolicy(),
            )

            outcome = agent.run_outcome("Make and verify a change")

            self.assertTrue(outcome.succeeded)
            self.assertEqual("verified", outcome.content)
            self.assertEqual(2, outcome.iterations)
            self.assertIn(
                "Run deterministic verification",
                str(client.requests[1][-1]["content"]),
            )

    def test_empty_no_tool_response_is_not_accepted_as_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    ChatResponse("", input_tokens=2, output_tokens=1),
                    ChatResponse("Now complete.", input_tokens=3, output_tokens=2),
                ]
            )
            events: list[tuple[str, str]] = []
            agent = Agent(
                client,
                ToolRegistry(directory),
                on_event=lambda kind, text: events.append((kind, text)),
            )

            outcome = agent.run_outcome("Do the task")

            self.assertTrue(outcome.succeeded)
            self.assertEqual(2, outcome.iterations)
            self.assertEqual("Now complete.", outcome.content)
            self.assertTrue(any(kind == "validation" for kind, _ in events))
            second_request = client.requests[1]
            self.assertTrue(
                any(
                    message.get("role") == "system"
                    and "Completion validation failed" in str(message.get("content"))
                    for message in second_request
                )
            )

    def test_repeated_empty_final_answers_stop_as_stagnant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient([ChatResponse("") for _ in range(3)])
            agent = Agent(client, ToolRegistry(directory), max_steps=50)

            outcome = agent.run_outcome("Return a real answer")

            self.assertIs(FinishReason.STAGNATION, outcome.finish_reason)
            self.assertEqual(3, outcome.iterations)
            self.assertEqual(3, len(client.requests))

    def test_three_identical_tool_observation_rounds_stop_as_stagnant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repeated = [
                ChatResponse(
                    "",
                    (ToolCall(f"call-{number}", "list_dir", "{}"),),
                    input_tokens=1,
                    output_tokens=1,
                )
                for number in range(3)
            ]
            client = FakeClient(repeated)
            agent = Agent(client, ToolRegistry(directory), max_steps=50)

            outcome = agent.run_outcome("Keep inspecting forever")

            self.assertIs(RunStatus.STOPPED, outcome.status)
            self.assertIs(FinishReason.STAGNATION, outcome.finish_reason)
            self.assertEqual(3, outcome.iterations)
            self.assertEqual(3, len(client.requests))
            self.assertIn("no observable progress", outcome.error)

    def test_token_budget_stops_before_another_model_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    ChatResponse(
                        "",
                        (ToolCall("call-1", "list_dir", "{}"),),
                        input_tokens=8,
                        output_tokens=3,
                    )
                ]
            )
            agent = Agent(client, ToolRegistry(directory), token_budget=10)

            outcome = agent.run_outcome("Inspect")

            self.assertIs(FinishReason.TOKEN_BUDGET, outcome.finish_reason)
            self.assertEqual(11, outcome.usage.total_tokens)
            self.assertEqual(1, len(client.requests))

    def test_legacy_run_raises_agent_loop_error_with_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    ChatResponse(
                        "",
                        (ToolCall("call-1", "list_dir", "{}"),),
                    )
                ]
            )
            agent = Agent(client, ToolRegistry(directory), max_steps=1)

            with self.assertRaises(AgentLoopError) as raised:
                agent.run("Never finish")

            self.assertIsNotNone(raised.exception.outcome)
            self.assertIs(
                FinishReason.MAX_ITERATIONS,
                raised.exception.outcome.finish_reason,  # type: ignore[union-attr]
            )

    def test_cancellation_during_model_call_prevents_requested_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = CancellationToken()

            class CancellingClient(FakeClient):
                def chat(
                    self,
                    messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]],
                ) -> ChatResponse:
                    token.cancel()
                    return super().chat(messages, tools)

            client = CancellingClient(
                [
                    ChatResponse(
                        "",
                        (
                            ToolCall(
                                "call-1",
                                "write_file",
                                '{"path":"must-not-exist.txt","content":"unsafe"}',
                            ),
                        ),
                        input_tokens=4,
                        output_tokens=1,
                    )
                ]
            )
            agent = Agent(
                client,
                ToolRegistry(directory),
                cancellation=token,
            )

            outcome = agent.run_outcome("Write a file")

            self.assertIs(RunStatus.CANCELLED, outcome.status)
            self.assertEqual(5, outcome.usage.total_tokens)
            self.assertFalse(Path(directory, "must-not-exist.txt").exists())
            self.assertEqual(["system", "user"], [m["role"] for m in agent.history])

    def test_cancellation_returns_structured_outcome_without_calling_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = CancellationToken()
            token.cancel()
            client = FakeClient([])
            agent = Agent(client, ToolRegistry(directory), cancellation=token)

            outcome = agent.run_outcome("Stop before the first request")

            self.assertIs(RunStatus.CANCELLED, outcome.status)
            self.assertIs(FinishReason.CANCELLED, outcome.finish_reason)
            self.assertEqual(0, outcome.iterations)
            self.assertEqual([], client.requests)

    def test_budget_state_is_new_for_each_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(
                [
                    ChatResponse("first", input_tokens=6, output_tokens=3),
                    ChatResponse("second", input_tokens=6, output_tokens=3),
                ]
            )
            agent = Agent(client, ToolRegistry(directory), token_budget=10)

            first = agent.run_outcome("one")
            second = agent.run_outcome("two")

            self.assertTrue(first.succeeded)
            self.assertTrue(second.succeeded)
            self.assertEqual(9, first.usage.total_tokens)
            self.assertEqual(9, second.usage.total_tokens)


if __name__ == "__main__":
    unittest.main()
