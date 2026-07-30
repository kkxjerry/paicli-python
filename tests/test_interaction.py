from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.interaction import (
    CliCommandParser,
    PaiCliHistory,
    StatusDock,
    normalize_input,
)


class InteractionTest(unittest.TestCase):
    def test_parses_commands_without_confusing_regular_prompts(self) -> None:
        """行首 /model 被解析为命令，句子中的 /model 仍是普通 prompt。"""

        # Act：分别解析真命令和内含斜杠文本的普通问题。
        command = CliCommandParser.parse("/model deepseek")

        # Assert：命令名/参数拆分正确，普通问题返回 None。
        self.assertEqual("model", command.name)  # type: ignore[union-attr]
        self.assertEqual(("deepseek",), command.arguments)  # type: ignore[union-attr]
        self.assertIsNone(CliCommandParser.parse("explain /model"))

    def test_history_persists_and_deduplicates_adjacent_entries(self) -> None:
        """相邻重复输入只保留一次，且重新建对象可从磁盘恢复。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange + Act：写入 first 两次、second 一次，然后重新加载。
            path = Path(directory, "history.json")
            history = PaiCliHistory(path)
            history.add("first")
            history.add("first")
            history.add("second")

            loaded = PaiCliHistory(path)

            # Assert：顺序保留，连续重复被去除。
            self.assertEqual(["first", "second"], loaded.entries)

    def test_status_dock_never_exceeds_terminal_width(self) -> None:
        """状态栏必须受宽度约束，输入换行符也要归一化。"""

        # Arrange：故意使用超过 20 字符的模型信息。
        dock = StatusDock("deepseek", "a-very-long-model-name", activity="thinking")

        # Act + Assert：渲染结果恰好截断到 20，CRLF/CR 被正常清理。
        self.assertEqual(20, len(dock.render(20)))
        self.assertEqual("hello", normalize_input("\r\nhello\r\n"))


if __name__ == "__main__":
    unittest.main()
