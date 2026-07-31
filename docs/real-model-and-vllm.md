# 真实模型与 A40 vLLM 接入笔记

## 哪些是假的

`tests/test_agent.py` 里的 `FakeClient` 会提前放好响应。它只验证：

1. Agent 能否看懂 `tool_calls`。
2. Agent 是否真的执行工具。
3. 工具结果是否通过 `tool_call_id` 回灌。
4. 模型给出最终答案后是否停止循环。

它不能证明真实模型会正确选工具。这是单元测试的有意隔离，
不是正式 CLI 使用的假模型。

## 现在已经是真的部分

`OpenAICompatibleClient` 会真正请求：

```text
{BASE_URL}/chat/completions
```

`tests/test_llm_http.py` 也会真正经过本机 HTTP socket，验证请求 JSON、
Authorization 和 tool call 解析。它不消耗云端 token。

## 使用云端密钥检查

密钥只写入本地 `.env`，不写入源码和测试。例如：

```dotenv
DEEPSEEK_API_KEY=your-key
DEEPSEEK_MODEL=deepseek-chat
```

先检查普通对话：

```bash
python3 -m paicli --provider deepseek --check-model chat
```

再检查真实 Tool Calling：

```bash
python3 -m paicli --provider deepseek --check-model tools
```

`tools` 检查只让模型生成 `probe_echo` 调用，不会真正运行 Shell 或写文件。

## A40 vLLM 服务器就绪后

现在不需要启动本地 Docker，也不将服务器地址硬编码到仓库。
等 A40 上的 OpenAI-compatible vLLM 服务可用后，在 `.env` 中填：

```dotenv
VLLM_BASE_URL=http://A40_SERVER:8000/v1
VLLM_MODEL=the-served-model-name
VLLM_API_KEY=
```

然后按顺序检查：

```bash
python3 -m paicli --provider vllm --check-model chat
python3 -m paicli --provider vllm --check-model tools
python3 -m paicli --provider vllm -p '列出当前目录文件'
```

如果 `chat` 通过而 `tools` 失败，说明 HTTP 链路没问题，但模型本身、
chat template 或 vLLM tool-call parser 尚未正确配置。

图片输入要放在 Tool Calling 通过之后再测。它还要求所部署模型本身是视觉语言模型。
