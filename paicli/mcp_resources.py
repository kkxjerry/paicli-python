"""Phase 11：MCP Resources、@Mention、通知路由和取消。

Tools 是“执行动作”，Resources 是“读取上下文资料”。
@docs:file://guide.md 可被展开为 resource 文本后再发给模型。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .mcp import McpClient
from .tools import (
    ConcurrencyPolicy,
    ToolRegistry,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)


@dataclass(frozen=True)
class McpResourceDescriptor:
    """resources/list 返回的资源元数据。"""

    uri: str
    name: str
    description: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class McpResourceContent:
    """resources/read 返回的文本内容。"""

    uri: str
    text: str
    mime_type: str = ""


class McpResourceCache:
    """带 TTL 的内存资源缓存，避免频繁请求同一 MCP Resource。"""

    def __init__(self, ttl_seconds: float = 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[float, McpResourceContent]] = {}

    def get(self, uri: str) -> McpResourceContent | None:
        item = self._items.get(uri)
        if not item:
            return None
        created_at, content = item
        # monotonic 不受系统时钟调整影响，更适合计算 TTL。
        if time.monotonic() - created_at > self.ttl_seconds:
            self._items.pop(uri, None)
            return None
        return content

    def put(self, content: McpResourceContent) -> None:
        self._items[content.uri] = (time.monotonic(), content)

    def invalidate(self, uri: str | None = None) -> None:
        # uri=None 表示整个 Server 资源可能已变更，需清空全部缓存。
        if uri is None:
            self._items.clear()
        else:
            self._items.pop(uri, None)


class McpResourceClient:
    """在 McpClient JSON-RPC 之上提供 Resource 领域方法。"""

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
        # 缓存命中时不再调用 resources/read。
        cached = self.cache.get(uri)
        if cached:
            return cached
        result = self.client.rpc.call("resources/read", {"uri": uri})
        contents = result.get("contents", [])
        if not contents:
            raise ValueError(f"MCP resource is empty: {uri}")
        # MCP 可返回多个 content block，本期只取第一个文本块。
        item = contents[0]
        content = McpResourceContent(
            uri=str(item.get("uri", uri)),
            text=str(item.get("text", "")),
            mime_type=str(item.get("mimeType", "")),
        )
        self.cache.put(content)
        return content

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        # Resources 也包装为两个常规工具，使 Agent 可按需列出和读取。
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
                risk=ToolRisk.SAFE,
                side_effect=ToolSideEffect.READ_ONLY,
                concurrency=ConcurrencyPolicy.PARALLEL,
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
                risk=ToolRisk.SAFE,
                side_effect=ToolSideEffect.READ_ONLY,
                concurrency=ConcurrencyPolicy.PARALLEL,
            )
        )
        return [
            f"{prefix}__list_resources",
            f"{prefix}__read_resource",
        ]


@dataclass(frozen=True)
class ResourceMention:
    """从 @server:uri 中解析出的结构化引用。"""

    server: str
    uri: str
    raw: str


class AtMentionParser:
    """识别消息中的 @MCP-resource 引用。"""

    PATTERN = re.compile(r"@([A-Za-z0-9_-]+):([^\s]+)")

    @classmethod
    def parse(cls, text: str) -> list[ResourceMention]:
        return [
            ResourceMention(match.group(1), match.group(2), match.group(0))
            for match in cls.PATTERN.finditer(text)
        ]


class AtMentionExpander:
    """通过注入的 reader 将 @Mention 替换为带来源标记的完整资源。"""

    def __init__(self, reader: Callable[[str, str], str]) -> None:
        self.reader = reader

    def expand(self, text: str) -> str:
        expanded = text
        # 从后往前处理，概念上可避免前面替换影响后续引用。
        for mention in reversed(AtMentionParser.parse(text)):
            content = self.reader(mention.server, mention.uri)
            block = (
                f"<resource server=\"{mention.server}\" uri=\"{mention.uri}\">\n"
                f"{content}\n</resource>"
            )
            expanded = expanded.replace(mention.raw, block, 1)
        return expanded


class NotificationRouter:
    """将无 ID 的 MCP notification 按 method 分发给本地订阅者。"""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def subscribe(
        self,
        method: str,
        handler: Callable[[dict[str, Any]], None],
    ) -> None:
        # 同一 method 允许多个 handler，按注册顺序执行。
        self._handlers.setdefault(method, []).append(handler)

    def route(self, message: dict[str, Any]) -> bool:
        # 返回值表示是否至少有一个 handler 处理，便于上层记录未知通知。
        method = message.get("method")
        if not isinstance(method, str):
            return False
        handlers = self._handlers.get(method, [])
        for handler in handlers:
            handler(dict(message.get("params") or {}))
        return bool(handlers)
