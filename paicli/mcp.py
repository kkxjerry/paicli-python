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
import queue
import subprocess
import threading
import time
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
    """通过子进程 stdin/stdout 传输每行一个 JSON-RPC 消息。

    A dedicated reader prevents a silent MCP process from blocking the whole
    Agent tool gateway forever. Requests remain serialized because one stdio
    stream cannot safely interleave request/response pairs without a dispatcher.
    """

    def __init__(
        self,
        command: list[str],
        *,
        timeout_seconds: float = 30.0,
        stderr_limit: int = 16_000,
    ) -> None:
        if not command:
            raise ValueError("MCP command cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("MCP timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.stderr_limit = max(1_000, int(stderr_limit))
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._stderr_parts: list[str] = []
        self.notifications: list[dict[str, Any]] = []
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="paicli-mcp-stdout",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            name="paicli-mcp-stderr",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise McpError("stdio transport is closed")
        expected_id = payload.get("id")
        deadline = time.monotonic() + self.timeout_seconds
        with self._lock:
            if self.process.poll() is not None:
                raise McpError(self._closed_message())
            try:
                self.process.stdin.write(json.dumps(payload) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise McpError(self._closed_message()) from exc

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpError(
                        f"MCP stdio request timed out after {self.timeout_seconds:g} seconds"
                    )
                try:
                    line = self._responses.get(timeout=remaining)
                except queue.Empty as exc:
                    raise McpError(
                        f"MCP stdio request timed out after {self.timeout_seconds:g} seconds"
                    ) from exc
                if line is None:
                    raise McpError(self._closed_message())
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise McpError(
                        f"MCP server returned invalid JSON: {line[:500]}"
                    ) from exc
                if not isinstance(value, dict):
                    raise McpError("MCP server response must be a JSON object")

                # MCP notifications have no id and may legally arrive before a
                # response. Preserve a bounded diagnostic history and continue
                # waiting for the response belonging to this serialized call.
                if "id" not in value:
                    self.notifications.append(value)
                    if len(self.notifications) > 100:
                        del self.notifications[:-100]
                    continue
                if expected_id is not None and value.get("id") != expected_id:
                    raise McpError(
                        "MCP stdio response id does not match the active request"
                    )
                return value

    def close(self) -> None:
        # Close stdin first so cooperative servers can exit naturally, then
        # enforce a bounded termination.  Popen leaves PIPE wrappers open unless
        # the owner closes them explicitly.
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except OSError:
                pass
        self._reader.join(timeout=1)
        self._stderr_reader.join(timeout=1)

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        if stream is None:
            self._responses.put(None)
            return
        try:
            for line in stream:
                self._responses.put(line.rstrip("\r\n"))
        finally:
            self._responses.put(None)

    def _read_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        for chunk in iter(lambda: stream.read(1024), ""):
            if not chunk:
                break
            self._stderr_parts.append(chunk)
            joined = "".join(self._stderr_parts)
            if len(joined) > self.stderr_limit:
                self._stderr_parts = [joined[-self.stderr_limit :]]

    def _closed_message(self) -> str:
        detail = "".join(self._stderr_parts).strip()
        suffix = f"; stderr: {detail[-2_000:]}" if detail else ""
        return f"MCP server exited with code {self.process.poll()}{suffix}"


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
                "clientInfo": {"name": "paicli-python", "version": "1.1.0"},
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
