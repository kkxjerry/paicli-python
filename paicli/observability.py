"""Structured tracing, metrics, configurable pricing, and parent run budgets."""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .llm_client import ChatResponse, LlmClient
from .tool_contracts import ToolResult


@dataclass(frozen=True)
class TraceContext:
    run_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    task_id: str = ""
    agent_role: str = ""
    agent_name: str = ""


_TRACE_CONTEXT: contextvars.ContextVar[TraceContext] = contextvars.ContextVar(
    "paicli_trace_context",
    default=TraceContext(),
)
_RUN_BUDGET: contextvars.ContextVar[RunBudget | None]


@dataclass(frozen=True)
class ModelPricing:
    """CNY per one million tokens; configured, never silently guessed."""

    input_cny_per_million: float
    output_cny_per_million: float
    cached_input_cny_per_million: float = 0.0

    def __post_init__(self) -> None:
        if min(
            self.input_cny_per_million,
            self.output_cny_per_million,
            self.cached_input_cny_per_million,
        ) < 0:
            raise ValueError("model prices cannot be negative")

    def estimate(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> float:
        cached = min(max(0, cached_input_tokens), max(0, input_tokens))
        uncached = max(0, input_tokens) - cached
        return (
            uncached * self.input_cny_per_million
            + cached * self.cached_input_cny_per_million
            + max(0, output_tokens) * self.output_cny_per_million
        ) / 1_000_000


class PricingCatalog:
    """Provider/model pricing loaded from configuration.

    Environment convention, after normalizing provider/model to upper-case and
    replacing punctuation with underscores::

        PAICLI_PRICE_DASHSCOPE_QWEN_PLUS_INPUT_CNY_PER_MILLION=...
        PAICLI_PRICE_DASHSCOPE_QWEN_PLUS_OUTPUT_CNY_PER_MILLION=...
        PAICLI_PRICE_DASHSCOPE_QWEN_PLUS_CACHED_CNY_PER_MILLION=...

    No price means the call remains explicitly unpriced rather than reported as
    zero-cost.
    """

    def __init__(
        self,
        values: Mapping[tuple[str, str], ModelPricing] | None = None,
    ) -> None:
        self._values = {
            (provider.lower(), model.lower()): pricing
            for (provider, model), pricing in (values or {}).items()
        }

    def get(self, provider: str, model: str) -> ModelPricing | None:
        exact = self._values.get((provider.lower(), model.lower()))
        if exact is not None:
            return exact
        return self._values.get((provider.lower(), "*"))

    @classmethod
    def from_env(
        cls,
        provider: str,
        model: str,
        environ: Mapping[str, str] | None = None,
    ) -> PricingCatalog:
        env = os.environ if environ is None else environ
        prefix = "PAICLI_PRICE_" + _env_key(provider) + "_" + _env_key(model)
        input_raw = env.get(prefix + "_INPUT_CNY_PER_MILLION", "").strip()
        output_raw = env.get(prefix + "_OUTPUT_CNY_PER_MILLION", "").strip()
        cached_raw = env.get(prefix + "_CACHED_CNY_PER_MILLION", "0").strip()
        if not input_raw and not output_raw:
            return cls()
        if not input_raw or not output_raw:
            raise ValueError(
                f"both {prefix}_INPUT_CNY_PER_MILLION and "
                f"{prefix}_OUTPUT_CNY_PER_MILLION are required"
            )
        try:
            pricing = ModelPricing(
                float(input_raw),
                float(output_raw),
                float(cached_raw or "0"),
            )
        except ValueError as exc:
            raise ValueError(f"invalid pricing configuration for {provider}/{model}") from exc
        return cls({(provider, model): pricing})


class TraceStore:
    """SQLite-backed append-oriented trace and metric store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                prompt TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS spans (
                span_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                parent_span_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                agent_role TEXT NOT NULL DEFAULT '',
                agent_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                attributes_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_spans_run ON spans(run_id, started_at);
            CREATE TABLE IF NOT EXISTS model_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                span_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                agent_role TEXT NOT NULL DEFAULT '',
                agent_name TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                duration_ms INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_cny REAL,
                priced INTEGER NOT NULL DEFAULT 0,
                tool_schema_count INTEGER NOT NULL DEFAULT 0,
                response_tool_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_model_calls_run ON model_calls(run_id, id);
            CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                span_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                agent_role TEXT NOT NULL DEFAULT '',
                agent_name TEXT NOT NULL DEFAULT '',
                call_id TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                duration_ms INTEGER NOT NULL,
                error_type TEXT NOT NULL DEFAULT '',
                retryable INTEGER NOT NULL DEFAULT 0,
                timed_out INTEGER NOT NULL DEFAULT 0,
                changed_files_json TEXT NOT NULL DEFAULT '[]',
                arguments_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, id);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                span_id TEXT NOT NULL DEFAULT '',
                timestamp REAL NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                data_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
            """
        )

    def start_run(
        self,
        mode: str,
        prompt: str,
        *,
        provider: str = "",
        model: str = "",
        metadata: Mapping[str, object] | None = None,
        run_id: str | None = None,
    ) -> str:
        value = run_id or ("run_" + uuid.uuid4().hex)
        with self._lock:
            self._connection.execute(
                """INSERT INTO runs
                   (run_id, mode, prompt, provider, model, status, started_at,
                    metadata_json)
                   VALUES (?, ?, ?, ?, ?, 'running', ?, ?)""",
                (
                    value,
                    str(mode),
                    redact_text(prompt),
                    str(provider),
                    str(model),
                    time.time(),
                    _json(metadata or {}),
                ),
            )
        return value

    def resume_run(self, run_id: str) -> None:
        with self._lock:
            updated = self._connection.execute(
                """UPDATE runs SET status='running', finished_at=NULL, error=''
                   WHERE run_id=?""",
                (run_id,),
            ).rowcount
        if not updated:
            raise KeyError(f"unknown trace run: {run_id}")

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        with self._lock:
            row = self._connection.execute(
                "SELECT metadata_json FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                return
            merged = json.loads(row["metadata_json"] or "{}")
            merged.update(metadata or {})
            self._connection.execute(
                """UPDATE runs
                   SET status=?, finished_at=?, error=?, metadata_json=?
                   WHERE run_id=?""",
                (
                    str(status),
                    time.time(),
                    redact_text(error),
                    _json(merged),
                    run_id,
                ),
            )

    def start_span(
        self,
        kind: str,
        name: str,
        *,
        run_id: str,
        parent_span_id: str = "",
        task_id: str = "",
        agent_role: str = "",
        agent_name: str = "",
        attributes: Mapping[str, object] | None = None,
    ) -> str:
        span_id = "span_" + uuid.uuid4().hex
        with self._lock:
            self._connection.execute(
                """INSERT INTO spans
                   (span_id, run_id, parent_span_id, kind, name, task_id,
                    agent_role, agent_name, status, started_at, attributes_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                (
                    span_id,
                    run_id,
                    parent_span_id,
                    str(kind),
                    str(name),
                    str(task_id),
                    str(agent_role),
                    str(agent_name),
                    time.time(),
                    _json(attributes or {}),
                ),
            )
        return span_id

    def finish_span(
        self,
        span_id: str,
        *,
        status: str = "succeeded",
        error: str = "",
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        finished = time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT started_at, attributes_json FROM spans WHERE span_id=?",
                (span_id,),
            ).fetchone()
            if row is None:
                return
            merged = json.loads(row["attributes_json"] or "{}")
            merged.update(attributes or {})
            duration = max(0, int((finished - float(row["started_at"])) * 1000))
            self._connection.execute(
                """UPDATE spans SET status=?, finished_at=?, duration_ms=?,
                   error=?, attributes_json=? WHERE span_id=?""",
                (
                    str(status),
                    finished,
                    duration,
                    redact_text(error),
                    _json(merged),
                    span_id,
                ),
            )

    def record_model_call(
        self,
        *,
        context: TraceContext,
        provider: str,
        model: str,
        status: str,
        started_at: float,
        duration_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        estimated_cost_cny: float | None = None,
        tool_schema_count: int = 0,
        response_tool_count: int = 0,
        error: str = "",
    ) -> None:
        if not context.run_id:
            return
        with self._lock:
            self._connection.execute(
                """INSERT INTO model_calls
                   (run_id, span_id, task_id, agent_role, agent_name, provider,
                    model, status, started_at, duration_ms, input_tokens,
                    output_tokens, cached_input_tokens, estimated_cost_cny,
                    priced, tool_schema_count, response_tool_count, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    context.run_id,
                    context.span_id,
                    context.task_id,
                    context.agent_role,
                    context.agent_name,
                    provider,
                    model,
                    status,
                    started_at,
                    duration_ms,
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0, cached_input_tokens),
                    estimated_cost_cny,
                    1 if estimated_cost_cny is not None else 0,
                    max(0, tool_schema_count),
                    max(0, response_tool_count),
                    redact_text(error),
                ),
            )

    def record_tool_call(
        self,
        result: ToolResult,
        *,
        context: TraceContext,
        arguments_json: str = "{}",
        started_at: float | None = None,
    ) -> None:
        if not context.run_id:
            return
        with self._lock:
            self._connection.execute(
                """INSERT INTO tool_calls
                   (run_id, span_id, task_id, agent_role, agent_name, call_id,
                    tool_name, status, started_at, duration_ms, error_type,
                    retryable, timed_out, changed_files_json, arguments_json,
                    error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    context.run_id,
                    context.span_id,
                    context.task_id,
                    context.agent_role,
                    context.agent_name,
                    result.call_id,
                    result.tool_name,
                    "succeeded" if result.ok else "failed",
                    started_at or time.time(),
                    max(0, result.elapsed_ms),
                    result.error_type.value if result.error_type else "",
                    1 if result.retryable else 0,
                    1 if result.timed_out else 0,
                    _json(result.changed_files),
                    redact_json(arguments_json),
                    "" if result.ok else redact_text(result.content),
                ),
            )

    def event(
        self,
        kind: str,
        message: str,
        *,
        context: TraceContext | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        ctx = context or current_trace_context()
        if not ctx.run_id:
            return
        with self._lock:
            self._connection.execute(
                """INSERT INTO events
                   (run_id, span_id, timestamp, kind, message, data_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    ctx.run_id,
                    ctx.span_id,
                    time.time(),
                    str(kind),
                    redact_text(message),
                    _json(data or {}),
                ),
            )

    def run_summary(self, run_id: str) -> dict[str, object]:
        with self._lock:
            run = self._connection.execute(
                "SELECT * FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown trace run: {run_id}")
            model = self._connection.execute(
                """SELECT COUNT(*) calls,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) errors,
                          COALESCE(SUM(input_tokens),0) input_tokens,
                          COALESCE(SUM(output_tokens),0) output_tokens,
                          COALESCE(SUM(cached_input_tokens),0) cached_input_tokens,
                          COALESCE(SUM(estimated_cost_cny),0) estimated_cost_cny,
                          SUM(CASE WHEN priced=1 THEN 1 ELSE 0 END) priced_calls,
                          SUM(CASE WHEN priced=0 THEN 1 ELSE 0 END) unpriced_calls,
                          COALESCE(SUM(duration_ms),0) duration_ms
                   FROM model_calls WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            tool = self._connection.execute(
                """SELECT COUNT(*) calls,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) errors,
                          SUM(CASE WHEN timed_out=1 THEN 1 ELSE 0 END) timeouts,
                          COALESCE(SUM(duration_ms),0) duration_ms
                   FROM tool_calls WHERE run_id=?""",
                (run_id,),
            ).fetchone()
        started = float(run["started_at"])
        finished = float(run["finished_at"] or time.time())
        return {
            "run_id": run_id,
            "mode": run["mode"],
            "status": run["status"],
            "provider": run["provider"],
            "model": run["model"],
            "elapsed_ms": max(0, int((finished - started) * 1000)),
            "model_calls": int(model["calls"] or 0),
            "model_errors": int(model["errors"] or 0),
            "input_tokens": int(model["input_tokens"] or 0),
            "output_tokens": int(model["output_tokens"] or 0),
            "cached_input_tokens": int(model["cached_input_tokens"] or 0),
            "estimated_cost_cny": float(model["estimated_cost_cny"] or 0.0),
            "priced_model_calls": int(model["priced_calls"] or 0),
            "unpriced_model_calls": int(model["unpriced_calls"] or 0),
            "model_duration_ms": int(model["duration_ms"] or 0),
            "tool_calls": int(tool["calls"] or 0),
            "tool_errors": int(tool["errors"] or 0),
            "tool_timeouts": int(tool["timeouts"] or 0),
            "tool_duration_ms": int(tool["duration_ms"] or 0),
            "error": run["error"],
            "metadata": json.loads(run["metadata_json"] or "{}"),
        }

    summary = run_summary

    def list_runs(self, limit: int = 20) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self.run_summary(str(row["run_id"])) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class ObservedLlmClient:
    """Record every provider attempt and enforce the current parent budget."""

    def __init__(
        self,
        client: LlmClient,
        trace_store: TraceStore | None,
        pricing: PricingCatalog | None = None,
    ) -> None:
        self.client = client
        self.trace_store = trace_store
        self.pricing = pricing or PricingCatalog()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        budget = current_run_budget()
        if budget is not None:
            budget.before_model_call()
        started_perf = time.perf_counter()
        started_wall = time.time()
        context = current_trace_context()
        provider = str(getattr(self.client, "provider", "custom"))
        model = str(getattr(self.client, "model", "unknown"))
        try:
            response = self.client.chat(messages, tools)
        except Exception as exc:
            duration = max(0, int((time.perf_counter() - started_perf) * 1000))
            if budget is not None:
                budget.after_model_call(0, 0, 0, None, failed=True)
            if self.trace_store is not None:
                self.trace_store.record_model_call(
                    context=context,
                    provider=provider,
                    model=model,
                    status="failed",
                    started_at=started_wall,
                    duration_ms=duration,
                    tool_schema_count=len(tools),
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise

        duration = max(0, int((time.perf_counter() - started_perf) * 1000))
        model_pricing = self.pricing.get(provider, model)
        cost = (
            model_pricing.estimate(
                response.input_tokens,
                response.output_tokens,
                response.cached_input_tokens,
            )
            if model_pricing is not None
            else None
        )
        if budget is not None:
            budget.after_model_call(
                response.input_tokens,
                response.output_tokens,
                response.cached_input_tokens,
                cost,
                failed=False,
            )
        if self.trace_store is not None:
            self.trace_store.record_model_call(
                context=context,
                provider=provider,
                model=model,
                status="succeeded",
                started_at=started_wall,
                duration_ms=duration,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cached_input_tokens=response.cached_input_tokens,
                estimated_cost_cny=cost,
                tool_schema_count=len(tools),
                response_tool_count=len(response.tool_calls),
            )
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


class ObservedToolGateway:
    """Transparent ToolRuntime decorator for parent budgets and trace rows."""

    def __init__(self, gateway: Any, trace_store: TraceStore | None) -> None:
        self.gateway = gateway
        self.trace_store = trace_store

    def definitions(self) -> list[dict[str, Any]]:
        return self.gateway.definitions()

    def names(self) -> list[str]:
        return self.gateway.names()

    def spec(self, name: str) -> Any:
        return self.gateway.spec(name)

    def validate_arguments(self, name: str, arguments_json: str) -> dict[str, Any]:
        return self.gateway.validate_arguments(name, arguments_json)

    def execute(self, name: str, arguments_json: str) -> str:
        return self.execute_result(name, arguments_json).content

    def execute_result(self, name: str, arguments_json: str) -> ToolResult:
        return self.execute_many_results([(name, arguments_json)])[0]

    def execute_many(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> list[str]:
        return [
            result.content
            for result in self.execute_many_results(
                calls,
                timeout_seconds=timeout_seconds,
            )
        ]

    def execute_many_results(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> list[ToolResult]:
        budget = current_run_budget()
        for _name, _arguments in calls:
            if budget is not None:
                budget.before_tool_call()
        started = [time.time() for _ in calls]
        results = self.gateway.execute_many_results(
            calls,
            timeout_seconds=timeout_seconds,
        )
        if len(results) != len(calls):
            raise RuntimeError("observed tool gateway received mismatched results")
        context = current_trace_context()
        for (name, arguments), result, started_at in zip(
            calls,
            results,
            started,
            strict=True,
        ):
            if budget is not None:
                budget.after_tool_call(result.ok)
            if self.trace_store is not None:
                self.trace_store.record_tool_call(
                    result,
                    context=context,
                    arguments_json=arguments,
                    started_at=started_at,
                )
        return list(results)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.gateway, name)


@dataclass(frozen=True)
class RunLimits:
    max_tokens: int | None = None
    max_cost_cny: float | None = None
    max_seconds: float | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_tokens", "max_model_calls", "max_tool_calls"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive")
        for name in ("max_cost_cny", "max_seconds"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")


class RunBudgetExceeded(RuntimeError):
    def __init__(self, reason: str, snapshot: Mapping[str, object]) -> None:
        self.reason = reason
        self.snapshot = dict(snapshot)
        super().__init__(f"{reason}: {self.snapshot}")


class RunBudget:
    """Thread-safe aggregate budget shared by planner/workers/reviewers."""

    def __init__(self, limits: RunLimits) -> None:
        self.limits = limits
        self.started_at = time.monotonic()
        self.model_calls = 0
        self.tool_calls = 0
        self.model_errors = 0
        self.tool_errors = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_input_tokens = 0
        self.estimated_cost_cny = 0.0
        self.unpriced_model_calls = 0
        self._lock = threading.RLock()

    def before_model_call(self) -> None:
        with self._lock:
            self._check_time()
            next_value = self.model_calls + 1
            if (
                self.limits.max_model_calls is not None
                and next_value > self.limits.max_model_calls
            ):
                self._raise("model_call_budget_exceeded")
            self.model_calls = next_value

    def after_model_call(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
        cost_cny: float | None,
        *,
        failed: bool,
    ) -> None:
        with self._lock:
            self.input_tokens += max(0, input_tokens)
            self.output_tokens += max(0, output_tokens)
            self.cached_input_tokens += max(0, cached_input_tokens)
            if cost_cny is None:
                self.unpriced_model_calls += 1
            else:
                self.estimated_cost_cny += max(0.0, cost_cny)
            if failed:
                self.model_errors += 1
            self._check_all()

    def before_tool_call(self) -> None:
        with self._lock:
            self._check_time()
            next_value = self.tool_calls + 1
            if (
                self.limits.max_tool_calls is not None
                and next_value > self.limits.max_tool_calls
            ):
                self._raise("tool_call_budget_exceeded")
            self.tool_calls = next_value

    def after_tool_call(self, ok: bool) -> None:
        with self._lock:
            if not ok:
                self.tool_errors += 1
            self._check_all()

    def check(self) -> None:
        with self._lock:
            self._check_all()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "elapsed_seconds": round(time.monotonic() - self.started_at, 6),
                "model_calls": self.model_calls,
                "tool_calls": self.tool_calls,
                "model_errors": self.model_errors,
                "tool_errors": self.tool_errors,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cached_input_tokens": self.cached_input_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "estimated_cost_cny": round(self.estimated_cost_cny, 8),
                "unpriced_model_calls": self.unpriced_model_calls,
                "limits": asdict(self.limits),
            }

    def _check_all(self) -> None:
        self._check_time()
        if (
            self.limits.max_tokens is not None
            and self.input_tokens + self.output_tokens > self.limits.max_tokens
        ):
            self._raise("token_budget_exceeded")
        if (
            self.limits.max_cost_cny is not None
            and self.estimated_cost_cny > self.limits.max_cost_cny
        ):
            self._raise("cost_budget_exceeded")

    def _check_time(self) -> None:
        if (
            self.limits.max_seconds is not None
            and time.monotonic() - self.started_at > self.limits.max_seconds
        ):
            self._raise("wall_clock_budget_exceeded")

    def _raise(self, reason: str) -> None:
        raise RunBudgetExceeded(reason, self.snapshot())


_RUN_BUDGET = contextvars.ContextVar("paicli_run_budget", default=None)


def current_trace_context() -> TraceContext:
    return _TRACE_CONTEXT.get()


def current_run_budget() -> RunBudget | None:
    return _RUN_BUDGET.get()


@contextlib.contextmanager
def run_budget_scope(budget: RunBudget | None) -> Iterator[None]:
    token = _RUN_BUDGET.set(budget)
    try:
        yield
    finally:
        _RUN_BUDGET.reset(token)


@contextlib.contextmanager
def trace_scope(
    *,
    run_id: str | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    task_id: str | None = None,
    agent_role: str | None = None,
    agent_name: str | None = None,
) -> Iterator[TraceContext]:
    previous = current_trace_context()
    value = TraceContext(
        run_id=previous.run_id if run_id is None else run_id,
        span_id=previous.span_id if span_id is None else span_id,
        parent_span_id=(
            previous.parent_span_id
            if parent_span_id is None
            else parent_span_id
        ),
        task_id=previous.task_id if task_id is None else task_id,
        agent_role=previous.agent_role if agent_role is None else agent_role,
        agent_name=previous.agent_name if agent_name is None else agent_name,
    )
    token = _TRACE_CONTEXT.set(value)
    try:
        yield value
    finally:
        _TRACE_CONTEXT.reset(token)


@contextlib.contextmanager
def traced_span(
    store: TraceStore | None,
    kind: str,
    name: str,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    agent_role: str | None = None,
    agent_name: str | None = None,
    attributes: Mapping[str, object] | None = None,
) -> Iterator[str]:
    parent = current_trace_context()
    effective_run = run_id or parent.run_id
    if store is None or not effective_run:
        with trace_scope(
            run_id=effective_run,
            task_id=task_id,
            agent_role=agent_role,
            agent_name=agent_name,
        ):
            yield ""
        return

    span_id = store.start_span(
        kind,
        name,
        run_id=effective_run,
        parent_span_id=parent.span_id,
        task_id=task_id or parent.task_id,
        agent_role=agent_role or parent.agent_role,
        agent_name=agent_name or parent.agent_name,
        attributes=attributes,
    )
    try:
        with trace_scope(
            run_id=effective_run,
            span_id=span_id,
            parent_span_id=parent.span_id,
            task_id=task_id,
            agent_role=agent_role,
            agent_name=agent_name,
        ):
            yield span_id
    except Exception as exc:
        store.finish_span(
            span_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        store.finish_span(span_id, status="succeeded")


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,}\"']+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def redact_text(value: object) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", text)
    return text


def redact_json(value: str) -> str:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return redact_text(value)
    return _json(_redact_object(parsed))


def _redact_object(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if re.search(r"(?i)api[_-]?key|token|secret|password|authorization", str(key)):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = _redact_object(item)
        return result
    if isinstance(value, list):
        return [_redact_object(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _env_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


__all__ = [
    "ModelPricing",
    "ObservedLlmClient",
    "ObservedToolGateway",
    "PricingCatalog",
    "RunBudget",
    "RunBudgetExceeded",
    "RunLimits",
    "TraceContext",
    "TraceStore",
    "current_run_budget",
    "current_trace_context",
    "redact_json",
    "redact_text",
    "run_budget_scope",
    "trace_scope",
    "traced_span",
]
