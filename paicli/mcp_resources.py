"""Phase 11: MCP resources, mentions, notifications, and cancellation."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .mcp import McpClient
from .tools import ToolRegistry, ToolSpec


@dataclass(frozen=True)
class McpResourceDescriptor:
    uri: str
    name: str
    description: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class McpResourceContent:
    uri: str
    text: str
    mime_type: str = ""


class McpResourceCache:
    def __init__(self, ttl_seconds: float = 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[float, McpResourceContent]] = {}

    def get(self, uri: str) -> McpResourceContent | None:
        item = self._items.get(uri)
        if not item:
            return None
        created_at, content = item
        if time.monotonic() - created_at > self.ttl_seconds:
            self._items.pop(uri, None)
            return None
        return content

    def put(self, content: McpResourceContent) -> None:
        self._items[content.uri] = (time.monotonic(), content)

    def invalidate(self, uri: str | None = None) -> None:
        if uri is None:
            self._items.clear()
        else:
            self._items.pop(uri, None)


class McpResourceClient:
    def __init__(
        self,
        client: McpClient,
        cache: McpResourceCache | None = None,
    ) -> None:
        self.client = client
        self.cache = cache or McpResourceCache()

    def list_resources(self) -> list[McpResourceDescriptor]:
        result = self.client.rpc.call("resources/list")
        return [
            McpResourceDescriptor(
                uri=str(item["uri"]),
                name=str(item.get("name", item["uri"])),
                description=str(item.get("description", "")),
                mime_type=str(item.get("mimeType", "")),
            )
            for item in result.get("resources", [])
        ]

    def read_resource(self, uri: str) -> McpResourceContent:
        cached = self.cache.get(uri)
        if cached:
            return cached
        result = self.client.rpc.call("resources/read", {"uri": uri})
        contents = result.get("contents", [])
        if not contents:
            raise ValueError(f"MCP resource is empty: {uri}")
        item = contents[0]
        content = McpResourceContent(
            uri=str(item.get("uri", uri)),
            text=str(item.get("text", "")),
            mime_type=str(item.get("mimeType", "")),
        )
        self.cache.put(content)
        return content

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        prefix = f"mcp__{self.client.name}"
        registry.register(
            ToolSpec(
                f"{prefix}__list_resources",
                f"List resources exposed by MCP server {self.client.name}.",
                registry.object_schema({}),
                lambda _arguments: "\n".join(
                    f"{resource.uri}\t{resource.description}"
                    for resource in self.list_resources()
                ),
            )
        )
        registry.register(
            ToolSpec(
                f"{prefix}__read_resource",
                f"Read one resource from MCP server {self.client.name}.",
                registry.object_schema(
                    {"uri": {"type": "string"}},
                    required=["uri"],
                ),
                lambda arguments: self.read_resource(str(arguments["uri"])).text,
            )
        )
        return [
            f"{prefix}__list_resources",
            f"{prefix}__read_resource",
        ]


@dataclass(frozen=True)
class ResourceMention:
    server: str
    uri: str
    raw: str


class AtMentionParser:
    PATTERN = re.compile(r"@([A-Za-z0-9_-]+):([^\s]+)")

    @classmethod
    def parse(cls, text: str) -> list[ResourceMention]:
        return [
            ResourceMention(match.group(1), match.group(2), match.group(0))
            for match in cls.PATTERN.finditer(text)
        ]


class AtMentionExpander:
    def __init__(self, reader: Callable[[str, str], str]) -> None:
        self.reader = reader

    def expand(self, text: str) -> str:
        expanded = text
        for mention in reversed(AtMentionParser.parse(text)):
            content = self.reader(mention.server, mention.uri)
            block = (
                f"<resource server=\"{mention.server}\" uri=\"{mention.uri}\">\n"
                f"{content}\n</resource>"
            )
            expanded = expanded.replace(mention.raw, block, 1)
        return expanded


class NotificationRouter:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def subscribe(
        self,
        method: str,
        handler: Callable[[dict[str, Any]], None],
    ) -> None:
        self._handlers.setdefault(method, []).append(handler)

    def route(self, message: dict[str, Any]) -> bool:
        method = message.get("method")
        if not isinstance(method, str):
            return False
        handlers = self._handlers.get(method, [])
        for handler in handlers:
            handler(dict(message.get("params") or {}))
        return bool(handlers)
