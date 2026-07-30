from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.tools import ToolRegistry


class ToolRegistryTest(unittest.TestCase):
    def test_write_read_and_list(self) -> None:
        """验证三个文件工具可以通过统一的 execute 入口调用。"""

        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)

            write_result = registry.execute(
                "write_file",
                '{"path":"notes/a.txt","content":"alpha"}',
            )
            read_result = registry.execute(
                "read_file",
                '{"path":"notes/a.txt"}',
            )
            list_result = registry.execute(
                "list_dir",
                '{"path":"notes"}',
            )

            self.assertEqual("Wrote notes/a.txt", write_result)
            self.assertEqual("alpha", read_result)
            self.assertIn("[F] a.txt", list_result)

    def test_path_cannot_escape_project_root(self) -> None:
        """包含 .. 的路径不能读取项目目录之外的文件。"""

        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)

            result = registry.execute(
                "read_file",
                '{"path":"../../outside.txt"}',
            )

            self.assertIn("path escapes the project root", result)

    def test_shell_is_disabled_by_default(self) -> None:
        """Shell 必须由用户显式开启，默认不能执行。"""

        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)

            result = registry.execute(
                "execute_command",
                '{"command":"pwd"}',
            )

            self.assertIn("execute_command is disabled", result)

    def test_create_python_project(self) -> None:
        """create_project 能生成最小 Python 项目。"""

        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)

            result = registry.execute(
                "create_project",
                '{"name":"demo","type":"python"}',
            )

            self.assertEqual("Created python project at demo", result)
            self.assertTrue(Path(directory, "demo", "main.py").is_file())

    def test_invalid_project_type_does_not_leave_directory(self) -> None:
        """参数非法时不能在磁盘上留下半成品目录。"""

        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)

            result = registry.execute(
                "create_project",
                '{"name":"broken","type":"unknown"}',
            )

            self.assertIn("unsupported project type", result)
            self.assertFalse(Path(directory, "broken").exists())


if __name__ == "__main__":
    unittest.main()
