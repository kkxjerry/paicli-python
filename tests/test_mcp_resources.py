from __future__ import annotations

import unittest

from paicli.mcp_resources import (
    AtMentionExpander,
    AtMentionParser,
    McpResourceCache,
    McpResourceContent,
    NotificationRouter,
)
from paicli.runtime import CancellationToken, CancelledError


class McpResourcesTest(unittest.TestCase):
    def test_resource_cache_can_be_invalidated(self) -> None:
        """验证 Resource 可放入缓存，也可按 URI 精确失效。"""

        # Arrange：放入一条 file://readme 资源。
        cache = McpResourceCache()
        cache.put(McpResourceContent("file://readme", "hello"))

        # Assert + Act + Assert：先命中，失效后再查询就返回 None。
        self.assertEqual("hello", cache.get("file://readme").text)  # type: ignore[union-attr]
        cache.invalidate("file://readme")
        self.assertIsNone(cache.get("file://readme"))

    def test_parses_and_expands_resource_mentions(self) -> None:
        """验证 @server:uri 可被解析，并通过 reader 展开为 resource XML 块。"""

        # Arrange：假 reader 把 server/uri 原样拼回，便于断言。
        mentions = AtMentionParser.parse("Review @docs:file://guide.md please")
        expander = AtMentionExpander(
            lambda server, uri: f"{server} returned {uri}"
        )

        # Act：展开同一段文本。
        expanded = expander.expand("Review @docs:file://guide.md please")

        self.assertEqual("docs", mentions[0].server)
        self.assertIn("<resource", expanded)
        self.assertIn("docs returned file://guide.md", expanded)

    def test_notification_router_and_cancellation(self) -> None:
        """验证通知分发和协作式取消两个独立运行时能力。"""

        # Arrange：seen.append 作为订阅 handler。
        seen = []
        router = NotificationRouter()
        router.subscribe("resources/list_changed", seen.append)

        # Act：路由 resources/list_changed，然后取消令牌。
        handled = router.route(
            {"method": "resources/list_changed", "params": {"server": "docs"}}
        )
        token = CancellationToken()
        token.cancel()

        # Assert：通知被处理且参数到达 handler；check() 抛 CancelledError。
        self.assertTrue(handled)
        self.assertEqual("docs", seen[0]["server"])
        with self.assertRaises(CancelledError):
            token.check()


if __name__ == "__main__":
    unittest.main()
