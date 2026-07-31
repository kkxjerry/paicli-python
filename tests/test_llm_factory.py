from __future__ import annotations

import unittest

from paicli.llm_client import LlmClientFactory


class LlmFactoryTest(unittest.TestCase):
    def test_builds_provider_with_runtime_model_override(self) -> None:
        """验证工厂能加载 provider 默认配置，并用环境变量覆盖模型名。"""

        # Arrange + Act：选择 deepseek，传入必需 Key 和自定义 MODEL，不访问真实环境变量。
        client = LlmClientFactory.create(
            "deepseek",
            environ={
                "DEEPSEEK_API_KEY": "secret",
                "DEEPSEEK_MODEL": "deepseek-reasoner",
            },
        )

        # Assert：provider 元数据正确，模型被覆盖，同时保留 DeepSeek 支持 prompt cache 的能力标记。
        self.assertEqual("deepseek", client.provider)
        self.assertEqual("deepseek-reasoner", client.model)
        self.assertTrue(client.supports_prompt_caching)

    def test_rejects_unknown_provider(self) -> None:
        """验证未注册的 provider 会立即报错，而不是发起错误的网络请求。"""

        with self.assertRaisesRegex(ValueError, "unknown provider"):
            LlmClientFactory.create("missing", environ={})


if __name__ == "__main__":
    unittest.main()
