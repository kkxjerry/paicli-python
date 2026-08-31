"""Durable run/checkpoint state for safe process-interruption recovery."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .agents.models import AgentOutcome, FinishReason, RunStatus
from .context import TokenUsage
from .orchestration import TaskRunRecord
from .planning import ExecutionPlan, Task, TaskStatus, TaskType
from .review import ReviewResult, ReviewRun, ReviewVerdict
from .subagents import ToolScope
from .tool_contracts import (
    ResourceAccess,
    ResourceMode,
    ToolErrorType,
    ToolResult,
)


class StoredRunStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    TERMINAL = {SUCCEEDED, PARTIAL, FAILED, CANCELLED}


@dataclass(frozen=True)
class StoredRun:
    run_id: str
    mode: str
    goal: str
    prompt: str
    status: str
    before_snapshot_id: str = ""
    after_snapshot_id: str = ""
    current_task_id: str = ""
    current_task_snapshot_id: str = ""
    plan: ExecutionPlan | None = None
    records: dict[str, TaskRunRecord] = field(default_factory=dict)
    answer: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def resumable(self) -> bool:
        return self.status in {
            StoredRunStatus.INTERRUPTED,
            StoredRunStatus.FAILED,
            StoredRunStatus.PARTIAL,
        }


class RunStateStore:
    """SQLite run state with append-only checkpoints and atomic current state."""

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
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    before_snapshot_id TEXT NOT NULL DEFAULT '',
                    after_snapshot_id TEXT NOT NULL DEFAULT '',
                    current_task_id TEXT NOT NULL DEFAULT '',
                    current_task_snapshot_id TEXT NOT NULL DEFAULT '',
                    plan_json TEXT NOT NULL DEFAULT '',
                    records_json TEXT NOT NULL DEFAULT '{}',
                    answer TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_updated
                  ON runs(updated_at DESC);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    plan_json TEXT NOT NULL DEFAULT '',
                    records_json TEXT NOT NULL DEFAULT '{}',
                    current_task_id TEXT NOT NULL DEFAULT '',
                    current_task_snapshot_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(run_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_run
                  ON checkpoints(run_id, sequence);
                """
            )

    def create(
        self,
        *,
        run_id: str,
        mode: str,
        goal: str,
        prompt: str,
        before_snapshot_id: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> StoredRun:
        now = time.time()
        with self._lock:
            self._connection.execute(
                """INSERT INTO runs
                   (run_id, mode, goal, prompt, status, before_snapshot_id,
                    metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    mode,
                    goal,
                    prompt,
                    StoredRunStatus.RUNNING,
                    before_snapshot_id,
                    _json(metadata or {}),
                    now,
                    now,
                ),
            )
        return self.load(run_id)

    def checkpoint(
        self,
        run_id: str,
        *,
        phase: str,
        plan: ExecutionPlan | None,
        records: Mapping[str, TaskRunRecord],
        current_task_id: str = "",
        current_task_snapshot_id: str = "",
    ) -> int:
        plan_json = _json(plan_to_dict(plan)) if plan is not None else ""
        records_json = _json(records_to_dict(records))
        now = time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 next_sequence "
                "FROM checkpoints WHERE run_id=?",
                (run_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """INSERT INTO checkpoints
                       (run_id, sequence, phase, plan_json, records_json,
                        current_task_id, current_task_snapshot_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        sequence,
                        phase,
                        plan_json,
                        records_json,
                        current_task_id,
                        current_task_snapshot_id,
                        now,
                    ),
                )
                updated = self._connection.execute(
                    """UPDATE runs SET plan_json=?, records_json=?,
                       current_task_id=?, current_task_snapshot_id=?,
                       updated_at=? WHERE run_id=?""",
                    (
                        plan_json,
                        records_json,
                        current_task_id,
                        current_task_snapshot_id,
                        now,
                        run_id,
                    ),
                ).rowcount
                if not updated:
                    raise KeyError(f"unknown run: {run_id}")
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return sequence

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        answer: str = "",
        error: str = "",
        plan: ExecutionPlan | None = None,
        records: Mapping[str, TaskRunRecord] | None = None,
        after_snapshot_id: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        if status not in {
            StoredRunStatus.SUCCEEDED,
            StoredRunStatus.PARTIAL,
            StoredRunStatus.FAILED,
            StoredRunStatus.CANCELLED,
            StoredRunStatus.INTERRUPTED,
        }:
            raise ValueError(f"invalid terminal run status: {status}")
        now = time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT metadata_json, plan_json, records_json FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            merged = json.loads(row["metadata_json"] or "{}")
            merged.update(metadata or {})
            plan_json = (
                _json(plan_to_dict(plan)) if plan is not None else row["plan_json"]
            )
            records_json = (
                _json(records_to_dict(records or {}))
                if records is not None
                else row["records_json"]
            )
            self._connection.execute(
                """UPDATE runs SET status=?, answer=?, error=?, plan_json=?,
                   records_json=?, after_snapshot_id=?, current_task_id='',
                   current_task_snapshot_id='', metadata_json=?, updated_at=?
                   WHERE run_id=?""",
                (
                    status,
                    answer,
                    error,
                    plan_json,
                    records_json,
                    after_snapshot_id,
                    _json(merged),
                    now,
                    run_id,
                ),
            )

    def mark_running(self, run_id: str) -> None:
        with self._lock:
            updated = self._connection.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
                (StoredRunStatus.RUNNING, time.time(), run_id),
            ).rowcount
        if not updated:
            raise KeyError(f"unknown run: {run_id}")

    def mark_stale_running_interrupted(self) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE runs SET status=?, updated_at=?
                   WHERE status=?""",
                (
                    StoredRunStatus.INTERRUPTED,
                    time.time(),
                    StoredRunStatus.RUNNING,
                ),
            )
            return int(cursor.rowcount)

    def load(self, run_id: str) -> StoredRun:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return _stored_run(row)

    def recent(self, limit: int = 20) -> list[StoredRun]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_stored_run(row) for row in rows]

    def resumable_runs(self) -> list[StoredRun]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM runs WHERE status IN (?, ?, ?)
                   ORDER BY updated_at DESC""",
                (
                    StoredRunStatus.INTERRUPTED,
                    StoredRunStatus.FAILED,
                    StoredRunStatus.PARTIAL,
                ),
            ).fetchall()
        return [_stored_run(row) for row in rows]

    def checkpoints(self, run_id: str) -> list[dict[str, object]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM checkpoints WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _stored_run(row: sqlite3.Row) -> StoredRun:
    plan_json = str(row["plan_json"] or "")
    return StoredRun(
        run_id=str(row["run_id"]),
        mode=str(row["mode"]),
        goal=str(row["goal"]),
        prompt=str(row["prompt"]),
        status=str(row["status"]),
        before_snapshot_id=str(row["before_snapshot_id"] or ""),
        after_snapshot_id=str(row["after_snapshot_id"] or ""),
        current_task_id=str(row["current_task_id"] or ""),
        current_task_snapshot_id=str(row["current_task_snapshot_id"] or ""),
        plan=plan_from_dict(json.loads(plan_json)) if plan_json else None,
        records=records_from_dict(json.loads(row["records_json"] or "{}")),
        answer=str(row["answer"] or ""),
        error=str(row["error"] or ""),
        metadata=json.loads(row["metadata_json"] or "{}"),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def plan_to_dict(plan: ExecutionPlan | None) -> dict[str, object] | None:
    if plan is None:
        return None
    return {
        "goal": plan.goal,
        "summary": plan.summary,
        "plan_id": plan.plan_id,
        "metadata": plan.metadata,
        "tasks": [
            {
                "id": task.id,
                "description": task.description,
                "dependencies": list(task.dependencies),
                "status": task.status.value,
                "result": task.result,
                "task_type": task.task_type.value,
                "acceptance_criteria": list(task.acceptance_criteria),
                "error": task.error,
                "execution_attempts": task.execution_attempts,
                "review_attempts": task.review_attempts,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
            }
            for task in plan.tasks
        ],
    }


def plan_from_dict(value: Mapping[str, object] | None) -> ExecutionPlan | None:
    if value is None:
        return None
    tasks: list[Task] = []
    for item in value.get("tasks", []):  # type: ignore[union-attr]
        task_data = dict(item)  # type: ignore[arg-type]
        tasks.append(
            Task(
                str(task_data["id"]),
                str(task_data["description"]),
                tuple(str(x) for x in task_data.get("dependencies", [])),
                TaskStatus(str(task_data.get("status", "pending"))),
                str(task_data.get("result", "")),
                TaskType(str(task_data.get("task_type", "ANALYSIS"))),
                tuple(str(x) for x in task_data.get("acceptance_criteria", [])),
                str(task_data.get("error", "")),
                int(task_data.get("execution_attempts", 0)),
                int(task_data.get("review_attempts", 0)),
                _optional_float(task_data.get("started_at")),
                _optional_float(task_data.get("finished_at")),
            )
        )
    return ExecutionPlan(
        str(value["goal"]),
        tasks,
        summary=str(value.get("summary", "")),
        plan_id=str(value.get("plan_id", "")),
        metadata=dict(value.get("metadata", {})),  # type: ignore[arg-type]
    )


def records_to_dict(
    records: Mapping[str, TaskRunRecord],
) -> dict[str, object]:
    return {
        task_id: {
            "task_id": record.task_id,
            "worker_name": record.worker_name,
            "tool_scope": record.tool_scope.value,
            "worker_outcomes": [
                outcome_to_dict(outcome) for outcome in record.worker_outcomes
            ],
            "reviews": [review_run_to_dict(review) for review in record.reviews],
        }
        for task_id, record in records.items()
    }


def records_from_dict(value: Mapping[str, object]) -> dict[str, TaskRunRecord]:
    result: dict[str, TaskRunRecord] = {}
    for task_id, raw in value.items():
        item = dict(raw)  # type: ignore[arg-type]
        result[str(task_id)] = TaskRunRecord(
            str(item.get("task_id", task_id)),
            str(item.get("worker_name", "")),
            ToolScope(str(item.get("tool_scope", "none"))),
            [
                outcome_from_dict(entry)
                for entry in item.get("worker_outcomes", [])
            ],
            [
                review_run_from_dict(entry)
                for entry in item.get("reviews", [])
            ],
        )
    return result


def outcome_to_dict(outcome: AgentOutcome) -> dict[str, object]:
    return {
        "run_id": outcome.run_id,
        "status": outcome.status.value,
        "finish_reason": outcome.finish_reason.value,
        "content": outcome.content,
        "usage": asdict(outcome.usage),
        "iterations": outcome.iterations,
        "error": outcome.error,
        "changed_files": list(outcome.changed_files),
        "tool_results": [tool_result_to_dict(item) for item in outcome.tool_results],
    }


def outcome_from_dict(value: Mapping[str, object]) -> AgentOutcome:
    usage = dict(value.get("usage", {}))  # type: ignore[arg-type]
    return AgentOutcome(
        run_id=str(value.get("run_id", "")),
        status=RunStatus(str(value.get("status", "failed"))),
        finish_reason=FinishReason(
            str(value.get("finish_reason", "internal_error"))
        ),
        content=str(value.get("content", "")),
        usage=TokenUsage(
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            int(usage.get("cached_input_tokens", 0)),
        ),
        iterations=int(value.get("iterations", 0)),
        error=str(value.get("error", "")),
        changed_files=tuple(str(x) for x in value.get("changed_files", [])),
        tool_results=tuple(
            tool_result_from_dict(entry)
            for entry in value.get("tool_results", [])
        ),
    )


def tool_result_to_dict(value: ToolResult) -> dict[str, object]:
    return {
        "tool_name": value.tool_name,
        "ok": value.ok,
        "content": value.content,
        "error_type": value.error_type.value if value.error_type else "",
        "retryable": value.retryable,
        "timed_out": value.timed_out,
        "elapsed_ms": value.elapsed_ms,
        "changed_files": list(value.changed_files),
        "accesses": [
            {
                "resource": item.resource,
                "mode": item.mode.value,
                "recursive": item.recursive,
            }
            for item in value.accesses
        ],
        "call_id": value.call_id,
    }


def tool_result_from_dict(value: Mapping[str, object]) -> ToolResult:
    error_type = str(value.get("error_type", ""))
    return ToolResult(
        tool_name=str(value.get("tool_name", "")),
        ok=bool(value.get("ok", False)),
        content=str(value.get("content", "")),
        error_type=ToolErrorType(error_type) if error_type else None,
        retryable=bool(value.get("retryable", False)),
        timed_out=bool(value.get("timed_out", False)),
        elapsed_ms=int(value.get("elapsed_ms", 0)),
        changed_files=tuple(str(x) for x in value.get("changed_files", [])),
        accesses=tuple(
            ResourceAccess(
                str(item["resource"]),
                ResourceMode(str(item["mode"])),
                bool(item.get("recursive", False)),
            )
            for item in value.get("accesses", [])  # type: ignore[union-attr]
        ),
        call_id=str(value.get("call_id", "")),
    )


def review_run_to_dict(value: ReviewRun) -> dict[str, object]:
    return {
        "result": {
            "verdict": value.result.verdict.value,
            "summary": value.result.summary,
            "issues": list(value.result.issues),
            "suggestions": list(value.result.suggestions),
            "evidence": list(value.result.evidence),
            "error": value.result.error,
            "retryable": value.result.retryable,
        },
        "model_outcomes": [
            outcome_to_dict(outcome) for outcome in value.model_outcomes
        ],
    }


def review_run_from_dict(value: Mapping[str, object]) -> ReviewRun:
    raw_result = dict(value.get("result", {}))  # type: ignore[arg-type]
    result = ReviewResult(
        ReviewVerdict(str(raw_result.get("verdict", "error"))),
        str(raw_result.get("summary", "")),
        tuple(str(x) for x in raw_result.get("issues", [])),
        tuple(str(x) for x in raw_result.get("suggestions", [])),
        tuple(str(x) for x in raw_result.get("evidence", [])),
        str(raw_result.get("error", "")),
        bool(raw_result.get("retryable", False)),
    )
    return ReviewRun(
        result,
        tuple(
            outcome_from_dict(entry)
            for entry in value.get("model_outcomes", [])
        ),
    )


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


__all__ = [
    "RunStateStore",
    "StoredRun",
    "StoredRunStatus",
    "outcome_from_dict",
    "outcome_to_dict",
    "plan_from_dict",
    "plan_to_dict",
    "records_from_dict",
    "records_to_dict",
]
