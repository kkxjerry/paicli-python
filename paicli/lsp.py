"""Phase 17: language diagnostics after code edits."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Protocol


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
        return any(
            item.severity is LspSeverity.ERROR for item in self.diagnostics
        )


class DiagnosticProvider(Protocol):
    def diagnostics(self, path: Path, content: str) -> list[LspDiagnostic]:
        """Return language diagnostics for one document."""


class PythonDiagnosticProvider:
    """Local stand-in with the same boundary as a pyright/pylsp adapter."""

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
                    "python",
                )
            ]
        return []


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
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.providers: dict[str, DiagnosticProvider] = {
            ".py": PythonDiagnosticProvider()
        }

    def register(self, suffix: str, provider: DiagnosticProvider) -> None:
        self.providers[suffix] = provider

    def diagnostics_for(self, raw_path: str) -> LspDiagnosticReport:
        path = (self.project_root / raw_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("diagnostic path escapes project root")
        provider = self.providers.get(path.suffix)
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
