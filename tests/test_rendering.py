from __future__ import annotations

import io
import unittest

from paicli.rendering import (
    FoldableBlock,
    InlineRenderer,
    SlashPalette,
    StatusInfo,
    TerminalCapabilities,
)


class RenderingTest(unittest.TestCase):
    def test_foldable_block_has_stable_collapsed_and_expanded_views(self) -> None:
        block = FoldableBlock("read_file", "line one\nline two")

        self.assertEqual("> read_file", block.render())
        block.expanded = True
        self.assertIn("line two", block.render())

    def test_inline_renderer_degrades_without_ansi(self) -> None:
        stream = io.StringIO()
        renderer = InlineRenderer(
            stream,
            capabilities=TerminalCapabilities(False, 40, False),
        )

        renderer.status(StatusInfo("glm", "glm-4", "react", "10/100"))
        renderer.event("tool", "read_file")
        renderer.event("result", "content")

        output = stream.getvalue()
        self.assertIn("[status]", output)
        self.assertIn("read_file", output)
        self.assertIn("content", output)

    def test_slash_palette_filters_commands(self) -> None:
        palette = SlashPalette({"/model": "Switch model", "/mcp": "MCP", "/exit": "Exit"})

        self.assertEqual(
            [("/mcp", "MCP"), ("/model", "Switch model")],
            palette.complete("/m"),
        )


if __name__ == "__main__":
    unittest.main()
