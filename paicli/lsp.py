"""Phase 17：代码编辑后的语言诊断。

这一期建立了与 LSP 类似的数据边界，但并没有真正启动 pyright/pylsp
语言服务器。PythonDiagnosticProvider 目前只通过 ast.parse 发现语法错误，
不能发现类型错误、未定义变量或跨文件引用问题。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Protocol


class LspSeverity(IntEnum):
    """遵循 LSP 常见的严重程度数值，数字越小越严重。"""

    ERROR = 1
    WARNING = 2
    INFORMATION = 3
    HINT = 4


@dataclass(frozen=True)
class LspDiagnostic:
    """一条定位到文件、行、列的诊断信息。"""

    path: str
    line: int
    column: int
    severity: LspSeverity
    message: str
    source: str = ""


@dataclass(frozen=True)
class LspDiagnosticReport:
    """一个文件的全部诊断结果。"""

    path: str
    diagnostics: tuple[LspDiagnostic, ...]

    @property
    def has_errors(self) -> bool:
        # 警告和提示不算 error，调用者可用这个属性快速判定失败。
        return any(
            item.severity is LspSeverity.ERROR for item in self.diagnostics
        )


class DiagnosticProvider(Protocol):
    """不同语言诊断后端需实现的统一接口。"""

    def diagnostics(self, path: Path, content: str) -> list[LspDiagnostic]:
        """Return language diagnostics for one document."""


class PythonDiagnosticProvider:
    """本地 Python 语法检查替身，接口形状可以平滑替换成 pyright/pylsp。"""

    def diagnostics(self, path: Path, content: str) -> list[LspDiagnostic]:
        try:
            # ast.parse 只解析代码，不会执行文件。
            ast.parse(content, filename=str(path))
        except SyntaxError as exc:
            # Python 在极少数错误中可能不提供行列，此时回退到 1:1。
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
    """把结构化诊断转为可直接展示给用户/Agent 的文本。"""

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
    """校验项目路径，并根据文件后缀路由到诊断后端。"""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.providers: dict[str, DiagnosticProvider] = {
            ".py": PythonDiagnosticProvider()
        }

    def register(self, suffix: str, provider: DiagnosticProvider) -> None:
        # 可通过注册 .js/.java 等后缀扩展，同后缀后注册者覆盖先注册者。
        self.providers[suffix] = provider

    def diagnostics_for(self, raw_path: str) -> LspDiagnosticReport:
        # resolve + is_relative_to 防止 ../secret.py 绕出项目根目录。
        path = (self.project_root / raw_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("diagnostic path escapes project root")
        provider = self.providers.get(path.suffix)
        # 不支持的后缀和不存在的文件，统一视为“无诊断”。
        if provider is None or not path.is_file():
            return LspDiagnosticReport(raw_path, ())
        diagnostics = provider.diagnostics(
            path,
            path.read_text(encoding="utf-8", errors="replace"),
        )
        # Provider 可能使用绝对路径，对外报告统一换回 raw_path。
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
