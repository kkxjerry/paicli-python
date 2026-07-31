from __future__ import annotations

import unittest

from paicli.context import (
    ContextController,
    ContextProfile,
    ContextSettings,
    ResourceIndexEntry,
    TokenUsage,
    TokenUsageFormatter,
)


class ContextTest(unittest.TestCase):
    def test_long_window_expands_rag_and_injects_resource_index(self) -> None:
        """验证 256K 窗口选择 LONG 档，提高 RAG top_k 并注入 MCP Resource 索引。"""

        # Arrange：模型窗口 256K，支持 prompt cache，并有一条 docs 资源。
        settings = ContextSettings.for_model(
            256_000,
            supports_prompt_caching=True,
        )
        controller = ContextController(
            settings,
            resources=[
                ResourceIndexEntry("docs", "file://guide", "Project guide")
            ],
        )

        # Act：准备一次最小 system + user 输入。
        prepared = controller.prepare(
            [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "question"},
            ]
        )

        # Assert：长档 top_k=20，资源目录插在 system 消息之后。
        self.assertEqual(ContextProfile.LONG, settings.profile)
        self.assertEqual(20, settings.rag_top_k)
        self.assertIn("file://guide", prepared[1]["content"])

    def test_short_window_uses_smaller_profile(self) -> None:
        """验证 16K 模型选择 SHORT 档并仅使用 80% 窗口作为输入预算。"""

        # Act：16K < 32K，应选 SHORT。
        settings = ContextSettings.for_model(16_000)

        # Assert：RAG 候选更少，预算为 16000 * 0.8 = 12800。
        self.assertEqual(ContextProfile.SHORT, settings.profile)
        self.assertEqual(5, settings.rag_top_k)
        self.assertEqual(12_800, settings.token_budget)

    def test_usage_formatter_surfaces_cached_tokens(self) -> None:
        """验证当 provider 支持 prompt cache 时，状态文本会显示缓存 token。"""

        settings = ContextSettings.for_model(
            128_000,
            supports_prompt_caching=True,
        )

        # Act：100 输入 + 20 输出 = 120 总 token，其中 80 输入命中缓存。
        rendered = TokenUsageFormatter.format(
            TokenUsage(100, 20, 80),
            settings,
        )

        # Assert：缓存数和总用量均可见。
        self.assertIn("cached 80", rendered)
        self.assertIn("120/", rendered)


if __name__ == "__main__":
    unittest.main()
