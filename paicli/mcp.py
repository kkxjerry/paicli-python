"""Phase 10：Model Context Protocol（MCP）的 JSON-RPC 核心。

    Agent -> ToolRegistry -> 本地 MCP handler
                              |
                              v
                         McpClient
                              |
                              v
                         JSON-RPC 2.0
                              |
                    stdio 或 HTTP Transport
                              |
                              v
                         MCP Server

MCP Server 的远程工具会被包装成普通 ToolSpec，因此 Agent 主循环无需特别区分。
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .tools import (
    ConcurrencyPolicy,
    ToolErrorType,
    ToolRegistry,
    ToolResult,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)


class McpError(RuntimeError):
    """MCP 传输、协议或远程错误的统一异常。"""


class McpTransport(Protocol):
    """屏蔽 stdio/HTTP 差异的请求-响应传输接口。"""

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON-RPC request and return its response."""

    def close(self) -> None:
        """Release transport resources."""


class JsonRpcClient:
    """为 MCP method 补齐 JSON-RPC 2.0 包装，并验证响应。"""

    def __init__(self, transport: McpTransport) -> None:
        self.transport = transport
        self._next_id = 1
        self._lock = threading.Lock()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        # 递增 ID 用于将响应匹配到请求；Lock 防止并发调用拿到相同 ID。
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
        response = self.transport.request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        if response.get("id") != request_id:
            # ID 不匹配意味着传输层把其他请求的响应交了回来。
            raise McpError("JSON-RPC response id does not match request")
        if "error" in response:
            error = response["error"]
            raise McpError(f"{error.get('code')}: {error.get('message')}")
        if "result" not in response:
            raise McpError("JSON-RPC response has no result")
        return response["result"]


class StdioTransport:
    """通过子进程 stdin/stdout 传输每行一个 JSON-RPC 消息。"""

    def __init__(self, command: list[str]) -> None:
        if not command:
            raise ValueError("MCP command cannot be empty")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # 本期忽略 server stderr，生产实现通常应收集为可诊断日志。
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise McpError("stdio transport is closed")
        # stdin/stdout 是同一条流，写请求和读响应必须在同一把锁中串行完成。
        with self._lock:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
        if not line:
            raise McpError("MCP server closed stdout")
        return json.loads(line)

    def close(self) -> None:
        # poll() 返回 None 代表子进程仍在运行。
        if self.process.poll() is None:
            self.process.terminate()


class StreamableHttpTransport:
    """使用 HTTP POST 传输 JSON-RPC；本期未实现 SSE/会话等完整 Streamable HTTP 特性。"""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout_seconds = timeout_seconds

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **self.headers},
            method="POST",
        )
        with urllib.request.urlopen(
            request,
            timeout=self.timeout_seconds,
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def close(self) -> None:
        # urllib 每次请求的 response 已由 with 关闭，无持久连接需释放。
        return None


@dataclass(frozen=True)
class McpToolDescriptor:
    """MCP tools/list 中一个远程工具的本地表示。"""

    name: str
    description: str
    input_schema: dict[str, Any]


class McpSchemaSanitizer:
    """删除模型工具接口不需要的 schema 元数据，并强制根节点为 object。"""

    @classmethod
    def sanitize(cls, schema: object) -> dict[str, Any]:
        if not isinstance(schema, dict):
            # 远程 Server 返回非对象 schema 时退化为无参对象。
            return {"type": "object", "properties": {}}
        cleaned = cls._clean(schema)
        if cleaned.get("type") != "object":
            # OpenAI 式 tools 的参数根节点需要 object，原 schema 放入 value 字段中。
            cleaned = {
                "type": "object",
                "properties": {"value": cleaned},
                "required": ["value"],
            }
        cleaned.setdefault("properties", {})
        return cleaned

    @classmethod
    def _clean(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._clean(item)
                for key, item in value.items()
                if key not in {"$schema", "examples", "title"}
            }
        if isinstance(value, list):
            return [cls._clean(item) for item in value]
        return value


class McpClient:
    """实现 MCP 初始化、工具发现、调用和本地注册。"""

    def __init__(
        self,
        name: str,
        transport: McpTransport,
        *,
        protocol_version: str = "2025-03-26",
    ) -> None:
        self.name = name
        self.transport = transport
        self.rpc = JsonRpcClient(transport)
        self.protocol_version = protocol_version
        self.capabilities: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}

    def initialize(self) -> dict[str, Any]:
        # 客户端先声明协议版本和自身信息，Server 返回能力与身份。
        result = self.rpc.call(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "paicli-python", "version": "0.1.0"},
            },
        )
        self.capabilities = dict(result.get("capabilities", {}))
        self.server_info = dict(result.get("serverInfo", {}))
        return result

    def list_tools(self) -> list[McpToolDescriptor]:
        # 在转成本地描述对象时同步清理 inputSchema。
        result = self.rpc.call("tools/list")
        return [
            McpToolDescriptor(
                name=str(item["name"]),
                description=str(item.get("description", "")),
                input_schema=McpSchemaSanitizer.sanitize(
                    item.get("inputSchema", {})
                ),
            )
            for item in result.get("tools", [])
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        # MCP 结果 content 可包含多种块，本期只收集 text 块。
        result = self.rpc.call(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        content = result.get("content", [])
        parts = [
            str(item.get("text", ""))
            for item in content
            if item.get("type") == "text"
        ]
        if result.get("isError"):
            return "MCP tool error: " + "\n".join(parts)
        return "\n".join(parts) or json.dumps(result, ensure_ascii=False)

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        registered: list[str] = []
        for descriptor in self.list_tools():
            # 加 Server 命名空间，避免多个 Server 都有 read_file 时冲突。
            local_name = f"mcp__{self.name}__{descriptor.name}"

            def handler(
                arguments: dict[str, Any],
                # 默认参数在定义时冻结当前 descriptor/name，避免 Python 闭包晚绑定问题。
                remote_name: str = descriptor.name,
                tool_name: str = local_name,
            ) -> str | ToolResult:
                result = self.call_tool(remote_name, arguments)
                if result.startswith("MCP tool error:"):
                    return ToolResult.failure(
                        tool_name,
                        result,
                        ToolErrorType.EXECUTION_ERROR,
                        # MCP does not declare idempotency or side effects in
                        # the base tool descriptor; an error may follow a
                        # partial remote action, so automatic retry is unsafe.
                        retryable=False,
                    )
                return result

            registry.register(
                ToolSpec(
                    local_name,
                    f"[MCP {self.name}] {descriptor.description}",
                    descriptor.input_schema,
                    handler,
                    # An external MCP schema does not declare side effects in
                    # the base protocol. Unknown tools therefore fail closed:
                    # policy may request approval and the scheduler serializes
                    # them until richer metadata is available.
                    risk=ToolRisk.UNKNOWN,
                    side_effect=ToolSideEffect.UNKNOWN,
                    concurrency=ConcurrencyPolicy.SERIAL,
                )
            )
            registered.append(local_name)
        return registered

    def close(self) -> None:
        # McpClient 不直接知道子进程或 HTTP，统一委托给 Transport。
        self.transport.close()
