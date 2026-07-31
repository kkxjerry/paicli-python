from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.lsp import LspDiagnosticFormatter, LspManager, LspSeverity


class LspTest(unittest.TestCase):
    def test_reports_python_syntax_error_with_location(self) -> None:
        """非法 Python 应产生 ERROR，且文本报告带文件和行号。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：写入 def 参数列表不完整的 Python 文件。
            Path(directory, "bad.py").write_text(
                "def broken(:\n    pass\n",
                encoding="utf-8",
            )
            manager = LspManager(directory)

            # Act：先取结构化报告，再渲染成文本。
            report = manager.diagnostics_for("bad.py")
            rendered = LspDiagnosticFormatter.format(report)

            # Assert：同时验证快捷属性、严重级别和位置输出。
            self.assertTrue(report.has_errors)
            self.assertEqual(LspSeverity.ERROR, report.diagnostics[0].severity)
            self.assertIn("bad.py:1", rendered)

    def test_clean_file_has_no_diagnostics(self) -> None:
        """语法正确的 Python 文件返回空诊断集。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：写入合法的单行 Python。
            Path(directory, "ok.py").write_text("value = 1\n", encoding="utf-8")

            # Act：对文件执行诊断。
            report = LspManager(directory).diagnostics_for("ok.py")

            # Assert：既没有 error，也没有其他级别的诊断。
            self.assertFalse(report.has_errors)
            self.assertEqual((), report.diagnostics)


if __name__ == "__main__":
    unittest.main()
