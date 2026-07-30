"""Built-in tools and their model-facing JSON schemas."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    """Combines the schema shown to the model with its Python handler."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Registers, describes, and dispatches all Phase 1 tools."""

    def __init__(
        self,
        project_root: str | Path = ".",
        *,
        allow_shell: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.allow_shell = allow_shell
        self._tools: dict[str, ToolSpec] = {}
        self._register_builtin_tools()

    def definitions(self) -> list[dict[str, Any]]:
        """Return schemas safe to send to the model."""

        return [tool.definition() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def register(self, tool: ToolSpec) -> None:
        """对外提供扩展工具注册入口，重复名称检查仍统一由 _register 处理。"""

        self._register(tool)

    def execute(self, name: str, arguments_json: str) -> str:
        """Parse model arguments and run one registered tool."""

        tool = self._tools.get(name)
        if tool is None:
            return f"Tool error: unknown tool {name!r}"

        try:
            arguments = json.loads(arguments_json or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            return tool.handler(arguments)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return f"Tool error: {exc}"
        except Exception as exc:
            return f"Tool error: {type(exc).__name__}: {exc}"

    def execute_many(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float = 60,
    ) -> list[str]:
        """Execute independent calls concurrently and preserve input order."""

        if not calls:
            return []
        executor = ThreadPoolExecutor(max_workers=min(len(calls), 8))
        futures = [
            executor.submit(self.execute, name, arguments)
            for name, arguments in calls
        ]
        results: list[str] = []
        try:
            for future in futures:
                try:
                    results.append(future.result(timeout=timeout_seconds))
                except TimeoutError:
                    future.cancel()
                    results.append(
                        f"Tool error: timed out after {timeout_seconds:g} seconds"
                    )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return results

    def _register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def _register_builtin_tools(self) -> None:
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
        return {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        }

    # 将原本内部的 schema 构造器公开，让 RAG 等扩展模块复用相同 JSON Schema 格式。
    object_schema = _object_schema

    def _safe_path(self, raw_path: str) -> Path:
        candidate = (self.project_root / raw_path).resolve()
        if not candidate.is_relative_to(self.project_root):
            raise ValueError("path escapes the project root")
        return candidate

    def _read_file(self, arguments: dict[str, Any]) -> str:
        path = self._safe_path(str(arguments["path"]))
        if not path.is_file():
            raise ValueError(f"not a file: {arguments['path']}")
        return path.read_text(encoding="utf-8")

    def _write_file(self, arguments: dict[str, Any]) -> str:
        path = self._safe_path(str(arguments["path"]))
        content = str(arguments["content"])
        if len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("content exceeds the 1 MB learning-project limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {path.relative_to(self.project_root)}"

    def _list_dir(self, arguments: dict[str, Any]) -> str:
        path = self._safe_path(str(arguments.get("path", ".")))
        if not path.is_dir():
            raise ValueError(f"not a directory: {arguments.get('path', '.')}")
        entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name))
        if not entries:
            return "(empty directory)"
        return "\n".join(
            f"[{'D' if entry.is_dir() else 'F'}] {entry.name}"
            for entry in entries
        )

    def _execute_command(self, arguments: dict[str, Any]) -> str:
        if not self.allow_shell:
            return "Tool error: execute_command is disabled; start with --allow-shell"

        command = str(arguments["command"]).strip()
        if not command:
            raise ValueError("command cannot be empty")
        try:
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

        output = (completed.stdout + completed.stderr)[:8_000]
        return f"Exit code: {completed.returncode}\n{output}".rstrip()

    def _create_project(self, arguments: dict[str, Any]) -> str:
        project = self._safe_path(str(arguments["name"]))
        project_type = str(arguments["type"]).lower()
        if project_type not in {"python", "java", "node"}:
            raise ValueError(f"unsupported project type: {project_type}")

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
