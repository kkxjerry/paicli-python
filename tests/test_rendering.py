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
        """折叠时只显示标题，展开后才显示完整内容。"""

        # Arrange：创建默认折叠的两行内容块。
        block = FoldableBlock("read_file", "line one\nline two")

        # Assert + Act：先验证折叠视图，再改状态验证展开视图。
        self.assertEqual("> read_file", block.render())
        block.expanded = True
        self.assertIn("line two", block.render())

    def test_inline_renderer_degrades_without_ansi(self) -> None:
        """输出不支持 ANSI 时，状态和工具结果仍然可读。"""

        # Arrange：StringIO 接住输出，并显式关闭 ANSI。
        stream = io.StringIO()
        renderer = InlineRenderer(
            stream,
            capabilities=TerminalCapabilities(False, 40, False),
        )

        # Act：依次渲染状态、工具开始、工具结果。
        renderer.status(StatusInfo("glm", "glm-4", "react", "10/100"))
        renderer.event("tool", "read_file")
        renderer.event("result", "content")

        # Assert：降级输出不丢失三类关键信息。
        output = stream.getvalue()
        self.assertIn("[status]", output)
        self.assertIn("read_file", output)
        self.assertIn("content", output)

    def test_slash_palette_filters_commands(self) -> None:
        """输入 /m 只返回以 /m 开头的命令，且按名字排序。"""

        # Arrange：注册两个 /m 和一个无关命令。
        palette = SlashPalette({"/model": "Switch model", "/mcp": "MCP", "/exit": "Exit"})

        # Act + Assert：/exit 被过滤，结果顺序稳定。
        self.assertEqual(
            [("/mcp", "MCP"), ("/model", "Switch model")],
            palette.complete("/m"),
        )


if __name__ == "__main__":
    unittest.main()
