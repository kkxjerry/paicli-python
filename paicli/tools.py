"""Tool gateway: registration, validation, policy metadata, and scheduling.

``execute()`` and ``execute_many()`` retain the original string-returning API.
The shared Agent loop uses ``execute_result()`` / ``execute_many_results()`` to
receive typed failures, elapsed time, resource claims, and changed files.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .tool_contracts import (
    ConcurrencyPolicy,
    ResourceAccess,
    ResourceMode,
    ToolErrorType,
    ToolExecutionFailure,
    ToolHandler,
    ToolResult,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)
from .tool_validation import SchemaValidationError, validate_json_schema


@dataclass(frozen=True)
class _PreparedCall:
    index: int
    name: str
    arguments_json: str
    tool: ToolSpec | None
    arguments: dict[str, Any] | None
    accesses: tuple[ResourceAccess, ...]
    error: ToolResult | None = None


@dataclass(frozen=True)
class _UncertainCall:
    future: Future[tuple[float, ToolResult]]
    tool_name: str
    accesses: tuple[ResourceAccess, ...]


class ToolRegistry:
    """Register tools and provide one validated execution boundary."""

    MAX_PARALLEL_TOOLS = 8

    def __init__(
        self,
        project_root: str | Path = ".",
        *,
        allow_shell: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.allow_shell = allow_shell
        self._tools: dict[str, ToolSpec] = {}
        self._uncertain_calls: list[_UncertainCall] = []
        self._uncertain_lock = threading.Lock()
        self._register_builtin_tools()

    def definitions(self) -> list[dict[str, Any]]:
        """Return model-visible schemas without exposing Python handlers."""

        return [tool.definition() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def spec(self, name: str) -> ToolSpec | None:
        """Return static metadata for policy and diagnostics layers."""

        return self._tools.get(name)

    def register(self, tool: ToolSpec) -> None:
        self._register(tool)

    def validate_arguments(self, name: str, arguments_json: str) -> dict[str, Any]:
        """Parse and validate one invocation without executing it.

        HITL wrappers use this before asking a user to approve a malformed or
        impossible request.  The arguments are validated again after an
        approver modifies them, so approval cannot bypass the schema gate.
        """

        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool {name!r}")
        return self._parse_and_validate(tool, arguments_json)

    def execute(self, name: str, arguments_json: str) -> str:
        """Backward-compatible string execution API."""

        return self.execute_result(name, arguments_json).content

    def execute_result(self, name: str, arguments_json: str) -> ToolResult:
        """Validate and execute one call with the tool's timeout policy."""

        return self.execute_many_results([(name, arguments_json)])[0]

    def execute_many(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> list[str]:
        """Backward-compatible batch API with stable result order."""

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
        """Execute calls in conflict-free waves while preserving input order.

        Read/read claims on the same resource may run together. Read/write,
        write/write, recursive-directory overlap, global process claims, and
        tools marked ``SERIAL`` are separated into later waves. This avoids the
        previous race where a model could request ``read_file(a.py)`` and
        ``write_file(a.py)`` in one response and observe nondeterministic state.
        """

        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when configured")
        if not calls:
            return []

        prepared = [
            self._prepare_call(index, name, arguments_json)
            for index, (name, arguments_json) in enumerate(calls)
        ]
        waves = self._schedule_waves(prepared)
        results_by_index: dict[int, ToolResult] = {}
        for wave in waves:
            guarded_wave = [self._recheck_uncertain(call) for call in wave]
            for index, result in self._execute_wave(
                guarded_wave,
                timeout_seconds=timeout_seconds,
            ).items():
                results_by_index[index] = result
        return [results_by_index[index] for index in range(len(calls))]

    def _register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def _prepare_call(
        self,
        index: int,
        name: str,
        arguments_json: str,
    ) -> _PreparedCall:
        tool = self._tools.get(name)
        if tool is None:
            error = ToolResult.failure(
                name,
                f"Tool error: unknown tool {name!r}",
                ToolErrorType.UNKNOWN_TOOL,
            )
            return _PreparedCall(index, name, arguments_json, None, None, (), error)

        try:
            arguments = self._parse_and_validate(tool, arguments_json)
            accesses = (
                tuple(tool.resource_resolver(arguments))
                if tool.resource_resolver is not None
                else self._default_accesses(tool)
            )
            blocked_by = self._uncertain_conflict(accesses)
            if blocked_by is not None:
                error = ToolResult.failure(
                    name,
                    "Tool error: resource is isolated while prior timed-out "
                    f"tool {blocked_by!r} may still be running",
                    ToolErrorType.RESOURCE_CONFLICT,
                    retryable=False,
                    accesses=accesses,
                )
                return _PreparedCall(
                    index,
                    name,
                    arguments_json,
                    tool,
                    arguments,
                    accesses,
                    error,
                )
        except (json.JSONDecodeError, SchemaValidationError, ValueError, TypeError) as exc:
            error = ToolResult.failure(
                name,
                f"Tool error: {exc}",
                ToolErrorType.INVALID_ARGUMENTS,
                retryable=True,
            )
            return _PreparedCall(index, name, arguments_json, tool, None, (), error)
        return _PreparedCall(
            index,
            name,
            arguments_json,
            tool,
            arguments,
            accesses,
        )

    @staticmethod
    def _default_accesses(tool: ToolSpec) -> tuple[ResourceAccess, ...]:
        if tool.side_effect is ToolSideEffect.READ_ONLY:
            return ()
        return (ResourceAccess("*", ResourceMode.WRITE, recursive=True),)

    def _recheck_uncertain(self, call: _PreparedCall) -> _PreparedCall:
        if call.error is not None:
            return call
        blocked_by = self._uncertain_conflict(call.accesses)
        if blocked_by is None:
            return call
        return _PreparedCall(
            call.index,
            call.name,
            call.arguments_json,
            call.tool,
            call.arguments,
            call.accesses,
            ToolResult.failure(
                call.name,
                "Tool error: resource is isolated while prior timed-out "
                f"tool {blocked_by!r} may still be running",
                ToolErrorType.RESOURCE_CONFLICT,
                retryable=False,
                accesses=call.accesses,
            ),
        )

    def _uncertain_conflict(
        self,
        accesses: tuple[ResourceAccess, ...],
    ) -> str | None:
        if not accesses:
            return None
        with self._uncertain_lock:
            self._uncertain_calls = [
                item for item in self._uncertain_calls if not item.future.done()
            ]
            for item in self._uncertain_calls:
                if any(
                    _accesses_conflict(left, right)
                    for left in accesses
                    for right in item.accesses
                ):
                    return item.tool_name
        return None

    def _track_uncertain(
        self,
        future: Future[tuple[float, ToolResult]],
        call: _PreparedCall,
    ) -> None:
        if call.tool is None or call.tool.side_effect is ToolSideEffect.READ_ONLY:
            return
        with self._uncertain_lock:
            self._uncertain_calls = [
                item for item in self._uncertain_calls if not item.future.done()
            ]
            if not future.done():
                self._uncertain_calls.append(
                    _UncertainCall(future, call.name, call.accesses)
                )

    @staticmethod
    def _parse_and_validate(
        tool: ToolSpec,
        arguments_json: str,
    ) -> dict[str, Any]:
        arguments = json.loads(arguments_json or "{}")
        if not isinstance(arguments, dict):
            raise SchemaValidationError("$: arguments must be a JSON object")
        validate_json_schema(arguments, tool.parameters)
        return arguments

    def _execute_prepared(self, prepared: _PreparedCall) -> ToolResult:
        if prepared.error is not None:
            return prepared.error
        if prepared.tool is None or prepared.arguments is None:
            return ToolResult.failure(
                prepared.name,
                "Tool error: invocation was not prepared",
                ToolErrorType.EXECUTION_ERROR,
            )

        started = time.perf_counter()
        try:
            value = prepared.tool.handler(prepared.arguments)
            elapsed_ms = _elapsed_ms(started)
            changed_files = self._changed_files(prepared.tool, prepared.accesses)
            if isinstance(value, ToolResult):
                # Do not infer a committed file change from a failed result.
                # A handler that partially changed state may report explicit
                # changed_files, but silence must remain silence on failure.
                effective_changed_files = (
                    value.changed_files
                    if value.changed_files or not value.ok
                    else changed_files
                )
                return value.with_runtime_metadata(
                    elapsed_ms=elapsed_ms,
                    accesses=prepared.accesses,
                    changed_files=effective_changed_files,
                )
            return ToolResult.success(
                prepared.name,
                str(value),
                elapsed_ms=elapsed_ms,
                changed_files=changed_files,
                accesses=prepared.accesses,
            )
        except ToolExecutionFailure as exc:
            return ToolResult.failure(
                prepared.name,
                f"Tool error: {exc}",
                exc.error_type,
                retryable=exc.retryable,
                timed_out=exc.timed_out,
                elapsed_ms=_elapsed_ms(started),
                accesses=prepared.accesses,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult.failure(
                prepared.name,
                f"Tool error: {exc}",
                ToolErrorType.INVALID_ARGUMENTS,
                retryable=True,
                elapsed_ms=_elapsed_ms(started),
                accesses=prepared.accesses,
            )
        except Exception as exc:
            return ToolResult.failure(
                prepared.name,
                f"Tool error: {type(exc).__name__}: {exc}",
                ToolErrorType.EXECUTION_ERROR,
                elapsed_ms=_elapsed_ms(started),
                accesses=prepared.accesses,
            )

    def _schedule_waves(
        self,
        calls: list[_PreparedCall],
    ) -> list[list[_PreparedCall]]:
        waves: list[list[_PreparedCall]] = []
        current: list[_PreparedCall] = []
        for call in calls:
            if self._must_run_alone(call):
                if current:
                    waves.append(current)
                    current = []
                waves.append([call])
                continue
            if current and any(self._calls_conflict(call, other) for other in current):
                waves.append(current)
                current = [call]
            else:
                current.append(call)
        if current:
            waves.append(current)
        return waves

    @staticmethod
    def _must_run_alone(call: _PreparedCall) -> bool:
        if call.tool is None:
            return False
        if call.tool.concurrency is ConcurrencyPolicy.SERIAL:
            return True
        return any(access.resource == "*" for access in call.accesses)

    @staticmethod
    def _calls_conflict(left: _PreparedCall, right: _PreparedCall) -> bool:
        if left.tool is not None and left.tool.concurrency is ConcurrencyPolicy.SERIAL:
            return True
        if right.tool is not None and right.tool.concurrency is ConcurrencyPolicy.SERIAL:
            return True
        for left_access in left.accesses:
            for right_access in right.accesses:
                if _accesses_conflict(left_access, right_access):
                    return True
        return False

    def _execute_wave(
        self,
        wave: list[_PreparedCall],
        *,
        timeout_seconds: float | None,
    ) -> dict[int, ToolResult]:
        # Prepared validation errors are already complete and need no worker.
        if len(wave) == 1 and wave[0].error is not None:
            call = wave[0]
            return {call.index: call.error}

        # Avoid queueing more tasks than workers: a queued task's timeout should
        # start when its own bounded group is submitted, not while an earlier
        # group occupies every worker.
        if len(wave) > self.MAX_PARALLEL_TOOLS:
            results: dict[int, ToolResult] = {}
            for start in range(0, len(wave), self.MAX_PARALLEL_TOOLS):
                results.update(
                    self._execute_wave(
                        wave[start : start + self.MAX_PARALLEL_TOOLS],
                        timeout_seconds=timeout_seconds,
                    )
                )
            return results

        executor = ThreadPoolExecutor(
            max_workers=len(wave),
            thread_name_prefix="paicli-tool",
        )
        futures: dict[int, Future[tuple[float, ToolResult]]] = {}
        submitted_at: dict[int, float] = {}
        timeouts: dict[int, float] = {}
        for call in wave:
            timeout = (
                timeout_seconds
                if timeout_seconds is not None
                else call.tool.timeout_seconds if call.tool is not None else 60.0
            )
            submitted_at[call.index] = time.perf_counter()
            timeouts[call.index] = timeout
            futures[call.index] = executor.submit(
                self._execute_prepared_with_completion_time,
                call,
            )

        results: dict[int, ToolResult] = {}
        try:
            for call in wave:
                future = futures[call.index]
                timeout = timeouts[call.index]
                deadline = submitted_at[call.index] + timeout
                remaining = max(0.0, deadline - time.perf_counter())
                try:
                    completed_at, result = future.result(timeout=remaining)
                except TimeoutError:
                    cancelled_before_start = future.cancel()
                    if not cancelled_before_start:
                        self._track_uncertain(future, call)
                    results[call.index] = self._timeout_result(call, timeout)
                    continue
                if completed_at > deadline:
                    results[call.index] = self._timeout_result(
                        call,
                        timeout,
                        completed=result,
                    )
                else:
                    results[call.index] = result
        finally:
            # A running Python thread cannot be killed safely. Built-in process
            # tools therefore implement their own subprocess timeout as well.
            executor.shutdown(wait=False, cancel_futures=True)
        return results

    def _execute_prepared_with_completion_time(
        self,
        call: _PreparedCall,
    ) -> tuple[float, ToolResult]:
        result = self._execute_prepared(call)
        return time.perf_counter(), result

    @staticmethod
    def _timeout_result(
        call: _PreparedCall,
        timeout: float,
        *,
        completed: ToolResult | None = None,
    ) -> ToolResult:
        safe_to_retry = (
            call.tool is not None
            and call.tool.side_effect is ToolSideEffect.READ_ONLY
        )
        if completed is None:
            suffix = (
                ""
                if safe_to_retry
                else "; side-effect status is unknown, do not retry blindly"
            )
            elapsed_ms = int(timeout * 1000)
            changed_files: tuple[str, ...] = ()
        else:
            suffix = "; operation completed after its deadline"
            if not safe_to_retry:
                suffix += "; do not retry blindly"
            elapsed_ms = max(completed.elapsed_ms, int(timeout * 1000))
            changed_files = completed.changed_files
        result = ToolResult.failure(
            call.name,
            f"Tool error: timed out after {timeout:g} seconds{suffix}",
            ToolErrorType.TIMEOUT,
            retryable=safe_to_retry,
            timed_out=True,
            elapsed_ms=elapsed_ms,
            accesses=call.accesses,
        )
        if changed_files:
            result = result.with_runtime_metadata(
                elapsed_ms=elapsed_ms,
                accesses=call.accesses,
                changed_files=changed_files,
            )
        return result

    @staticmethod
    def _changed_files(
        tool: ToolSpec,
        accesses: tuple[ResourceAccess, ...],
    ) -> tuple[str, ...]:
        if tool.side_effect not in {
            ToolSideEffect.FILE_WRITE,
            ToolSideEffect.DIRECTORY_WRITE,
        }:
            return ()
        return tuple(
            dict.fromkeys(
                access.resource
                for access in accesses
                if access.mode is ResourceMode.WRITE and access.resource != "*"
            )
        )

    def _register_builtin_tools(self) -> None:
        self._register(
            ToolSpec(
                "read_file",
                "Read a UTF-8 text file inside the project root.",
                self._object_schema(
                    {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "description": "File path",
                        }
                    },
                    required=["path"],
                ),
                self._read_file,
                risk=ToolRisk.SAFE,
                side_effect=ToolSideEffect.READ_ONLY,
                concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                resource_resolver=lambda args: self._path_access(
                    args["path"], ResourceMode.READ
                ),
            )
        )
        self._register(
            ToolSpec(
                "write_file",
                "Create or overwrite a UTF-8 file inside the project root.",
                self._object_schema(
                    {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "description": "File path",
                        },
                        "content": {
                            "type": "string",
                            "description": "New file content",
                        },
                    },
                    required=["path", "content"],
                ),
                self._write_file,
                risk=ToolRisk.MEDIUM,
                side_effect=ToolSideEffect.FILE_WRITE,
                concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                resource_resolver=lambda args: self._path_access(
                    args["path"], ResourceMode.WRITE
                ),
            )
        )
        self._register(
            ToolSpec(
                "list_dir",
                "List files and directories inside the project root.",
                self._object_schema(
                    {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Directory path",
                        }
                    },
                ),
                self._list_dir,
                risk=ToolRisk.SAFE,
                side_effect=ToolSideEffect.READ_ONLY,
                concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                resource_resolver=lambda args: self._path_access(
                    args.get("path", "."),
                    ResourceMode.READ,
                    recursive=True,
                ),
            )
        )
        self._register(
            ToolSpec(
                "execute_command",
                "Run a short shell command in the project root. This tool may be disabled by local policy.",
                self._object_schema(
                    {
                        "command": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Shell command",
                        }
                    },
                    required=["command"],
                ),
                self._execute_command,
                risk=ToolRisk.HIGH,
                side_effect=ToolSideEffect.PROCESS,
                concurrency=ConcurrencyPolicy.SERIAL,
                resource_resolver=lambda _args: (
                    ResourceAccess("*", ResourceMode.WRITE, recursive=True),
                ),
                timeout_seconds=65.0,
            )
        )
        self._register(
            ToolSpec(
                "create_project",
                "Create a minimal Python, Java, or Node project directory.",
                self._object_schema(
                    {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Project name",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["python", "java", "node"],
                            "errorMessage": "unsupported project type",
                            "description": "Project type",
                        },
                    },
                    required=["name", "type"],
                ),
                self._create_project,
                risk=ToolRisk.MEDIUM,
                side_effect=ToolSideEffect.DIRECTORY_WRITE,
                concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                resource_resolver=lambda args: self._path_access(
                    args["name"], ResourceMode.WRITE, recursive=True
                ),
            )
        )

    @staticmethod
    def _object_schema(
        properties: dict[str, Any],
        *,
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }

    object_schema = _object_schema

    def _safe_path(self, raw_path: str) -> Path:
        candidate = (self.project_root / raw_path).resolve()
        if not candidate.is_relative_to(self.project_root):
            raise ValueError("path escapes the project root")
        return candidate

    def _path_access(
        self,
        raw_path: Any,
        mode: ResourceMode,
        *,
        recursive: bool = False,
    ) -> tuple[ResourceAccess, ...]:
        path = self._safe_path(str(raw_path))
        relative = path.relative_to(self.project_root).as_posix() or "."
        return (ResourceAccess(relative, mode, recursive),)

    def _read_file(self, arguments: dict[str, Any]) -> str:
        path = self._safe_path(arguments["path"])
        if not path.is_file():
            raise ValueError(f"not a file: {arguments['path']}")
        return path.read_text(encoding="utf-8")

    def _write_file(self, arguments: dict[str, Any]) -> str:
        path = self._safe_path(arguments["path"])
        content = arguments["content"]
        if len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("content exceeds the 1 MB learning-project limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {path.relative_to(self.project_root)}"

    def _list_dir(self, arguments: dict[str, Any]) -> str:
        raw_path = arguments.get("path", ".")
        path = self._safe_path(raw_path)
        if not path.is_dir():
            raise ValueError(f"not a directory: {raw_path}")
        entries = sorted(
            path.iterdir(),
            key=lambda item: (not item.is_dir(), item.name),
        )
        if not entries:
            return "(empty directory)"
        return "\n".join(
            f"[{'D' if entry.is_dir() else 'F'}] {entry.name}"
            for entry in entries
        )

    def _execute_command(self, arguments: dict[str, Any]) -> str | ToolResult:
        if not self.allow_shell:
            raise ToolExecutionFailure(
                "execute_command is disabled; start with --allow-shell",
                ToolErrorType.POLICY_DENIED,
            )

        command = arguments["command"].strip()
        if not command:
            raise ValueError("command cannot be empty")
        try:
            completed = subprocess.run(
                command,
                cwd=self.project_root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure(
                "execute_command",
                "Command timed out after 60 seconds; partial side effects may exist",
                ToolErrorType.TIMEOUT,
                retryable=False,
                timed_out=True,
            )

        output = (completed.stdout + completed.stderr)[:8_000]
        content = f"Exit code: {completed.returncode}\n{output}".rstrip()
        if completed.returncode != 0:
            return ToolResult.failure(
                "execute_command",
                content,
                ToolErrorType.EXECUTION_ERROR,
                retryable=False,
            )
        return content

    def _create_project(self, arguments: dict[str, Any]) -> str:
        project = self._safe_path(arguments["name"])
        project_type = arguments["type"].lower()
        if project_type not in {"python", "java", "node"}:
            raise ValueError(f"unsupported project type: {project_type}")

        project.mkdir(parents=True, exist_ok=False)
        if project_type == "python":
            (project / "main.py").write_text(
                'def main() -> None:\n    print("Hello")\n\n\nif __name__ == "__main__":\n    main()\n',
                encoding="utf-8",
            )
            (project / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
        elif project_type == "java":
            source = project / "src" / "main" / "java"
            source.mkdir(parents=True)
            (source / "Main.java").write_text(
                'public class Main {\n    public static void main(String[] args) {\n'
                '        System.out.println("Hello");\n    }\n}\n',
                encoding="utf-8",
            )
        else:
            (project / "package.json").write_text(
                json.dumps(
                    {"name": project.name, "version": "0.1.0", "type": "module"},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (project / "index.js").write_text(
                'console.log("Hello");\n',
                encoding="utf-8",
            )
        return f"Created {project_type} project at {project.name}"


class ScopedToolRuntime:
    """Expose a capability-scoped view of one shared ``ToolRegistry``.

    Sub-agents receive only the schemas they are allowed to use. A hallucinated
    call to a hidden tool is also rejected at execution time, so tool scoping is
    an authorization boundary rather than a prompt-only convention.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        allowed_names: Iterable[str],
        *,
        scope_name: str = "scoped-agent",
    ) -> None:
        self.registry = registry
        self.allowed_names = frozenset(str(name) for name in allowed_names)
        self.scope_name = str(scope_name).strip() or "scoped-agent"
        unknown = sorted(self.allowed_names - set(registry.names()))
        if unknown:
            raise ValueError(
                "tool scope contains unknown names: " + ", ".join(unknown)
            )

    @classmethod
    def read_only(
        cls,
        registry: ToolRegistry,
        *,
        scope_name: str = "read-only-agent",
    ) -> ScopedToolRuntime:
        allowed = []
        for name in registry.names():
            spec = registry.spec(name)
            if spec is not None and spec.side_effect is ToolSideEffect.READ_ONLY:
                allowed.append(name)
        return cls(registry, allowed, scope_name=scope_name)

    def definitions(self) -> list[dict[str, Any]]:
        return [
            definition
            for definition in self.registry.definitions()
            if definition["function"]["name"] in self.allowed_names
        ]

    def names(self) -> list[str]:
        return [name for name in self.registry.names() if name in self.allowed_names]

    def spec(self, name: str) -> ToolSpec | None:
        if name not in self.allowed_names:
            return None
        return self.registry.spec(name)

    def validate_arguments(self, name: str, arguments_json: str) -> dict[str, Any]:
        if name not in self.allowed_names:
            raise ValueError(self._denied_message(name))
        return self.registry.validate_arguments(name, arguments_json)

    def execute(self, name: str, arguments_json: str) -> str:
        return self.execute_result(name, arguments_json).content

    def execute_result(self, name: str, arguments_json: str) -> ToolResult:
        if name not in self.allowed_names:
            return self._denied_result(name)
        return self.registry.execute_result(name, arguments_json)

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
        if not calls:
            return []
        results: list[ToolResult | None] = [None] * len(calls)
        delegated_calls: list[tuple[str, str]] = []
        delegated_positions: list[int] = []
        for index, (name, arguments) in enumerate(calls):
            if name not in self.allowed_names:
                results[index] = self._denied_result(name)
                continue
            delegated_positions.append(index)
            delegated_calls.append((name, arguments))

        if delegated_calls:
            delegated_results = self.registry.execute_many_results(
                delegated_calls,
                timeout_seconds=timeout_seconds,
            )
            for position, result in zip(
                delegated_positions,
                delegated_results,
                strict=True,
            ):
                results[position] = result
        return [
            result if result is not None else self._denied_result(calls[index][0])
            for index, result in enumerate(results)
        ]

    def _denied_result(self, name: str) -> ToolResult:
        return ToolResult.failure(
            name,
            self._denied_message(name),
            ToolErrorType.POLICY_DENIED,
            retryable=False,
        )

    def _denied_message(self, name: str) -> str:
        return (
            f"Tool denied: {name!r} is outside the {self.scope_name} capability scope"
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _accesses_conflict(left: ResourceAccess, right: ResourceAccess) -> bool:
    if left.resource == "*" or right.resource == "*":
        return True
    if not _resources_overlap(left, right):
        return False
    return left.mode is ResourceMode.WRITE or right.mode is ResourceMode.WRITE


def _resources_overlap(left: ResourceAccess, right: ResourceAccess) -> bool:
    if left.resource == right.resource:
        return True
    left_parts = PurePosixPath(left.resource).parts
    right_parts = PurePosixPath(right.resource).parts
    if left.recursive and _is_prefix(left_parts, right_parts):
        return True
    if right.recursive and _is_prefix(right_parts, left_parts):
        return True
    return False


def _is_prefix(prefix: tuple[str, ...], value: tuple[str, ...]) -> bool:
    # PurePosixPath(".").parts is empty; a recursive project-root claim covers all.
    return len(prefix) <= len(value) and value[: len(prefix)] == prefix


__all__ = [
    "ConcurrencyPolicy",
    "ResourceAccess",
    "ResourceMode",
    "ToolErrorType",
    "ToolHandler",
    "ScopedToolRuntime",
    "ToolRegistry",
    "ToolResult",
    "ToolRisk",
    "ToolSideEffect",
    "ToolSpec",
]
