from __future__ import annotations

import unittest
from typing import Any

from paicli.mcp import McpClient, McpSchemaSanitizer
from paicli.tools import ToolRegistry


class InMemoryTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
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
        transport = InMemoryTransport()
        client = McpClient("demo", transport)
        registry = ToolRegistry(".")

        client.initialize()
        registered = client.register_tools(registry)
        result = registry.execute(
            "mcp__demo__echo",
            '{"text":"hello MCP"}',
        )

        self.assertEqual(["mcp__demo__echo"], registered)
        self.assertEqual("hello MCP", result)
        self.assertEqual("demo", client.server_info["name"])

    def test_sanitizer_wraps_non_object_schema(self) -> None:
        schema = McpSchemaSanitizer.sanitize(
            {"type": "string", "title": "Value"}
        )

        self.assertEqual("object", schema["type"])
        self.assertNotIn("title", schema["properties"]["value"])


if __name__ == "__main__":
    unittest.main()
