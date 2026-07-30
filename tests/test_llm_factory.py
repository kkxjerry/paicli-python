from __future__ import annotations

import unittest

from paicli.llm_client import LlmClientFactory


class LlmFactoryTest(unittest.TestCase):
    def test_builds_provider_with_runtime_model_override(self) -> None:
        client = LlmClientFactory.create(
            "deepseek",
            environ={
                "DEEPSEEK_API_KEY": "secret",
                "DEEPSEEK_MODEL": "deepseek-reasoner",
            },
        )

        self.assertEqual("deepseek", client.provider)
        self.assertEqual("deepseek-reasoner", client.model)
        self.assertTrue(client.supports_prompt_caching)

    def test_rejects_unknown_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown provider"):
            LlmClientFactory.create("missing", environ={})


if __name__ == "__main__":
    unittest.main()
