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

    def test_builds_dashscope_from_its_native_environment_contract(self) -> None:
        client = LlmClientFactory.create(
            "dashscope",
            environ={
                "DASHSCOPE_API_KEY": "secret",
                "DASHSCOPE_MODEL": "qwen-plus",
                "DASHSCOPE_BASE_URL": (
                    "https://dashscope.aliyuncs.com/compatible-mode/v1"
                ),
                "DASHSCOPE_CONTEXT_WINDOW": "200000",
                "DASHSCOPE_TIMEOUT_SECONDS": "45",
            },
        )

        self.assertEqual("dashscope", client.provider)
        self.assertEqual("qwen-plus", client.model)
        self.assertEqual(200_000, client.context_window)
        self.assertEqual(45.0, client.timeout_seconds)
        self.assertTrue(client.supports_prompt_caching)

    def test_provider_runtime_numbers_are_validated(self) -> None:
        base = {"DASHSCOPE_API_KEY": "secret"}
        with self.assertRaisesRegex(ValueError, "TIMEOUT_SECONDS"):
            LlmClientFactory.create(
                "dashscope",
                environ={**base, "DASHSCOPE_TIMEOUT_SECONDS": "zero"},
            )
        with self.assertRaisesRegex(ValueError, "CONTEXT_WINDOW"):
            LlmClientFactory.create(
                "dashscope",
                environ={**base, "DASHSCOPE_CONTEXT_WINDOW": "100"},
            )

    def test_rejects_unknown_provider(self) -> None:
        """验证未注册的 provider 会立即报错，而不是发起错误的网络请求。"""

        with self.assertRaisesRegex(ValueError, "unknown provider"):
            LlmClientFactory.create("missing", environ={})

    def test_builds_vllm_without_api_key(self) -> None:
        """A40 上的 vLLM 可以不设 API Key，但必须明确配置模型和地址。"""

        # Arrange + Act：只提供将来服务器就绪后能确定的两项配置。
        client = LlmClientFactory.create(
            "vllm",
            environ={
                "VLLM_MODEL": "local-tool-model",
                "VLLM_BASE_URL": "http://a40.example:8000/v1",
            },
        )

        # Assert：内网无鉴权模式使用占位 Key，其他连接信息保持不变。
        self.assertEqual("vllm", client.provider)
        self.assertEqual("EMPTY", client.api_key)
        self.assertEqual("local-tool-model", client.model)
        self.assertEqual("http://a40.example:8000/v1", client.base_url)

    def test_vllm_requires_explicit_endpoint_and_model(self) -> None:
        """服务器未准备好时立即报配置错误，不误连本机端口。"""

        with self.assertRaisesRegex(ValueError, "VLLM_MODEL"):
            LlmClientFactory.create("vllm", environ={})
        with self.assertRaisesRegex(ValueError, "VLLM_BASE_URL"):
            LlmClientFactory.create(
                "vllm",
                environ={"VLLM_MODEL": "local-tool-model"},
            )


if __name__ == "__main__":
    unittest.main()
