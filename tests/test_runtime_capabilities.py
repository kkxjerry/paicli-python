from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.bootstrap import build_application_runtime
from paicli.llm_client import ChatResponse


class _Client:
    model = "fake"
    provider = "fake"
    context_window = 32_000
    supports_prompt_caching = False

    def chat(self, messages, tools):  # type: ignore[no-untyped-def]
        return ChatResponse("done")


class RuntimeCapabilityTest(unittest.TestCase):
    def test_matrix_reports_reachable_default_product_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            runtime = build_application_runtime(
                _Client(),
                root,
                enable_trace=False,
                enable_hitl=False,
                memory_path=root / "memory.db",
                rag_path=root / ".paicli" / "code.db",
            )
            try:
                matrix = runtime.capability_matrix()
                tools = set(matrix["tools"])
                self.assertEqual(("react", "plan", "team"), matrix["modes"])
                self.assertTrue(matrix["memory"])
                self.assertTrue(matrix["rag"])
                self.assertFalse(matrix["extensions"])
                self.assertTrue(
                    {
                        "read_file",
                        "write_file",
                        "replace_text",
                        "multi_edit",
                        "apply_patch",
                        "grep",
                        "glob",
                        "execute_command",
                        "search_code",
                        "save_memory",
                    }.issubset(tools)
                )
                self.assertNotIn("web_fetch", tools)
                self.assertNotIn("load_skill", tools)
                self.assertFalse(any(name.startswith("mcp__") for name in tools))
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
