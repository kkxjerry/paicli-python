from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.context import TokenUsage
from paicli.llm_client import ChatResponse, ToolCall
from paicli.observability import (
    ModelPricing,
    ObservedLlmClient,
    ObservedToolGateway,
    PricingCatalog,
    TraceStore,
    trace_scope,
)
from paicli.tools import ToolRegistry


class FakeClient:
    provider = "test"
    model = "priced-model"
    context_window = 32_000
    supports_prompt_caching = False

    def chat(self, messages, tools):
        del messages, tools
        return ChatResponse(
            "done",
            (ToolCall("call-1", "read_file", '{"path":"a.txt"}'),),
            input_tokens=100,
            output_tokens=20,
            cached_input_tokens=25,
        )


class ObservabilityTest(unittest.TestCase):
    def test_trace_store_records_model_tool_usage_cost_and_latency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("evidence", encoding="utf-8")
            store = TraceStore(root / "trace.db")
            run_id = store.start_run(
                mode="react",
                goal="read a file",
                provider="test",
                model="priced-model",
            )
            prices = PricingCatalog(
                {"priced-model": ModelPricing(2.0, 4.0, 0.5, "test")}
            )
            client = ObservedLlmClient(FakeClient(), store, pricing=prices)
            tools = ObservedToolGateway(ToolRegistry(root), store)

            with trace_scope(
                run_id=run_id,
                agent_role="react",
                agent_name="main",
            ):
                response = client.chat([{"role": "user", "content": "read"}], [])
                result = tools.execute_result("read_file", '{"path":"a.txt"}')

            self.assertEqual("done", response.content)
            self.assertTrue(result.ok)
            store.finish_run(run_id, status="succeeded")
            summary = store.summary(run_id)
            run = store.run(run_id)

            self.assertEqual(1, summary["model_calls"])
            self.assertEqual(1, summary["tool_calls"])
            self.assertEqual(100, summary["input_tokens"])
            self.assertEqual(20, summary["output_tokens"])
            self.assertEqual(25, summary["cached_input_tokens"])
            expected = ((75 * 2.0) + (25 * 0.5) + (20 * 4.0)) / 1_000_000
            self.assertAlmostEqual(expected, summary["estimated_cost_cny"])
            self.assertEqual("succeeded", run["status"])
            self.assertEqual(1, run["model_calls"])
            self.assertEqual(1, run["tool_calls"])
            store.close()

    def test_failed_model_call_is_persisted_and_secret_is_redacted(self) -> None:
        class RaisingClient(FakeClient):
            def chat(self, messages, tools):
                del messages, tools
                raise RuntimeError("Bearer secret-value must not leak")

        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "trace.db")
            run_id = store.start_run(
                mode="team",
                goal="fail",
                provider="test",
                model="priced-model",
            )
            client = ObservedLlmClient(RaisingClient(), store)

            with trace_scope(run_id=run_id, agent_role="worker"):
                with self.assertRaises(RuntimeError):
                    client.chat([], [])
            store.finish_run(run_id, status="failed", error="expected")

            summary = store.summary(run_id)
            self.assertEqual(1, summary["model_errors"])
            row = store._connection.execute(  # white-box security assertion
                "SELECT error FROM model_calls WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self.assertNotIn("secret-value", row["error"])
            self.assertIn("Bearer ***", row["error"])
            store.close()

    def test_trace_scope_propagates_parent_and_task_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "trace.db")
            run_id = store.start_run(
                mode="plan",
                goal="g",
                provider="p",
                model="m",
            )
            with trace_scope(run_id=run_id, agent_role="planner", agent_name="p"):
                parent = store.start_span("agent", "planner")
                with trace_scope(
                    span_id=parent,
                    task_id="task_1",
                    agent_role="worker",
                    agent_name="worker-1",
                ):
                    child = store.start_span("agent", "worker")
                    store.finish_span(child, status="ok")
                store.finish_span(parent, status="ok")

            rows = store._connection.execute(
                "SELECT * FROM spans ORDER BY started_at"
            ).fetchall()
            self.assertEqual(2, len(rows))
            self.assertEqual(parent, rows[1]["parent_span_id"])
            self.assertEqual("task_1", rows[1]["task_id"])
            self.assertEqual("worker", rows[1]["agent_role"])
            store.close()

    def test_pricing_catalog_uses_versioned_default(self) -> None:
        price = PricingCatalog().price_for("qwen-plus")
        self.assertIsNotNone(price)
        self.assertEqual("aliyun-2026-08-30", price.source)  # type: ignore[union-attr]
        self.assertGreater(
            price.estimate(TokenUsage(1_000_000, 1_000_000, 0)),  # type: ignore[union-attr]
            0,
        )


if __name__ == "__main__":
    unittest.main()
