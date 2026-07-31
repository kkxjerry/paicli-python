"""工具层：注册工具、生成 JSON Schema，并执行模型请求的工具。"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# 所有 handler 都接收已解析的参数字典，并返回可回灌给模型的字符串。
ToolHandler = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    """把“给模型看的工具说明”和“本地 Python handler”绑定在一起。"""

    # 模型在 tool_call 中使用的唯一名称。
    name: str
    # 描述用来帮模型判断什么时候应调用该工具。
    description: str
    # parameters 是约束工具参数的 JSON Schema。
    parameters: dict[str, Any]
    # handler 才是真正访问文件系统/命令行的函数。
    handler: ToolHandler

    def definition(self) -> dict[str, Any]:
        """生成 OpenAI Function Calling 要求的工具定义。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """集中注册、描述和分发项目内的工具。"""

    def __init__(
        self,
        project_root: str | Path = ".",
        *,
        allow_shell: bool = False,
    ) -> None:
        # 规范化为绝对路径，后续所有文件工具都以它为安全边界。
        self.project_root = Path(project_root).resolve()
        # Shell 副作用大，默认关闭，必须由用户显式开启。
        self.allow_shell = allow_shell
        # 字典同时用于按名路由和防止重复注册。
        self._tools: dict[str, ToolSpec] = {}
        self._register_builtin_tools()

    def definitions(self) -> list[dict[str, Any]]:
        """只返回可发给模型的 Schema，不暴露本地 handler。"""

        return [tool.definition() for tool in self._tools.values()]

    def names(self) -> list[str]:
        """返回已注册工具名，供 CLI /tools 展示。"""

        return list(self._tools)

    def register(self, tool: ToolSpec) -> None:
        """对外提供扩展工具注册入口，重复名称检查仍统一由 _register 处理。"""

        self._register(tool)

    def execute(self, name: str, arguments_json: str) -> str:
        """解析模型给出的 JSON 参数，查找并执行对应工具。"""

        # 模型可能幻觉出未注册工具，错误需作为 tool 结果回灌，而不是让进程崩溃。
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
            # 参数错误回灌后，模型还有机会修正并重试。
            return f"Tool error: {exc}"
        except Exception as exc:
            # 工具内部意外异常也不能终止整个 Agent 循环。
            return f"Tool error: {type(exc).__name__}: {exc}"

    def execute_many(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float = 60,
    ) -> list[str]:
        """并行执行互不依赖的工具调用，但返回顺序与输入保持一致。"""

        if not calls:
            return []
        # 线程数不超过调用数，也不超过 8，避免模型一次返回大量调用时无限建线程。
        executor = ThreadPoolExecutor(max_workers=min(len(calls), 8))
        # submit 会立即把所有任务交给线程池，这一步实现真正并发。
        futures = [
            executor.submit(self.execute, name, arguments)
            for name, arguments in calls
        ]
        results: list[str] = []
        try:
            # futures 按输入顺序遍历，因此即使第二个先完成，返回结果仍不会乱序。
            for future in futures:
                try:
                    results.append(future.result(timeout=timeout_seconds))
                except TimeoutError:
                    # cancel 只能取消还没开始的任务；已在运行的 Python 线程无法强制终止。
                    future.cancel()
                    results.append(
                        f"Tool error: timed out after {timeout_seconds:g} seconds"
                    )
        finally:
            # 不等待超时任务结束，并取消尚未开始的 future。
            executor.shutdown(wait=False, cancel_futures=True)
        return results

    def _register(self, tool: ToolSpec) -> None:
        """注册单个工具，并保证工具名不重复。"""

        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def _register_builtin_tools(self) -> None:
        """注册读、写、列目录、执行命令和创建项目五个内置工具。"""

        # read_file：只读 UTF-8 文本。
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
        # write_file：创建或覆盖 UTF-8 文本。
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
        # list_dir：观察一层目录结构。
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
        # execute_command：受 allow_shell 显式开关约束。
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
        # create_project：演示一个工具内完成多步文件写入。
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
        """生成不允许额外字段的严格 object JSON Schema。"""

        return {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }

    # 将原本内部的 schema 构造器公开，让 RAG 等扩展模块复用相同 JSON Schema 格式。
    object_schema = _object_schema

    def _safe_path(self, raw_path: str) -> Path:
        """把路径限制在项目根目录，拒绝 ``..`` 和符号链接越界。"""

        candidate = (self.project_root / raw_path).resolve()
        # 必须在 resolve 后判断，否则符号链接可能绕过字符串层的检查。
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
        """写入 UTF-8 文本，并自动创建嵌套父目录。"""

        path = self._safe_path(str(arguments["path"]))
        content = str(arguments["content"])
        # 学习版设置 1 MB 上限，避免模型一次写入异常大内容。
        if len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("content exceeds the 1 MB learning-project limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {path.relative_to(self.project_root)}"

    def _list_dir(self, arguments: dict[str, Any]) -> str:
        """列出一层目录，并用 D/F 区分目录和文件。"""

        path = self._safe_path(str(arguments.get("path", ".")))
        if not path.is_dir():
            raise ValueError(f"not a directory: {arguments.get('path', '.')}")
        # 目录在前、文件在后，同类按名字排序，使输出稳定。
        entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name))
        if not entries:
            return "(empty directory)"
        return "\n".join(
            f"[{'D' if entry.is_dir() else 'F'}] {entry.name}"
            for entry in entries
        )

    def _execute_command(self, arguments: dict[str, Any]) -> str:
        """在项目根目录执行短命令，并返回退出码与截断后输出。"""

        if not self.allow_shell:
            return "Tool error: execute_command is disabled; start with --allow-shell"

        command = str(arguments["command"]).strip()
        if not command:
            raise ValueError("command cannot be empty")
        try:
            # cwd 固定为 project_root，timeout 避免命令永久阻塞 Agent。
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

        # 截断超长日志，避免快速撑满模型上下文。
        output = (completed.stdout + completed.stderr)[:8_000]
        return f"Exit code: {completed.returncode}\n{output}".rstrip()

    def _create_project(self, arguments: dict[str, Any]) -> str:
        """根据类型创建最小可运行项目。"""

        project = self._safe_path(str(arguments["name"]))
        project_type = str(arguments["type"]).lower()
        # 先校验再创建目录，失败时不留半成品。
        if project_type not in {"python", "java", "node"}:
            raise ValueError(f"unsupported project type: {project_type}")

        # exist_ok=False 防止意外覆盖同名项目。
        project.mkdir(parents=True, exist_ok=False)

        if project_type == "python":
            (project / "main.py").write_text(
                'def main() -> None:\n    print("Hello")\n\n\nif __name__ == "__main__":\n    main()\n',
                encoding="utf-8",
            )
            (project / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
        elif project_type == "java":
            source = project / "src" / "main" / "java"
            source.mkdir(parents=True)
            (source / "Main.java").write_text(
                'public class Main {\n    public static void main(String[] args) {\n'
                '        System.out.println("Hello");\n    }\n}\n',
                encoding="utf-8",
            )
        elif project_type == "node":
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
