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
        cache = McpResourceCache()
        cache.put(McpResourceContent("file://readme", "hello"))

        self.assertEqual("hello", cache.get("file://readme").text)  # type: ignore[union-attr]
        cache.invalidate("file://readme")
        self.assertIsNone(cache.get("file://readme"))

    def test_parses_and_expands_resource_mentions(self) -> None:
        mentions = AtMentionParser.parse("Review @docs:file://guide.md please")
        expander = AtMentionExpander(
            lambda server, uri: f"{server} returned {uri}"
        )

        expanded = expander.expand("Review @docs:file://guide.md please")

        self.assertEqual("docs", mentions[0].server)
        self.assertIn("<resource", expanded)
        self.assertIn("docs returned file://guide.md", expanded)

    def test_notification_router_and_cancellation(self) -> None:
        seen = []
        router = NotificationRouter()
        router.subscribe("resources/list_changed", seen.append)

        handled = router.route(
            {"method": "resources/list_changed", "params": {"server": "docs"}}
        )
        token = CancellationToken()
        token.cancel()

        self.assertTrue(handled)
        self.assertEqual("docs", seen[0]["server"])
        with self.assertRaises(CancelledError):
            token.check()


if __name__ == "__main__":
    unittest.main()
