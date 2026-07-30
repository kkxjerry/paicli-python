from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.lsp import LspDiagnosticFormatter, LspManager, LspSeverity


class LspTest(unittest.TestCase):
    def test_reports_python_syntax_error_with_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "bad.py").write_text(
                "def broken(:\n    pass\n",
                encoding="utf-8",
            )
            manager = LspManager(directory)

            report = manager.diagnostics_for("bad.py")
            rendered = LspDiagnosticFormatter.format(report)

            self.assertTrue(report.has_errors)
            self.assertEqual(LspSeverity.ERROR, report.diagnostics[0].severity)
            self.assertIn("bad.py:1", rendered)

    def test_clean_file_has_no_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "ok.py").write_text("value = 1\n", encoding="utf-8")

            report = LspManager(directory).diagnostics_for("ok.py")

            self.assertFalse(report.has_errors)
            self.assertEqual((), report.diagnostics)


if __name__ == "__main__":
    unittest.main()
