"""Tool gateway: registration, validation, policy metadata, and scheduling.

``execute()`` and ``execute_many()`` retain the original string-returning API.
The shared Agent loop uses ``execute_result()`` / ``execute_many_results()`` to
receive typed failures, elapsed time, resource claims, and changed files.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Protocol

from .command_policy import CommandGuard
from .file_editing import (
    FileMutation,
    TextEdit,
    patch_paths,
    patch_preview,
    prepare_patch,
    prepare_text_edits,
    sha256_file,
    sha256_text,
    unified_diff,
    write_mutations,
)
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


class ToolGateway(Protocol):
    """Catalog plus structured execution surface used by every Agent mode."""

    def definitions(self) -> list[dict[str, Any]]: ...
    def names(self) -> list[str]: ...
    def spec(self, name: str) -> ToolSpec | None: ...
    def execute(self, name: str, arguments_json: str) -> str: ...
    def execute_result(self, name: str, arguments_json: str) -> ToolResult: ...
    def execute_many_results(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> list[ToolResult]: ...


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
    MAX_WRITE_BYTES = 10_000_000
    MAX_SEARCH_FILE_BYTES = 2_000_000
    DEFAULT_IGNORED_DIRECTORIES = {
        ".git",
        ".paicli",
        ".venv",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
    }

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
                        },
                        "start_line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Optional 1-indexed inclusive start line",
                        },
                        "end_line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Optional 1-indexed inclusive end line",
                        },
                        "include_sha256": {
                            "type": "boolean",
                            "description": "Prefix the result with the file SHA-256",
                        },
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
                        "expected_sha256": {
                            "type": "string",
                            "pattern": "^[0-9a-fA-F]{64}$",
                            "description": "Optional optimistic-concurrency hash of the current file",
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
                "replace_text",
                "Replace an exact text block in one file with optimistic concurrency checks.",
                self._object_schema(
                    {
                        "path": {"type": "string", "minLength": 1},
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                        "expected_replacements": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                        },
                        "expected_sha256": {
                            "type": "string",
                            "pattern": "^[0-9a-fA-F]{64}$",
                        },
                    },
                    required=["path", "old_text", "new_text"],
                ),
                self._replace_text,
                risk=ToolRisk.MEDIUM,
                side_effect=ToolSideEffect.FILE_WRITE,
                concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                resource_resolver=lambda args: self._path_access(
                    args["path"], ResourceMode.WRITE
                ),
                previewer=self._preview_replace_text,
            )
        )
        self._register(
            ToolSpec(
                "multi_edit",
                "Atomically validate and apply multiple exact text edits.",
                self._object_schema(
                    {
                        "edits": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 100,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "minLength": 1},
                                    "old_text": {"type": "string", "minLength": 1},
                                    "new_text": {"type": "string"},
                                    "expected_replacements": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 1000,
                                    },
                                    "expected_sha256": {
                                        "type": "string",
                                        "pattern": "^[0-9a-fA-F]{64}$",
                                    },
                                },
                                "required": ["path", "old_text", "new_text"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    required=["edits"],
                ),
                self._multi_edit,
                risk=ToolRisk.MEDIUM,
                side_effect=ToolSideEffect.FILE_WRITE,
                concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                resource_resolver=self._multi_edit_accesses,
                previewer=self._preview_multi_edit,
            )
        )
        self._register(
            ToolSpec(
                "apply_patch",
                "Apply a validated multi-file unified diff atomically per file.",
                self._object_schema(
                    {
                        "patch": {"type": "string", "minLength": 1},
                        "expected_sha256": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "string",
                                "pattern": "^[0-9a-fA-F]{64}$",
                            },
                        },
                    },
                    required=["patch"],
                ),
                self._apply_patch,
                risk=ToolRisk.MEDIUM,
                side_effect=ToolSideEffect.FILE_WRITE,
                concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                resource_resolver=self._patch_accesses,
                previewer=lambda args: patch_preview(
                    self.project_root, str(args["patch"])
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
                "grep",
                "Search project text files and return path:line matches.",
                self._object_schema(
                    {
                        "pattern": {"type": "string", "minLength": 1},
                        "path": {"type": "string", "minLength": 1},
                        "file_glob": {"type": "string", "minLength": 1},
                        "regex": {"type": "boolean"},
                        "case_sensitive": {"type": "boolean"},
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                        },
                    },
                    required=["pattern"],
                ),
                self._grep,
                risk=ToolRisk.SAFE,
                side_effect=ToolSideEffect.READ_ONLY,
                concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                resource_resolver=lambda args: self._path_access(
                    args.get("path", "."), ResourceMode.READ, recursive=True
                ),
            )
        )
        self._register(
            ToolSpec(
                "glob",
                "List project files matching a glob pattern.",
                self._object_schema(
                    {
                        "pattern": {"type": "string", "minLength": 1},
                        "path": {"type": "string", "minLength": 1},
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 2000,
                        },
                    },
                    required=["pattern"],
                ),
                self._glob,
                risk=ToolRisk.SAFE,
                side_effect=ToolSideEffect.READ_ONLY,
                concurrency=ConcurrencyPolicy.RESOURCE_LOCKED,
                resource_resolver=lambda args: self._path_access(
                    args.get("path", "."), ResourceMode.READ, recursive=True
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
        content = path.read_text(encoding="utf-8")
        start = int(arguments.get("start_line", 1))
        end_raw = arguments.get("end_line")
        if end_raw is not None and int(end_raw) < start:
            raise ValueError("end_line cannot be smaller than start_line")
        if start != 1 or end_raw is not None:
            lines = content.splitlines(keepends=True)
            end = len(lines) if end_raw is None else int(end_raw)
            content = "".join(lines[start - 1 : end])
        if arguments.get("include_sha256"):
            content = f"SHA256: {sha256_file(path)}\n" + content
        return content

    def _write_file(self, arguments: dict[str, Any]) -> str:
        path = self._safe_path(arguments["path"])
        content = str(arguments["content"])
        if len(content.encode("utf-8")) > self.MAX_WRITE_BYTES:
            raise ValueError(
                f"content exceeds write limit ({self.MAX_WRITE_BYTES} bytes)"
            )
        before = path.read_text(encoding="utf-8") if path.is_file() else None
        expected = str(arguments.get("expected_sha256", "")).strip()
        if expected:
            if before is None:
                raise ValueError("expected_sha256 was supplied but the file does not exist")
            actual = sha256_text(before)
            if actual.lower() != expected.lower():
                raise ValueError(
                    f"file hash changed: expected {expected}, found {actual}"
                )
        if before == content:
            raise ValueError("write produces no textual change")
        relative = path.relative_to(self.project_root).as_posix()
        write_mutations(
            self.project_root,
            (FileMutation(relative, before, content),),
        )
        return f"Wrote {relative}"

    def _replace_text(self, arguments: dict[str, Any]) -> ToolResult:
        edit = TextEdit(
            str(arguments["path"]),
            str(arguments["old_text"]),
            str(arguments["new_text"]),
            int(arguments.get("expected_replacements", 1)),
            str(arguments.get("expected_sha256", "")),
        )
        mutations = prepare_text_edits(self.project_root, (edit,))
        if not mutations:
            raise ValueError("replacement produces no textual change")
        changed = write_mutations(self.project_root, mutations)
        return ToolResult.success(
            "replace_text",
            "Applied exact replacement:\n" + "".join(item.diff for item in mutations),
            changed_files=changed,
        )

    def _multi_edit(self, arguments: dict[str, Any]) -> ToolResult:
        edits = tuple(
            TextEdit(
                str(item["path"]),
                str(item["old_text"]),
                str(item["new_text"]),
                int(item.get("expected_replacements", 1)),
                str(item.get("expected_sha256", "")),
            )
            for item in arguments["edits"]
        )
        mutations = prepare_text_edits(self.project_root, edits)
        if not mutations:
            raise ValueError("multi_edit produces no textual change")
        changed = write_mutations(self.project_root, mutations)
        return ToolResult.success(
            "multi_edit",
            "Applied edits:\n" + "".join(item.diff for item in mutations),
            changed_files=changed,
        )

    def _apply_patch(self, arguments: dict[str, Any]) -> ToolResult:
        mutations = prepare_patch(
            self.project_root,
            str(arguments["patch"]),
            expected_sha256=arguments.get("expected_sha256"),
        )
        changed = write_mutations(self.project_root, mutations)
        if not changed:
            raise ValueError("patch produces no textual change")
        return ToolResult.success(
            "apply_patch",
            "Applied patch:\n" + "".join(item.diff for item in mutations),
            changed_files=changed,
        )

    def _preview_replace_text(self, arguments: dict[str, Any]) -> str:
        mutations = prepare_text_edits(
            self.project_root,
            (
                TextEdit(
                    str(arguments["path"]),
                    str(arguments["old_text"]),
                    str(arguments["new_text"]),
                    int(arguments.get("expected_replacements", 1)),
                    str(arguments.get("expected_sha256", "")),
                ),
            ),
        )
        return "".join(item.diff for item in mutations) or "(no textual change)"

    def _preview_multi_edit(self, arguments: dict[str, Any]) -> str:
        edits = tuple(
            TextEdit(
                str(item["path"]),
                str(item["old_text"]),
                str(item["new_text"]),
                int(item.get("expected_replacements", 1)),
                str(item.get("expected_sha256", "")),
            )
            for item in arguments["edits"]
        )
        return "".join(
            item.diff for item in prepare_text_edits(self.project_root, edits)
        ) or "(no textual change)"

    def _multi_edit_accesses(
        self,
        arguments: dict[str, Any],
    ) -> tuple[ResourceAccess, ...]:
        return tuple(
            self._path_access(item["path"], ResourceMode.WRITE)[0]
            for item in arguments["edits"]
        )

    def _patch_accesses(
        self,
        arguments: dict[str, Any],
    ) -> tuple[ResourceAccess, ...]:
        return tuple(
            self._path_access(path, ResourceMode.WRITE)[0]
            for path in patch_paths(str(arguments["patch"]))
        )

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

    def _grep(self, arguments: dict[str, Any]) -> str:
        base = self._safe_path(arguments.get("path", "."))
        if not base.exists():
            raise ValueError(f"search path does not exist: {arguments.get('path', '.')}")
        pattern = str(arguments["pattern"])
        use_regex = bool(arguments.get("regex", False))
        case_sensitive = bool(arguments.get("case_sensitive", False))
        file_glob = str(arguments.get("file_glob", "*"))
        max_results = int(arguments.get("max_results", 100))
        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(pattern if use_regex else re.escape(pattern), flags)
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        results: list[str] = []
        for path in candidates:
            if not path.is_file() or self._ignored_search_path(path):
                continue
            relative = path.relative_to(self.project_root)
            if file_glob and not (
                relative.match(file_glob) or path.name == file_glob
            ):
                continue
            try:
                if path.stat().st_size > self.MAX_SEARCH_FILE_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line) is None:
                    continue
                results.append(
                    f"{relative.as_posix()}:{line_number}:{line[:500]}"
                )
                if len(results) >= max_results:
                    return "\n".join(results)
        return "\n".join(results) if results else "(no matches)"

    def _glob(self, arguments: dict[str, Any]) -> str:
        base = self._safe_path(arguments.get("path", "."))
        if not base.is_dir():
            raise ValueError(f"glob path is not a directory: {arguments.get('path', '.')}")
        pattern = str(arguments["pattern"])
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("glob pattern must stay inside the project root")
        max_results = int(arguments.get("max_results", 500))
        results: list[str] = []
        for path in sorted(base.glob(pattern)):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(self.project_root):
                continue
            if self._ignored_search_path(resolved):
                continue
            relative = resolved.relative_to(self.project_root).as_posix()
            results.append(relative + ("/" if resolved.is_dir() else ""))
            if len(results) >= max_results:
                break
        return "\n".join(results) if results else "(no matches)"

    def _ignored_search_path(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.project_root)
        except ValueError:
            return True
        return any(
            part in self.DEFAULT_IGNORED_DIRECTORIES for part in relative.parts
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
        blocked = CommandGuard.reject_reason(command)
        if blocked:
            raise ToolExecutionFailure(
                blocked,
                ToolErrorType.POLICY_DENIED,
                retryable=False,
            )
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
        registry: ToolGateway,
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
        registry: ToolGateway,
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
    "ToolGateway",
    "ToolHandler",
    "ScopedToolRuntime",
    "ToolRegistry",
    "ToolResult",
    "ToolRisk",
    "ToolSideEffect",
    "ToolSpec",
]
