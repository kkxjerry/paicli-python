"""Phase 10: Model Context Protocol JSON-RPC core."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .tools import ToolRegistry, ToolSpec


class McpError(RuntimeError):
    pass


class McpTransport(Protocol):
    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON-RPC request and return its response."""

    def close(self) -> None:
        """Release transport resources."""


class JsonRpcClient:
    def __init__(self, transport: McpTransport) -> None:
        self.transport = transport
        self._next_id = 1
        self._lock = threading.Lock()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
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
            raise McpError("JSON-RPC response id does not match request")
        if "error" in response:
            error = response["error"]
            raise McpError(f"{error.get('code')}: {error.get('message')}")
        if "result" not in response:
            raise McpError("JSON-RPC response has no result")
        return response["result"]


class StdioTransport:
    """Newline-delimited JSON-RPC transport for local MCP servers."""

    def __init__(self, command: list[str]) -> None:
        if not command:
            raise ValueError("MCP command cannot be empty")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.process.stdin is None or self.process.stdout is None:
            raise McpError("stdio transport is closed")
        with self._lock:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
        if not line:
            raise McpError("MCP server closed stdout")
        return json.loads(line)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()


class StreamableHttpTransport:
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
        return None


@dataclass(frozen=True)
class McpToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]


class McpSchemaSanitizer:
    @classmethod
    def sanitize(cls, schema: object) -> dict[str, Any]:
        if not isinstance(schema, dict):
            return {"type": "object", "properties": {}}
        cleaned = cls._clean(schema)
        if cleaned.get("type") != "object":
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
            local_name = f"mcp__{self.name}__{descriptor.name}"

            def handler(
                arguments: dict[str, Any],
                remote_name: str = descriptor.name,
            ) -> str:
                return self.call_tool(remote_name, arguments)

            registry.register(
                ToolSpec(
                    local_name,
                    f"[MCP {self.name}] {descriptor.description}",
                    descriptor.input_schema,
                    handler,
                )
            )
            registered.append(local_name)
        return registered

    def close(self) -> None:
        self.transport.close()
