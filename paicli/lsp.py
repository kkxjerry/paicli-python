"""Post-edit diagnostics with an optional real stdio Language Server.

The default Python provider remains dependency-free and uses ``ast.parse``.
When a language-server command is configured, PaiCLI speaks the LSP JSON-RPC
protocol over stdio (initialize -> didOpen -> publishDiagnostics) and falls back
to the deterministic parser if the server is unavailable.  Diagnostics are
returned to the Agent loop and participate in its completion gate.
"""

from __future__ import annotations

import ast
import json
import os
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol, Sequence


class LspSeverity(IntEnum):
    ERROR = 1
    WARNING = 2
    INFORMATION = 3
    HINT = 4


@dataclass(frozen=True)
class LspDiagnostic:
    path: str
    line: int
    column: int
    severity: LspSeverity
    message: str
    source: str = ""


@dataclass(frozen=True)
class LspDiagnosticReport:
    path: str
    diagnostics: tuple[LspDiagnostic, ...]

    @property
    def has_errors(self) -> bool:
        return any(item.severity is LspSeverity.ERROR for item in self.diagnostics)


class DiagnosticProvider(Protocol):
    def diagnostics(self, path: Path, content: str) -> list[LspDiagnostic]: ...


class PythonDiagnosticProvider:
    """Dependency-free Python syntax fallback."""

    def diagnostics(self, path: Path, content: str) -> list[LspDiagnostic]:
        try:
            ast.parse(content, filename=str(path))
        except SyntaxError as exc:
            return [
                LspDiagnostic(
                    str(path),
                    exc.lineno or 1,
                    exc.offset or 1,
                    LspSeverity.ERROR,
                    exc.msg,
                    "python-ast",
                )
            ]
        return []


class FallbackDiagnosticProvider:
    """Use the real provider when possible and fail safely to a local check."""

    def __init__(
        self,
        primary: DiagnosticProvider,
        fallback: DiagnosticProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.last_error = ""

    def diagnostics(self, path: Path, content: str) -> list[LspDiagnostic]:
        try:
            result = self.primary.diagnostics(path, content)
            self.last_error = ""
            return result
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self.fallback.diagnostics(path, content)


class StdioLanguageServerDiagnosticProvider:
    """Minimal real LSP client for one-document diagnostics.

    A fresh server process is used for each diagnostic request.  This is slower
    than a long-lived IDE session but keeps lifecycle, cancellation, and crash
    recovery deterministic for a local CLI.  It supports servers such as
    ``pyright-langserver --stdio`` without adding a Python dependency.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        workspace_root: str | Path,
        language_id: str,
        timeout_seconds: float = 8.0,
    ) -> None:
        if not command:
            raise ValueError("language server command cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("language server timeout_seconds must be positive")
        self.command = tuple(str(value) for value in command)
        self.workspace_root = Path(workspace_root).resolve()
        self.language_id = str(language_id)
        self.timeout_seconds = float(timeout_seconds)

    def diagnostics(self, path: Path, content: str) -> list[LspDiagnostic]:
        process = subprocess.Popen(
            list(self.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            process.wait(timeout=1)
            raise RuntimeError("language server stdio is unavailable")
        messages: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue()
        reader = threading.Thread(
            target=_read_lsp_messages,
            args=(process.stdout, messages),
            name="paicli-lsp-reader",
            daemon=True,
        )
        reader.start()
        stderr_reader = threading.Thread(
            target=_drain_lsp_stderr,
            args=(process.stderr,),
            name="paicli-lsp-stderr",
            daemon=True,
        )
        stderr_reader.start()
        uri = path.resolve().as_uri()
        deadline = time.monotonic() + self.timeout_seconds
        try:
            _write_lsp_message(
                process.stdin,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "processId": os.getpid(),
                        "rootUri": self.workspace_root.as_uri(),
                        "capabilities": {
                            "textDocument": {
                                "publishDiagnostics": {
                                    "relatedInformation": True,
                                }
                            }
                        },
                        "clientInfo": {"name": "paicli-python", "version": "1.1.1"},
                    },
                },
            )
            _wait_for_response(messages, 1, deadline)
            _write_lsp_message(
                process.stdin,
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            )
            _write_lsp_message(
                process.stdin,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": uri,
                            "languageId": self.language_id,
                            "version": 1,
                            "text": content,
                        }
                    },
                },
            )
            payload = _wait_for_diagnostics(messages, uri, deadline)
            return _parse_lsp_diagnostics(path, payload)
        finally:
            try:
                _write_lsp_message(
                    process.stdin,
                    {"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": None},
                )
                _write_lsp_message(
                    process.stdin,
                    {"jsonrpc": "2.0", "method": "exit", "params": None},
                )
            except Exception:
                pass
            try:
                process.stdin.close()
            except Exception:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            # Popen does not close PIPE handles automatically.  Closing and
            # joining both readers prevents descriptor leaks across long runs.
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
            reader.join(timeout=1)
            stderr_reader.join(timeout=1)


class LspDiagnosticFormatter:
    @staticmethod
    def format(report: LspDiagnosticReport) -> str:
        if not report.diagnostics:
            return f"{report.path}: no diagnostics"
        return "\n".join(
            f"{item.path}:{item.line}:{item.column} "
            f"{item.severity.name.lower()}: {item.message}"
            for item in report.diagnostics
        )


class LspManager:
    """Project-root constrained diagnostic router."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        python_lsp_command: Sequence[str] | str | None = None,
        lsp_timeout_seconds: float = 8.0,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        fallback = PythonDiagnosticProvider()
        command = _command_value(
            python_lsp_command
            if python_lsp_command is not None
            else os.getenv("PAICLI_PYTHON_LSP_COMMAND", "")
        )
        python_provider: DiagnosticProvider = fallback
        if command:
            python_provider = FallbackDiagnosticProvider(
                StdioLanguageServerDiagnosticProvider(
                    command,
                    workspace_root=self.project_root,
                    language_id="python",
                    timeout_seconds=lsp_timeout_seconds,
                ),
                fallback,
            )
        self.providers: dict[str, DiagnosticProvider] = {".py": python_provider}

    def register(self, suffix: str, provider: DiagnosticProvider) -> None:
        normalized = suffix if suffix.startswith(".") else "." + suffix
        self.providers[normalized.lower()] = provider

    def diagnostics_for(self, raw_path: str) -> LspDiagnosticReport:
        path = (self.project_root / raw_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("diagnostic path escapes project root")
        provider = self.providers.get(path.suffix.lower())
        if provider is None or not path.is_file():
            return LspDiagnosticReport(raw_path, ())
        diagnostics = provider.diagnostics(
            path,
            path.read_text(encoding="utf-8", errors="replace"),
        )
        normalized = tuple(
            LspDiagnostic(
                raw_path,
                item.line,
                item.column,
                item.severity,
                item.message,
                item.source,
            )
            for item in diagnostics
        )
        return LspDiagnosticReport(raw_path, normalized)


def _command_value(value: Sequence[str] | str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(shlex.split(value)) if value.strip() else ()
    return tuple(str(item) for item in value if str(item))


def _write_lsp_message(stream: Any, payload: MappingLike) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stream.write(body)
    stream.flush()


MappingLike = dict[str, Any]


def _drain_lsp_stderr(stream: Any) -> None:
    """Drain server stderr so verbose language servers cannot block on PIPE."""

    try:
        while stream.read(8_192):
            pass
    except (OSError, ValueError):
        # The owner closes the pipe during shutdown to unblock this reader.
        return


def _read_lsp_messages(stream: Any, output: queue.Queue[dict[str, Any] | BaseException | None]) -> None:
    try:
        while True:
            headers: dict[str, str] = {}
            while True:
                line = stream.readline()
                if not line:
                    output.put(None)
                    return
                decoded = line.decode("ascii", errors="replace").strip()
                if not decoded:
                    break
                if ":" in decoded:
                    key, value = decoded.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            if length < 1:
                continue
            body = stream.read(length)
            if len(body) != length:
                raise EOFError("language server closed mid-message")
            value = json.loads(body.decode("utf-8"))
            if isinstance(value, dict):
                output.put(value)
    except BaseException as exc:
        output.put(exc)


def _next_lsp_message(
    messages: queue.Queue[dict[str, Any] | BaseException | None],
    deadline: float,
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("language server diagnostic timeout")
    try:
        value = messages.get(timeout=remaining)
    except queue.Empty as exc:
        raise TimeoutError("language server diagnostic timeout") from exc
    if value is None:
        raise RuntimeError("language server closed stdout")
    if isinstance(value, BaseException):
        raise RuntimeError(f"language server reader failed: {value}") from value
    return value


def _wait_for_response(
    messages: queue.Queue[dict[str, Any] | BaseException | None],
    request_id: int,
    deadline: float,
) -> dict[str, Any]:
    while True:
        value = _next_lsp_message(messages, deadline)
        if value.get("id") != request_id:
            continue
        if "error" in value:
            raise RuntimeError(f"language server initialize failed: {value['error']}")
        return value


def _wait_for_diagnostics(
    messages: queue.Queue[dict[str, Any] | BaseException | None],
    uri: str,
    deadline: float,
) -> list[dict[str, Any]]:
    while True:
        value = _next_lsp_message(messages, deadline)
        if value.get("method") != "textDocument/publishDiagnostics":
            continue
        params = value.get("params")
        if not isinstance(params, dict) or str(params.get("uri", "")) != uri:
            continue
        diagnostics = params.get("diagnostics", [])
        if not isinstance(diagnostics, list):
            raise RuntimeError("language server diagnostics must be an array")
        return [item for item in diagnostics if isinstance(item, dict)]


def _parse_lsp_diagnostics(
    path: Path,
    values: list[dict[str, Any]],
) -> list[LspDiagnostic]:
    result: list[LspDiagnostic] = []
    for value in values:
        range_value = value.get("range")
        start = range_value.get("start", {}) if isinstance(range_value, dict) else {}
        try:
            severity = LspSeverity(int(value.get("severity", LspSeverity.ERROR)))
        except (ValueError, TypeError):
            severity = LspSeverity.ERROR
        result.append(
            LspDiagnostic(
                str(path),
                int(start.get("line", 0)) + 1,
                int(start.get("character", 0)) + 1,
                severity,
                str(value.get("message", "language server diagnostic")),
                str(value.get("source", "lsp")),
            )
        )
    return result


__all__ = [
    "DiagnosticProvider",
    "FallbackDiagnosticProvider",
    "LspDiagnostic",
    "LspDiagnosticFormatter",
    "LspDiagnosticReport",
    "LspManager",
    "LspSeverity",
    "PythonDiagnosticProvider",
    "StdioLanguageServerDiagnosticProvider",
]
