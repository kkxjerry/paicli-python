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

        prepared = controller.prepare(
            [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "question"},
            ]
        )

        self.assertEqual(ContextProfile.LONG, settings.profile)
        self.assertEqual(20, settings.rag_top_k)
        self.assertIn("file://guide", prepared[1]["content"])

    def test_short_window_uses_smaller_profile(self) -> None:
        settings = ContextSettings.for_model(16_000)

        self.assertEqual(ContextProfile.SHORT, settings.profile)
        self.assertEqual(5, settings.rag_top_k)
        self.assertEqual(12_800, settings.token_budget)

    def test_usage_formatter_surfaces_cached_tokens(self) -> None:
        settings = ContextSettings.for_model(
            128_000,
            supports_prompt_caching=True,
        )

        rendered = TokenUsageFormatter.format(
            TokenUsage(100, 20, 80),
            settings,
        )

        self.assertIn("cached 80", rendered)
        self.assertIn("120/", rendered)


if __name__ == "__main__":
    unittest.main()
