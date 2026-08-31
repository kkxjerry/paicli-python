"""Fixed-task evaluation and baseline/candidate comparison for PaiCLI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .bootstrap import build_application_runtime
from .execution import CoordinatedRun, RunCoordinator
from .llm_client import LlmClientFactory
from .observability import RunLimits
from .policy import ApprovalMode
from .safety import RollbackPolicy


@dataclass(frozen=True)
class EvalAssertion:
    kind: str
    path: str = ""
    value: str = ""
    command: tuple[str, ...] = ()
    exit_code: int = 0
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class EvalTask:
    id: str
    prompt: str
    mode: str = "react"
    files: Mapping[str, str] = field(default_factory=dict)
    assertions: tuple[EvalAssertion, ...] = ()
    max_run_seconds: float = 180.0


@dataclass(frozen=True)
class EvalSuite:
    name: str
    version: str
    tasks: tuple[EvalTask, ...]


@dataclass(frozen=True)
class AssertionResult:
    kind: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CaseExecution:
    run_id: str
    status: str
    answer: str
    error: str
    changed_files: tuple[str, ...]
    metrics: Mapping[str, object]


@dataclass(frozen=True)
class EvalCaseResult:
    task_id: str
    mode: str
    success: bool
    run_id: str
    run_status: str
    answer: str
    error: str
    duration_ms: int
    changed_files: tuple[str, ...]
    assertions: tuple[AssertionResult, ...]
    metrics: Mapping[str, object]


@dataclass(frozen=True)
class EvaluationReport:
    schema_version: int
    suite_name: str
    suite_version: str
    created_at: float
    git_commit: str
    provider: str
    model: str
    cases: tuple[EvalCaseResult, ...]
    metrics: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "suite_name": self.suite_name,
            "suite_version": self.suite_version,
            "created_at": self.created_at,
            "git_commit": self.git_commit,
            "provider": self.provider,
            "model": self.model,
            "cases": [
                {
                    **asdict(case),
                    "changed_files": list(case.changed_files),
                    "assertions": [asdict(item) for item in case.assertions],
                    "metrics": dict(case.metrics),
                }
                for case in self.cases
            ],
            "metrics": dict(self.metrics),
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target

    @classmethod
    def load(cls, path: str | Path) -> EvaluationReport:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cases = tuple(
            EvalCaseResult(
                task_id=str(item["task_id"]),
                mode=str(item["mode"]),
                success=bool(item["success"]),
                run_id=str(item.get("run_id", "")),
                run_status=str(item.get("run_status", "")),
                answer=str(item.get("answer", "")),
                error=str(item.get("error", "")),
                duration_ms=int(item.get("duration_ms", 0)),
                changed_files=tuple(item.get("changed_files", [])),
                assertions=tuple(
                    AssertionResult(
                        str(assertion["kind"]),
                        bool(assertion["passed"]),
                        str(assertion["detail"]),
                    )
                    for assertion in item.get("assertions", [])
                ),
                metrics=dict(item.get("metrics", {})),
            )
            for item in data.get("cases", [])
        )
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            suite_name=str(data["suite_name"]),
            suite_version=str(data.get("suite_version", "1")),
            created_at=float(data.get("created_at", 0)),
            git_commit=str(data.get("git_commit", "")),
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            cases=cases,
            metrics=dict(data.get("metrics", {})),
        )


class CaseExecutor(Protocol):
    provider: str
    model: str

    def execute(self, task: EvalTask, workspace: Path) -> CaseExecution:
        ...


class AgentCaseExecutor:
    """Execute one case using the same coordinated CLI runtime graph."""

    def __init__(
        self,
        provider: str,
        *,
        environ: Mapping[str, str] | None = None,
        max_model_calls: int = 80,
        max_tool_calls: int = 120,
    ) -> None:
        self.provider = provider
        self.environ = dict(os.environ if environ is None else environ)
        probe = LlmClientFactory.create(provider, environ=self.environ)
        self.model = str(probe.model)
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls

    def execute(self, task: EvalTask, workspace: Path) -> CaseExecution:
        client = LlmClientFactory.create(self.provider, environ=self.environ)
        runtime = build_application_runtime(
            client,
            workspace,
            allow_shell=True,
            enable_memory=False,
            max_steps=30,
            subagent_max_steps=20,
            plan_workers=2,
            team_workers=2,
            review_retries=2,
            enable_hitl=True,
            approval_mode=ApprovalMode.ALLOW,
            enable_trace=True,
            trace_path=workspace / ".paicli" / "traces.db",
            llm_max_attempts=3,
        )
        coordinator = RunCoordinator(
            runtime,
            workspace,
            limits=RunLimits(
                max_seconds=task.max_run_seconds,
                max_model_calls=self.max_model_calls,
                max_tool_calls=self.max_tool_calls,
            ),
            rollback_policy=RollbackPolicy.NEVER,
        )
        try:
            result = coordinator.execute(task.mode, task.prompt)
            metrics = (
                runtime.trace_store.run_summary(result.run_id)
                if runtime.trace_store is not None
                else dict(result.budget or {})
            )
            return CaseExecution(
                result.run_id,
                result.status,
                result.answer,
                result.error,
                _changed_files(result),
                metrics,
            )
        finally:
            coordinator.close()


class EvaluationRunner:
    def __init__(
        self,
        executor: CaseExecutor,
        *,
        keep_workspaces: bool = False,
        workspace_parent: str | Path | None = None,
    ) -> None:
        self.executor = executor
        self.keep_workspaces = keep_workspaces
        self.workspace_parent = (
            Path(workspace_parent).resolve() if workspace_parent else None
        )

    def run(self, suite: EvalSuite) -> EvaluationReport:
        results: list[EvalCaseResult] = []
        for task in suite.tasks:
            results.append(self._run_case(task))
        return EvaluationReport(
            schema_version=1,
            suite_name=suite.name,
            suite_version=suite.version,
            created_at=time.time(),
            git_commit=_git_commit(),
            provider=self.executor.provider,
            model=self.executor.model,
            cases=tuple(results),
            metrics=_aggregate(results),
        )

    def _run_case(self, task: EvalTask) -> EvalCaseResult:
        if self.keep_workspaces:
            parent = self.workspace_parent or Path(".paicli/eval-workspaces").resolve()
            parent.mkdir(parents=True, exist_ok=True)
            workspace = parent / f"{task.id}-{int(time.time() * 1000)}"
            workspace.mkdir()
            cleanup: Callable[[], None] = lambda: None
        else:
            temporary = tempfile.TemporaryDirectory(prefix=f"paicli-eval-{task.id}-")
            workspace = Path(temporary.name)
            cleanup = temporary.cleanup

        started = time.perf_counter()
        execution: CaseExecution
        try:
            _seed_files(workspace, task.files)
            try:
                execution = self.executor.execute(task, workspace)
            except Exception as exc:
                execution = CaseExecution(
                    "",
                    "failed",
                    "",
                    f"{type(exc).__name__}: {exc}",
                    (),
                    {},
                )
            assertions = tuple(
                _evaluate_assertion(assertion, workspace, execution.answer)
                for assertion in task.assertions
            )
            success = (
                execution.status == "succeeded"
                and all(item.passed for item in assertions)
            )
            return EvalCaseResult(
                task.id,
                task.mode,
                success,
                execution.run_id,
                execution.status,
                execution.answer,
                execution.error,
                max(0, int((time.perf_counter() - started) * 1000)),
                execution.changed_files,
                assertions,
                dict(execution.metrics),
            )
        finally:
            cleanup()


def load_suite(path: str | Path) -> EvalSuite:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks: list[EvalTask] = []
    for raw_task in data.get("tasks", []):
        assertions = tuple(
            EvalAssertion(
                kind=str(item["kind"]),
                path=str(item.get("path", "")),
                value=str(item.get("value", "")),
                command=tuple(str(x) for x in item.get("command", [])),
                exit_code=int(item.get("exit_code", 0)),
                timeout_seconds=float(item.get("timeout_seconds", 30)),
            )
            for item in raw_task.get("assertions", [])
        )
        tasks.append(
            EvalTask(
                id=str(raw_task["id"]),
                prompt=str(raw_task["prompt"]),
                mode=str(raw_task.get("mode", "react")),
                files={
                    str(name): str(content)
                    for name, content in raw_task.get("files", {}).items()
                },
                assertions=assertions,
                max_run_seconds=float(raw_task.get("max_run_seconds", 180)),
            )
        )
    if not tasks:
        raise ValueError("evaluation suite must contain at least one task")
    return EvalSuite(
        str(data.get("name", Path(path).stem)),
        str(data.get("version", "1")),
        tuple(tasks),
    )


def compare_reports(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
) -> dict[str, object]:
    if (
        baseline.suite_name != candidate.suite_name
        or baseline.suite_version != candidate.suite_version
    ):
        raise ValueError("reports must use the same suite name and version")
    numeric = {
        key
        for key in set(baseline.metrics) & set(candidate.metrics)
        if isinstance(baseline.metrics[key], (int, float))
        and isinstance(candidate.metrics[key], (int, float))
    }
    deltas = {
        key: float(candidate.metrics[key]) - float(baseline.metrics[key])
        for key in sorted(numeric)
    }
    base_success = float(baseline.metrics.get("success_rate", 0.0))
    candidate_success = float(candidate.metrics.get("success_rate", 0.0))
    base_assertions = float(baseline.metrics.get("assertion_pass_rate", 0.0))
    candidate_assertions = float(candidate.metrics.get("assertion_pass_rate", 0.0))
    if candidate_success > base_success or (
        candidate_success == base_success and candidate_assertions > base_assertions
    ):
        verdict = "improved"
    elif candidate_success < base_success or (
        candidate_success == base_success and candidate_assertions < base_assertions
    ):
        verdict = "regressed"
    else:
        base_cost = float(baseline.metrics.get("estimated_cost_cny", 0.0))
        candidate_cost = float(candidate.metrics.get("estimated_cost_cny", 0.0))
        base_latency = float(baseline.metrics.get("average_duration_ms", 0.0))
        candidate_latency = float(candidate.metrics.get("average_duration_ms", 0.0))
        if candidate_cost < base_cost or candidate_latency < base_latency:
            verdict = "efficiency_improved"
        elif candidate_cost > base_cost and candidate_latency > base_latency:
            verdict = "efficiency_regressed"
        else:
            verdict = "unchanged_or_mixed"
    baseline_by_id = {case.task_id: case for case in baseline.cases}
    candidate_by_id = {case.task_id: case for case in candidate.cases}
    case_changes = {
        task_id: {
            "baseline": baseline_by_id[task_id].success,
            "candidate": candidate_by_id[task_id].success,
        }
        for task_id in sorted(set(baseline_by_id) & set(candidate_by_id))
        if baseline_by_id[task_id].success != candidate_by_id[task_id].success
    }
    return {
        "suite": baseline.suite_name,
        "version": baseline.suite_version,
        "baseline_commit": baseline.git_commit,
        "candidate_commit": candidate.git_commit,
        "verdict": verdict,
        "metric_deltas": deltas,
        "case_success_changes": case_changes,
    }


def _seed_files(root: Path, files: Mapping[str, str]) -> None:
    for raw_path, content in files.items():
        path = (root / raw_path).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(f"evaluation file escapes workspace: {raw_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _evaluate_assertion(
    assertion: EvalAssertion,
    workspace: Path,
    answer: str,
) -> AssertionResult:
    try:
        if assertion.kind == "answer_contains":
            passed = assertion.value in answer
            return AssertionResult(
                assertion.kind,
                passed,
                f"expected answer to contain {assertion.value!r}",
            )
        path = (workspace / assertion.path).resolve()
        if assertion.path and not path.is_relative_to(workspace.resolve()):
            raise ValueError("assertion path escapes workspace")
        if assertion.kind == "file_exists":
            return AssertionResult(
                assertion.kind,
                path.is_file(),
                f"expected file {assertion.path} to exist",
            )
        if assertion.kind in {"file_contains", "file_equals", "file_not_contains"}:
            content = path.read_text(encoding="utf-8")
            passed = (
                assertion.value in content
                if assertion.kind == "file_contains"
                else assertion.value not in content
                if assertion.kind == "file_not_contains"
                else content == assertion.value
            )
            return AssertionResult(
                assertion.kind,
                passed,
                f"checked {assertion.path}",
            )
        if assertion.kind == "command":
            if not assertion.command:
                raise ValueError("command assertion requires a non-empty command array")
            completed = subprocess.run(
                list(assertion.command),
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=assertion.timeout_seconds,
                check=False,
            )
            passed = completed.returncode == assertion.exit_code
            return AssertionResult(
                assertion.kind,
                passed,
                (
                    f"exit={completed.returncode}, expected={assertion.exit_code}; "
                    + (completed.stdout + completed.stderr)[-2_000:]
                ),
            )
        raise ValueError(f"unknown assertion kind: {assertion.kind}")
    except Exception as exc:
        return AssertionResult(
            assertion.kind,
            False,
            f"{type(exc).__name__}: {exc}",
        )


def _aggregate(cases: list[EvalCaseResult]) -> dict[str, object]:
    total = len(cases)
    passed = sum(case.success for case in cases)
    assertions = [item for case in cases for item in case.assertions]
    assertion_passed = sum(item.passed for item in assertions)

    def metric_sum(name: str) -> float:
        return sum(
            float(case.metrics.get(name, 0) or 0)
            for case in cases
        )

    return {
        "tasks": total,
        "tasks_succeeded": passed,
        "success_rate": passed / total if total else 0.0,
        "assertions": len(assertions),
        "assertions_passed": assertion_passed,
        "assertion_pass_rate": (
            assertion_passed / len(assertions) if assertions else 0.0
        ),
        "average_duration_ms": (
            sum(case.duration_ms for case in cases) / total if total else 0.0
        ),
        "input_tokens": int(metric_sum("input_tokens")),
        "output_tokens": int(metric_sum("output_tokens")),
        "model_calls": int(metric_sum("model_calls")),
        "tool_calls": int(metric_sum("tool_calls")),
        "model_errors": int(metric_sum("model_errors")),
        "tool_errors": int(metric_sum("tool_errors")),
        "estimated_cost_cny": metric_sum("estimated_cost_cny"),
        "unpriced_model_calls": int(metric_sum("unpriced_model_calls")),
    }


def _changed_files(result: CoordinatedRun) -> tuple[str, ...]:
    if result.agent_outcome is not None:
        return result.agent_outcome.changed_files
    if result.orchestration_result is not None:
        return result.orchestration_result.changed_files
    return ()


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _load_env(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, item = value.split("=", 1)
        os.environ.setdefault(key.strip(), item.strip().strip("\"'"))


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PaiCLI fixed-task evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run a fixed evaluation suite")
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument("--provider", required=True, choices=sorted(LlmClientFactory.PROVIDERS))
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--keep-workspaces", action="store_true")
    run.add_argument("--workspace-parent", type=Path)
    compare = subparsers.add_parser("compare", help="Compare two reports")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = build_cli().parse_args(argv)
    if args.command == "run":
        suite = load_suite(args.suite)
        executor = AgentCaseExecutor(args.provider)
        report = EvaluationRunner(
            executor,
            keep_workspaces=args.keep_workspaces,
            workspace_parent=args.workspace_parent,
        ).run(suite)
        report.save(args.output)
        print(json.dumps(report.metrics, ensure_ascii=False, indent=2))
        print(f"report={args.output}")
        return 0 if float(report.metrics["success_rate"]) == 1.0 else 1
    baseline = EvaluationReport.load(args.baseline)
    candidate = EvaluationReport.load(args.candidate)
    comparison = compare_reports(baseline, candidate)
    rendered = json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 1 if comparison["verdict"] == "regressed" else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AgentCaseExecutor",
    "AssertionResult",
    "CaseExecution",
    "CaseExecutor",
    "EvalAssertion",
    "EvalCaseResult",
    "EvalSuite",
    "EvalTask",
    "EvaluationReport",
    "EvaluationRunner",
    "compare_reports",
    "load_suite",
]
