"""Fixed-task evaluation and baseline/candidate comparison for PaiCLI."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
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
    git_commit: str

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
        self.git_commit = _git_commit()
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


class GitRevisionCaseExecutor:
    """Execute a suite against an exported historical Git revision.

    The revision is materialized with ``git archive`` rather than a worktree, so
    benchmark runs cannot mutate the developer checkout or violate the single-
    worktree project policy. Older revisions that predate Plan/Team are recorded
    as real capability failures instead of being simulated by current code.
    """

    def __init__(
        self,
        provider: str,
        revision: str,
        *,
        repository_root: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        python_executable: str | None = None,
        max_model_calls: int = 80,
        max_tool_calls: int = 120,
    ) -> None:
        self.provider = provider.strip().lower()
        self.environ = dict(os.environ if environ is None else environ)
        self.repository_root = _repository_root(repository_root)
        self.git_commit = _resolve_revision(self.repository_root, revision)
        self.revision = revision
        self.python_executable = python_executable or sys.executable
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self._source_directory = tempfile.TemporaryDirectory(
            prefix="paicli-revision-source-"
        )
        self.source_root = Path(self._source_directory.name)
        _export_revision(self.repository_root, self.git_commit, self.source_root)
        self._environment = _revision_environment(
            self.environ,
            self.source_root,
            self.provider,
        )
        help_result = self._run_source(["--help"], timeout_seconds=30)
        if help_result.returncode != 0:
            self.close()
            detail = _bounded_text(
                help_result.stdout + "\n" + help_result.stderr,
                4_000,
            )
            raise RuntimeError(
                f"revision {self.git_commit[:12]} cannot start PaiCLI: {detail}"
            )
        self.help_text = help_result.stdout + "\n" + help_result.stderr
        self.supported_modes = (
            ("react", "plan", "team")
            if "--mode" in self.help_text
            else ("react",)
        )
        self._provider_flag = self.provider in self.help_text
        self.model = _configured_model(self.provider, self.environ)

    def execute(self, task: EvalTask, workspace: Path) -> CaseExecution:
        if task.mode not in self.supported_modes:
            return CaseExecution(
                run_id="",
                status="failed",
                answer="",
                error=(
                    f"revision {self.git_commit[:12]} does not expose "
                    f"the {task.mode!r} execution mode"
                ),
                changed_files=(),
                metrics=_revision_metrics(exit_code=2),
            )

        before = _workspace_fingerprints(workspace)
        arguments = [
            "--project-root",
            str(workspace),
            "--prompt",
            task.prompt,
        ]
        if self._provider_flag:
            arguments.extend(["--provider", self.provider])
        if "--mode" in self.help_text:
            arguments.extend(["--mode", task.mode])
        if "--renderer" in self.help_text:
            arguments.extend(["--renderer", "plain"])
        if "--allow-shell" in self.help_text:
            arguments.append("--allow-shell")
        if "--approval-mode" in self.help_text:
            arguments.extend(["--approval-mode", "allow"])
        if "--rollback-on-failure" in self.help_text:
            arguments.extend(["--rollback-on-failure", "never"])
        if "--no-snapshot" in self.help_text:
            arguments.append("--no-snapshot")
        if "--no-memory" in self.help_text:
            arguments.append("--no-memory")
        if "--no-trace" in self.help_text:
            arguments.append("--no-trace")
        if "--max-run-seconds" in self.help_text:
            arguments.extend(["--max-run-seconds", str(task.max_run_seconds)])
        if "--max-model-calls" in self.help_text:
            arguments.extend(["--max-model-calls", str(self.max_model_calls)])
        if "--max-tool-calls" in self.help_text:
            arguments.extend(["--max-tool-calls", str(self.max_tool_calls)])

        try:
            completed = self._run_source(
                arguments,
                timeout_seconds=max(60.0, task.max_run_seconds + 30.0),
            )
        except subprocess.TimeoutExpired as exc:
            after = _workspace_fingerprints(workspace)
            return CaseExecution(
                run_id="",
                status="failed",
                answer=_bounded_text(exc.stdout or "", 12_000),
                error=(
                    f"revision subprocess exceeded {task.max_run_seconds:.0f}s: "
                    + _bounded_text(exc.stderr or "", 4_000)
                ),
                changed_files=_changed_workspace_files(before, after),
                metrics=_revision_metrics(exit_code=124),
            )

        after = _workspace_fingerprints(workspace)
        output = _bounded_text(completed.stdout, 12_000)
        status = "succeeded" if completed.returncode == 0 else "failed"
        error = ""
        if completed.returncode != 0:
            error = _bounded_text(
                completed.stderr + "\n" + completed.stdout,
                8_000,
            )
        return CaseExecution(
            run_id="",
            status=status,
            answer=output,
            error=error,
            changed_files=_changed_workspace_files(before, after),
            metrics=_revision_metrics(exit_code=completed.returncode),
        )

    def close(self) -> None:
        directory = getattr(self, "_source_directory", None)
        if directory is not None:
            directory.cleanup()
            self._source_directory = None  # type: ignore[assignment]

    def __enter__(self) -> GitRevisionCaseExecutor:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _run_source(
        self,
        arguments: list[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.python_executable, "-m", "paicli", *arguments],
            cwd=self.source_root,
            env=self._environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )


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
            git_commit=str(getattr(self.executor, "git_commit", _git_commit())),
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


def summarize_stability(
    reports: list[EvaluationReport] | tuple[EvaluationReport, ...],
) -> dict[str, object]:
    """Aggregate repeated real-model runs without hiding individual failures."""

    if not reports:
        raise ValueError("at least one evaluation report is required")
    first = reports[0]
    expected_cases = tuple(case.task_id for case in first.cases)
    for report in reports[1:]:
        if (
            report.suite_name != first.suite_name
            or report.suite_version != first.suite_version
            or report.provider != first.provider
            or report.model != first.model
        ):
            raise ValueError(
                "stability reports must use the same suite, provider, and model"
            )
        if tuple(case.task_id for case in report.cases) != expected_cases:
            raise ValueError("stability reports must contain the same ordered cases")

    case_summaries: dict[str, object] = {}
    for task_id in expected_cases:
        attempts = [
            next(case for case in report.cases if case.task_id == task_id)
            for report in reports
        ]
        assertions = [item for case in attempts for item in case.assertions]
        durations = [case.duration_ms for case in attempts]
        case_summaries[task_id] = {
            "mode": attempts[0].mode,
            "attempts": len(attempts),
            "successes": sum(case.success for case in attempts),
            "success_rate": sum(case.success for case in attempts) / len(attempts),
            "assertions": len(assertions),
            "assertions_passed": sum(item.passed for item in assertions),
            "assertion_pass_rate": (
                sum(item.passed for item in assertions) / len(assertions)
                if assertions
                else 0.0
            ),
            "duration_ms": _distribution(durations),
            "input_tokens": _distribution(
                [int(case.metrics.get("input_tokens", 0) or 0) for case in attempts]
            ),
            "output_tokens": _distribution(
                [int(case.metrics.get("output_tokens", 0) or 0) for case in attempts]
            ),
            "model_calls": _distribution(
                [int(case.metrics.get("model_calls", 0) or 0) for case in attempts]
            ),
            "tool_calls": _distribution(
                [int(case.metrics.get("tool_calls", 0) or 0) for case in attempts]
            ),
            "statuses": {
                status: sum(case.run_status == status for case in attempts)
                for status in sorted({case.run_status for case in attempts})
            },
            "failed_runs": [
                {
                    "run_id": case.run_id,
                    "status": case.run_status,
                    "error": _bounded_text(case.error, 2_000),
                }
                for case in attempts
                if not case.success
            ],
        }

    total_cases = sum(len(report.cases) for report in reports)
    successful_cases = sum(
        case.success for report in reports for case in report.cases
    )
    fully_successful_runs = sum(
        bool(report.cases) and all(case.success for case in report.cases)
        for report in reports
    )
    assertion_results = [
        assertion
        for report in reports
        for case in report.cases
        for assertion in case.assertions
    ]
    return {
        "schema_version": 1,
        "kind": "stability",
        "created_at": time.time(),
        "suite_name": first.suite_name,
        "suite_version": first.suite_version,
        "provider": first.provider,
        "model": first.model,
        "git_commits": sorted({report.git_commit for report in reports}),
        "runs": [
            {
                "git_commit": report.git_commit,
                "created_at": report.created_at,
                "success_rate": report.metrics.get("success_rate", 0.0),
                "assertion_pass_rate": report.metrics.get(
                    "assertion_pass_rate",
                    0.0,
                ),
                "average_duration_ms": report.metrics.get(
                    "average_duration_ms",
                    0.0,
                ),
                "input_tokens": report.metrics.get("input_tokens", 0),
                "output_tokens": report.metrics.get("output_tokens", 0),
                "model_calls": report.metrics.get("model_calls", 0),
                "tool_calls": report.metrics.get("tool_calls", 0),
                "estimated_cost_cny": report.metrics.get(
                    "estimated_cost_cny",
                    0.0,
                ),
            }
            for report in reports
        ],
        "metrics": {
            "run_count": len(reports),
            "fully_successful_runs": fully_successful_runs,
            "run_success_rate": fully_successful_runs / len(reports),
            "task_attempts": total_cases,
            "tasks_succeeded": successful_cases,
            "task_success_rate": (
                successful_cases / total_cases if total_cases else 0.0
            ),
            "assertions": len(assertion_results),
            "assertions_passed": sum(item.passed for item in assertion_results),
            "assertion_pass_rate": (
                sum(item.passed for item in assertion_results)
                / len(assertion_results)
                if assertion_results
                else 0.0
            ),
            "total_input_tokens": sum(
                int(report.metrics.get("input_tokens", 0) or 0)
                for report in reports
            ),
            "total_output_tokens": sum(
                int(report.metrics.get("output_tokens", 0) or 0)
                for report in reports
            ),
            "total_model_calls": sum(
                int(report.metrics.get("model_calls", 0) or 0)
                for report in reports
            ),
            "total_tool_calls": sum(
                int(report.metrics.get("tool_calls", 0) or 0)
                for report in reports
            ),
            "total_model_errors": sum(
                int(report.metrics.get("model_errors", 0) or 0)
                for report in reports
            ),
            "total_tool_errors": sum(
                int(report.metrics.get("tool_errors", 0) or 0)
                for report in reports
            ),
            "total_estimated_cost_cny": sum(
                float(report.metrics.get("estimated_cost_cny", 0.0) or 0.0)
                for report in reports
            ),
            "unpriced_model_calls": sum(
                int(report.metrics.get("unpriced_model_calls", 0) or 0)
                for report in reports
            ),
        },
        "cases": case_summaries,
    }


def _distribution(values: list[int] | tuple[int, ...]) -> dict[str, float | int]:
    if not values:
        return {"min": 0, "max": 0, "mean": 0.0, "median": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
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


def _repository_root(value: str | Path | None) -> Path:
    candidate = Path(value or Path.cwd()).expanduser().resolve()
    try:
        root = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"not a readable Git repository: {candidate}") from exc
    return Path(root).resolve()


def _resolve_revision(repository: Path, revision: str) -> str:
    normalized = str(revision).strip()
    if not normalized:
        raise ValueError("revision cannot be empty")
    try:
        return subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "rev-parse",
                "--verify",
                normalized + "^{commit}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"unknown Git revision: {revision}") from exc


def _export_revision(repository: Path, revision: str, destination: Path) -> None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "archive", "--format=tar", revision],
            capture_output=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"failed to export revision {revision[:12]}") from exc
    with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsafe path in Git archive: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(
                    f"symlinks are not allowed in evaluation exports: {member.name}"
                )
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError(
                    f"unsupported entry in evaluation export: {member.name}"
                )
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError(f"cannot read Git archive entry: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stream.read())
            target.chmod(member.mode & 0o777)


def _revision_environment(
    source: Mapping[str, str],
    source_root: Path,
    provider: str,
) -> dict[str, str]:
    environment = dict(source)
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(source_root) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    prefix = provider.upper()
    aliases = {
        "PAICLI_API_KEY": f"{prefix}_API_KEY",
        "PAICLI_MODEL": f"{prefix}_MODEL",
        "PAICLI_BASE_URL": f"{prefix}_BASE_URL",
    }
    for target, source_name in aliases.items():
        value = environment.get(source_name, "").strip()
        if value:
            environment[target] = value
    return environment


def _configured_model(provider: str, environment: Mapping[str, str]) -> str:
    return environment.get(f"{provider.upper()}_MODEL", "").strip() or provider


def _workspace_fingerprints(root: Path) -> dict[str, str]:
    ignored = {".git", ".paicli", ".pytest_cache", "__pycache__"}
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ignored.intersection(relative.parts):
            continue
        name = relative.as_posix()
        try:
            if path.is_symlink():
                result[name] = "symlink:" + os.readlink(path)
            elif path.is_file():
                result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            result[name] = "unreadable"
    return result


def _changed_workspace_files(
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )
    )


def _revision_metrics(*, exit_code: int) -> dict[str, object]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "model_errors": 0,
        "tool_errors": 0,
        "estimated_cost_cny": 0.0,
        "unpriced_model_calls": 0,
        "revision_subprocess": True,
        "exit_code": int(exit_code),
    }


def _bounded_text(value: str | bytes, limit: int) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    return text if len(text) <= limit else text[-limit:]


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


def _write_json(path: str | Path, payload: Mapping[str, object]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


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
    run.add_argument(
        "--revision",
        help="Run the suite against an exported historical Git revision",
    )
    run.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="Git repository containing --revision (defaults to current repo)",
    )
    compare = subparsers.add_parser("compare", help="Compare two reports")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--output", type=Path)
    stability = subparsers.add_parser(
        "stability",
        help="Aggregate repeated reports for the same suite/provider/model",
    )
    stability.add_argument("reports", type=Path, nargs="+")
    stability.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = build_cli().parse_args(argv)
    if args.command == "run":
        suite = load_suite(args.suite)
        revision_executor: GitRevisionCaseExecutor | None = None
        try:
            if args.revision:
                revision_executor = GitRevisionCaseExecutor(
                    args.provider,
                    args.revision,
                    repository_root=args.repository,
                )
                executor: CaseExecutor = revision_executor
            else:
                executor = AgentCaseExecutor(args.provider)
            report = EvaluationRunner(
                executor,
                keep_workspaces=args.keep_workspaces,
                workspace_parent=args.workspace_parent,
            ).run(suite)
        finally:
            if revision_executor is not None:
                revision_executor.close()
        report.save(args.output)
        print(json.dumps(report.metrics, ensure_ascii=False, indent=2))
        print(f"report={args.output}")
        return 0 if float(report.metrics["success_rate"]) == 1.0 else 1
    if args.command == "compare":
        baseline = EvaluationReport.load(args.baseline)
        candidate = EvaluationReport.load(args.candidate)
        comparison = compare_reports(baseline, candidate)
        rendered = json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True)
        print(rendered)
        if args.output:
            _write_json(args.output, comparison)
        return 1 if comparison["verdict"] == "regressed" else 0
    reports = [EvaluationReport.load(path) for path in args.reports]
    stability = summarize_stability(reports)
    _write_json(args.output, stability)
    print(json.dumps(stability["metrics"], ensure_ascii=False, indent=2))
    print(f"report={args.output}")
    return 0 if float(stability["metrics"]["run_success_rate"]) == 1.0 else 1


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
    "GitRevisionCaseExecutor",
    "compare_reports",
    "load_suite",
    "summarize_stability",
]
