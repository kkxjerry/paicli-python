from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from paicli.bootstrap import build_react_runtime
from paicli.context import ContextController, ContextSettings
from paicli.llm_client import ChatResponse
from paicli.memory import (
    ConversationHistoryCompactor,
    LongTermMemory,
    MemoryManager,
    estimate_message_tokens,
    estimate_tokens,
)


class SummaryClient:
    def __init__(self, summary: str = "goal and completed work") -> None:
        self.summary = summary
        self.requests: list[list[dict[str, Any]]] = []
        self.context_window = 16_000
        self.supports_prompt_caching = False
        self.model = "summary-model"
        self.provider = "test"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        self.requests.append([dict(message) for message in messages])
        return ChatResponse(self.summary)


class FailingSummaryClient(SummaryClient):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        self.requests.append([dict(message) for message in messages])
        raise RuntimeError("summary provider unavailable")


def round_messages(number: int) -> list[dict[str, Any]]:
    call_id = f"call-{number}"
    return [
        {"role": "user", "content": f"user round {number}"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": f'{{"path":"file-{number}.py"}}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "read_file",
            "content": f"content {number}",
        },
        {"role": "assistant", "content": f"round {number} complete"},
    ]


class ContextMemoryRuntimeTest(unittest.TestCase):
    def test_compactor_keeps_recent_user_rounds_and_tool_pairs(self) -> None:
        summarized: list[list[dict[str, Any]]] = []
        compactor = ConversationHistoryCompactor(
            summarize=lambda messages: summarized.append(messages) or "old round summary",
            retain_recent_rounds=3,
        )
        history = [{"role": "system", "content": "base rules"}]
        for number in range(4):
            history.extend(round_messages(number))

        compacted = compactor.compact(history)

        self.assertEqual("base rules", compacted[0]["content"])
        self.assertIn("old round summary", compacted[1]["content"])
        self.assertEqual(1, len(summarized))
        self.assertEqual("user round 0", summarized[0][0]["content"])
        recent_users = [
            message["content"]
            for message in compacted
            if message.get("role") == "user"
        ]
        self.assertEqual(
            ["user round 1", "user round 2", "user round 3"],
            recent_users,
        )
        self._assert_no_orphan_tool_results(compacted)

    def test_single_long_round_uses_a_protocol_safe_suffix(self) -> None:
        compactor = ConversationHistoryCompactor(
            summarize=lambda _messages: "single-turn summary",
            retain_recent_rounds=3,
            minimum_tail_messages=5,
        )
        history = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "one long coding task"},
        ]
        for number in range(6):
            history.extend(round_messages(number)[1:])

        compacted = compactor.compact(history)

        self.assertLess(len(compacted), len(history))
        self.assertNotEqual("tool", compacted[2].get("role"))
        self._assert_no_orphan_tool_results(compacted)

    def test_multimodal_history_omits_base64_from_summary_and_token_estimate(self) -> None:
        payload = "A" * 100_000
        multimodal = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this screenshot"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + payload},
                },
            ],
        }
        self.assertLess(estimate_message_tokens(multimodal), 1_000)

        client = SummaryClient("image task summary")
        compactor = ConversationHistoryCompactor(client, retain_recent_rounds=1)
        compacted = compactor.compact(
            [
                {"role": "system", "content": "rules"},
                multimodal,
                {"role": "assistant", "content": "inspected"},
                {"role": "user", "content": "continue"},
            ]
        )

        summary_prompt = str(client.requests[0][-1]["content"])
        self.assertNotIn(payload, summary_prompt)
        self.assertIn("image attachment", summary_prompt)
        self.assertIn("image task summary", compacted[1]["content"])

    def test_compactor_caches_an_unchanged_old_prefix(self) -> None:
        client = SummaryClient("cached summary")
        compactor = ConversationHistoryCompactor(client, retain_recent_rounds=1)
        history = [{"role": "system", "content": "rules"}]
        history.extend(round_messages(0))
        history.extend(round_messages(1))

        first = compactor.compact(history)
        second = compactor.compact(history)

        self.assertEqual(first, second)
        self.assertEqual(1, compactor.summary_calls)
        self.assertEqual(1, len(client.requests))

    def test_summary_failure_falls_back_without_breaking_context(self) -> None:
        client = FailingSummaryClient()
        compactor = ConversationHistoryCompactor(client, retain_recent_rounds=1)
        history = [{"role": "system", "content": "rules"}]
        history.extend(round_messages(0))
        history.extend(round_messages(1))

        compacted = compactor.compact(history)
        cached = compactor.compact(history)

        self.assertTrue(compactor.last_used_fallback)
        self.assertEqual(compacted, cached)
        self.assertEqual(1, len(client.requests))
        self.assertIn("user round 0", compacted[1]["content"])
        self.assertEqual("user round 1", compacted[2]["content"])

    def test_shared_long_term_memory_serializes_threaded_appends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            long_term = LongTermMemory(Path(directory, "memory.jsonl"))
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(long_term.save, f"fact {index}", ("fact",))
                    for index in range(20)
                ]
                for future in futures:
                    future.result(timeout=1)

            self.assertEqual(20, len(long_term.entries()))

    def test_long_term_memory_injection_has_an_independent_token_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            long_term = LongTermMemory(Path(directory, "memory.jsonl"))
            long_term.save("database " + "SQLite " * 200, ("database",))
            manager = MemoryManager(
                max_tokens=100_000,
                long_term=long_term,
                long_term_context_tokens=40,
            )

            prepared = manager.prepare(
                [
                    {"role": "system", "content": "rules"},
                    {"role": "user", "content": "Which database is used?"},
                ]
            )

            memory_message = next(
                message
                for message in prepared
                if str(message.get("content", "")).startswith("Relevant memory")
            )
            self.assertLessEqual(
                estimate_tokens(str(memory_message["content"])),
                40,
            )

    def test_long_window_still_injects_relevant_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            long_term = LongTermMemory(Path(directory, "memory.jsonl"))
            long_term.save("The project database is SQLite", ("database",))
            manager = MemoryManager(max_tokens=100_000, long_term=long_term)
            settings = ContextSettings.for_model(256_000)
            controller = ContextController(settings)

            prepared = controller.prepare(
                [
                    {"role": "system", "content": "rules"},
                    {"role": "user", "content": "Which database is used?"},
                ],
                manager,
            )

            self.assertTrue(
                any("SQLite" in str(message.get("content")) for message in prepared)
            )

    def test_default_bootstrap_wires_context_memory_tool_and_lsp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = SummaryClient()
            runtime = build_react_runtime(
                client,
                root,
                memory_path=root / "memory.jsonl",
            )

            self.assertIs(runtime.agent.memory, runtime.memory)
            self.assertIs(runtime.agent.context, runtime.context)
            self.assertIsNotNone(runtime.agent.lsp)
            self.assertIn("save_memory", runtime.tools.names())
            self.assertEqual(
                runtime.settings.compression_trigger_tokens,
                runtime.memory.max_tokens,  # type: ignore[union-attr]
            )

            result = runtime.tools.execute_result(
                "save_memory",
                '{"content":"Use SQLite","tags":["database"]}',
            )
            self.assertTrue(result.ok)
            self.assertEqual(1, len(runtime.long_term_memory.entries()))  # type: ignore[union-attr]

    def test_bootstrap_can_explicitly_disable_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_react_runtime(
                SummaryClient(),
                directory,
                enable_memory=False,
            )

            self.assertIsNone(runtime.memory)
            self.assertIsNone(runtime.long_term_memory)
            self.assertNotIn("save_memory", runtime.tools.names())
            self.assertIsNotNone(runtime.context)

    def test_model_switch_updates_context_and_summary_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = SummaryClient()
            runtime = build_react_runtime(
                first,
                directory,
                memory_path=Path(directory, "memory.jsonl"),
            )
            second = SummaryClient()
            second.context_window = 256_000
            second.supports_prompt_caching = True

            runtime.agent.set_client(second)

            self.assertEqual(256_000, runtime.agent.context.settings.window)  # type: ignore[union-attr]
            self.assertEqual(
                runtime.agent.context.settings.compression_trigger_tokens,  # type: ignore[union-attr]
                runtime.agent.memory.max_tokens,  # type: ignore[union-attr]
            )
            self.assertIs(
                second,
                runtime.agent.memory.history_compactor.client,  # type: ignore[union-attr]
            )

    def _assert_no_orphan_tool_results(
        self,
        messages: list[dict[str, Any]],
    ) -> None:
        known_calls: set[str] = set()
        for message in messages:
            for call in message.get("tool_calls") or []:
                known_calls.add(str(call["id"]))
            if message.get("role") == "tool":
                self.assertIn(str(message.get("tool_call_id")), known_calls)


if __name__ == "__main__":
    unittest.main()
