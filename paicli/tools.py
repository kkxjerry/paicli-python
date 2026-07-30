"""第一期工具层：注册工具、生成 JSON Schema，并执行模型请求的工具。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# 所有工具处理函数都接收解析后的参数字典，并返回可回灌给模型的字符串。
ToolHandler = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    """把“给模型看的工具说明”和“本地执行函数”绑定在一起。"""

    # 模型在 tool_call 中使用的唯一工具名。
    name: str
    # 描述应告诉模型什么时候使用该工具。
    description: str
    # parameters 是约束工具参数的 JSON Schema。
    parameters: dict[str, Any]
    # handler 才是真正操作本地文件或运行命令的 Python 函数。
    handler: ToolHandler

    def definition(self) -> dict[str, Any]:
        """生成 OpenAI Function Calling 所要求的工具定义结构。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """集中注册、描述和分发第一期的所有工具。"""

    def __init__(
        self,
        project_root: str | Path = ".",
        *,
        allow_shell: bool = False,
    ) -> None:
        # resolve() 得到规范化绝对路径，后续用它做路径越界判断。
        self.project_root = Path(project_root).resolve()
        # Shell 具有较大副作用，所以默认关闭，必须由用户显式开启。
        self.allow_shell = allow_shell
        # 字典同时承担工具索引和重复名称检查的作用。
        self._tools: dict[str, ToolSpec] = {}
        self._register_builtin_tools()

    def definitions(self) -> list[dict[str, Any]]:
        """只返回模型需要的 Schema，不暴露本地 handler。"""

        return [tool.definition() for tool in self._tools.values()]

    def names(self) -> list[str]:
        """返回已注册工具名，供 CLI 的 /tools 命令展示。"""

        return list(self._tools)

    def execute(self, name: str, arguments_json: str) -> str:
        """解析模型给出的 JSON 参数，查找并执行对应工具。"""

        # 模型可能幻觉出不存在的工具名，必须返回错误而不是让程序崩溃。
        tool = self._tools.get(name)
        if tool is None:
            return f"Tool error: unknown tool {name!r}"

        try:
            # Function Calling 协议把 arguments 表示为 JSON 字符串。
            arguments = json.loads(arguments_json or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            return tool.handler(arguments)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            # 参数错误同样作为观察结果回灌，让模型有机会修正后重新调用。
            return f"Tool error: {exc}"
        except Exception as exc:
            # 工具内部的意外异常不能终止整个 Agent 循环。
            return f"Tool error: {type(exc).__name__}: {exc}"

    def _register(self, tool: ToolSpec) -> None:
        """注册单个工具，并保证工具名不重复。"""

        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def _register_builtin_tools(self) -> None:
        """注册第一期的五个内置工具。"""

        # read_file：只读文本文件。
        self._register(
            ToolSpec(
                name="read_file",
                description="Read a UTF-8 text file inside the project root.",
                parameters=self._object_schema(
                    {"path": {"type": "string", "description": "File path"}},
                    required=["path"],
                ),
                handler=self._read_file,
            )
        )
        # write_file：创建或覆盖文本文件。
        self._register(
            ToolSpec(
                name="write_file",
                description="Create or overwrite a UTF-8 file inside the project root.",
                parameters=self._object_schema(
                    {
                        "path": {"type": "string", "description": "File path"},
                        "content": {"type": "string", "description": "New file content"},
                    },
                    required=["path", "content"],
                ),
                handler=self._write_file,
            )
        )
        # list_dir：观察目录结构。
        self._register(
            ToolSpec(
                name="list_dir",
                description="List files and directories inside the project root.",
                parameters=self._object_schema(
                    {"path": {"type": "string", "description": "Directory path"}},
                ),
                handler=self._list_dir,
            )
        )
        # execute_command：运行 Shell，但受 allow_shell 开关控制。
        self._register(
            ToolSpec(
                name="execute_command",
                description=(
                    "Run a short shell command in the project root. "
                    "This tool may be disabled by local policy."
                ),
                parameters=self._object_schema(
                    {"command": {"type": "string", "description": "Shell command"}},
                    required=["command"],
                ),
                handler=self._execute_command,
            )
        )
        # create_project：演示一个工具也可以执行多步文件操作。
        self._register(
            ToolSpec(
                name="create_project",
                description="Create a minimal Python, Java, or Node project directory.",
                parameters=self._object_schema(
                    {
                        "name": {"type": "string", "description": "Project name"},
                        "type": {
                            "type": "string",
                            "enum": ["python", "java", "node"],
                            "description": "Project type",
                        },
                    },
                    required=["name", "type"],
                ),
                handler=self._create_project,
            )
        )

    @staticmethod
    def _object_schema(
        properties: dict[str, Any],
        *,
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        """生成严格的对象型 JSON Schema。"""

        return {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }

    def _safe_path(self, raw_path: str) -> Path:
        """把用户路径限制在项目根目录内，阻止 ``..`` 等路径穿越。"""

        candidate = (self.project_root / raw_path).resolve()
        # 必须在 resolve() 之后判断，否则符号链接也可能逃出项目目录。
        if not candidate.is_relative_to(self.project_root):
            raise ValueError("path escapes the project root")
        return candidate

    def _read_file(self, arguments: dict[str, Any]) -> str:
        """读取 UTF-8 文本，内容会直接成为 tool 消息。"""

        path = self._safe_path(str(arguments["path"]))
        if not path.is_file():
            raise ValueError(f"not a file: {arguments['path']}")
        return path.read_text(encoding="utf-8")

    def _write_file(self, arguments: dict[str, Any]) -> str:
        """写入 UTF-8 文本，并为嵌套路径自动创建父目录。"""

        path = self._safe_path(str(arguments["path"]))
        content = str(arguments["content"])
        # 教学版设置 1 MB 上限，避免模型一次写入异常大的内容。
        if len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("content exceeds the 1 MB learning-project limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {path.relative_to(self.project_root)}"

    def _list_dir(self, arguments: dict[str, Any]) -> str:
        """列出一层目录，并用 D/F 标记目录和文件。"""

        path = self._safe_path(str(arguments.get("path", ".")))
        if not path.is_dir():
            raise ValueError(f"not a directory: {arguments.get('path', '.')}")
        # 目录排在文件前面，同类条目按名称排序，保证输出稳定。
        entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name))
        if not entries:
            return "(empty directory)"
        return "\n".join(
            f"[{'D' if entry.is_dir() else 'F'}] {entry.name}"
            for entry in entries
        )

    def _execute_command(self, arguments: dict[str, Any]) -> str:
        """在项目根目录运行短命令，并返回退出码与输出。"""

        if not self.allow_shell:
            return "Tool error: execute_command is disabled; start with --allow-shell"

        command = str(arguments["command"]).strip()
        if not command:
            raise ValueError("command cannot be empty")
        try:
            # cwd 固定为 project_root；timeout 防止命令永久阻塞 Agent。
            completed = subprocess.run(
                command,
                cwd=self.project_root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "Command timed out after 60 seconds"

        # 截断输出，避免超长日志快速撑满模型上下文。
        output = (completed.stdout + completed.stderr)[:8_000]
        return f"Exit code: {completed.returncode}\n{output}".rstrip()

    def _create_project(self, arguments: dict[str, Any]) -> str:
        """根据类型创建一个最小可运行项目。"""

        project = self._safe_path(str(arguments["name"]))
        project_type = str(arguments["type"]).lower()
        # 先校验类型再创建目录，失败时不会留下半成品空目录。
        if project_type not in {"python", "java", "node"}:
            raise ValueError(f"unsupported project type: {project_type}")

        # exist_ok=False 防止模型误覆盖同名项目。
        project.mkdir(parents=True, exist_ok=False)

        if project_type == "python":
            # Python 项目包含入口文件和最小 pyproject.toml。
            (project / "main.py").write_text(
                'def main() -> None:\n    print("Hello")\n\n\nif __name__ == "__main__":\n    main()\n',
                encoding="utf-8",
            )
            (project / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
        elif project_type == "java":
            # Java 项目使用常见的 Maven 源码目录结构。
            source = project / "src" / "main" / "java"
            source.mkdir(parents=True)
            (source / "Main.java").write_text(
                'public class Main {\n    public static void main(String[] args) {\n'
                '        System.out.println("Hello");\n    }\n}\n',
                encoding="utf-8",
            )
        elif project_type == "node":
            # Node 项目包含 package.json 和 ES Module 入口。
            (project / "package.json").write_text(
                json.dumps(
                    {"name": project.name, "version": "0.1.0", "type": "module"},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (project / "index.js").write_text(
                'console.log("Hello");\n',
                encoding="utf-8",
            )
        return f"Created {project_type} project at {project.name}"
