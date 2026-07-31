from __future__ import annotations

import unittest
from typing import Any

from paicli.mcp import McpClient, McpSchemaSanitizer
from paicli.tools import ToolRegistry


class InMemoryTransport:
    """在内存中模拟 MCP Server，同时记录所有 JSON-RPC 请求。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        # 根据 method 返回固定协议响应，让测试无需启动真实 MCP 进程。
        method = payload["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo text",
                        "inputSchema": {
                            "$schema": "ignored",
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": payload["params"]["arguments"]["text"],
                    }
                ]
            }
        else:
            raise AssertionError(method)
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}

    def close(self) -> None:
        return None


class McpTest(unittest.TestCase):
    def test_initializes_and_registers_remote_tool(self) -> None:
        """验证从 initialize、tools/list 到本地注册和 tools/call 的完整最小链路。"""

        # Arrange：McpClient 使用假 Transport，ToolRegistry 用于接收远程工具。
        transport = InMemoryTransport()
        client = McpClient("demo", transport)
        registry = ToolRegistry(".")

        # Act：握手，发现 echo，注册为 mcp__demo__echo，再从统一工具入口调用。
        client.initialize()
        registered = client.register_tools(registry)
        result = registry.execute(
            "mcp__demo__echo",
            '{"text":"hello MCP"}',
        )

        # Assert：本地名字带命名空间，返回远程文本，并保存 Server 信息。
        self.assertEqual(["mcp__demo__echo"], registered)
        self.assertEqual("hello MCP", result)
        self.assertEqual("demo", client.server_info["name"])

    def test_sanitizer_wraps_non_object_schema(self) -> None:
        """验证根节点为 string 的远程 schema 会被包装成 object，并删除 title。"""

        # Act：原 schema 不符合工具参数必须是对象的约定。
        schema = McpSchemaSanitizer.sanitize(
            {"type": "string", "title": "Value"}
        )

        # Assert：根节点已是 object，原 string schema 在 properties.value 中且 title 被清理。
        self.assertEqual("object", schema["type"])
        self.assertNotIn("title", schema["properties"]["value"])


if __name__ == "__main__":
    unittest.main()
