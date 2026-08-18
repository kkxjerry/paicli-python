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

## A40 vLLM 已部署

2026-08-01 在 A40 服务器完成了真实部署：

- 模型：`/opt/models/Qwen3.5-9B`，对外模型名 `Qwen/Qwen3.5-9B`。
- 运行环境：`/opt/paicli-vllm`，`vLLM 0.19.0 + PyTorch 2.10.0 cu128`。
- 服务：`paicli-qwen35.service`，只监听 `127.0.0.1:8000`。
- GPU：使用第 1 张 A40，其他两张卡不占用。
- 上下文：32,768 tokens；启用 `qwen3` reasoning parser 和
  `qwen3_coder` tool-call parser，保留多模态能力。

`vLLM 0.26.0` 默认依赖 CUDA 13，但当前服务器驱动支持到 CUDA 12.8，
因此部署固定在仍支持 Qwen3.5 的 `vLLM 0.19.0`，没有升级服务器驱动。
对应的 systemd 配置留在 `deploy/paicli-qwen35.service`。

### 本机连接

服务不直接暴露到网络。本机先打开 SSH 隧道：

```bash
ssh -J <jump-host> -i ~/.ssh/<key-file> -p <ssh-port> \
  -N -L 18000:127.0.0.1:8000 <user>@<a40-host>
```

再在 `.env` 中填：

```dotenv
VLLM_BASE_URL=http://127.0.0.1:18000/v1
VLLM_MODEL=Qwen/Qwen3.5-9B
VLLM_API_KEY=
```

然后按顺序检查：

```bash
python3 -m paicli --provider vllm --check-model chat
python3 -m paicli --provider vllm --check-model tools
python3 -m paicli --provider vllm -p '请使用 read_file 读取 README.md 的第一行'
```

如果 `chat` 通过而 `tools` 失败，说明 HTTP 链路没问题，但模型本身、
chat template 或 vLLM tool-call parser 尚未正确配置。

### 单步调试真实全流程

`tests/test_vllm_live.py` 是专门留给 IDE Debug 的真实集成测试。
它默认跳过，打开 SSH 隧道后显式启用：

```bash
PAICLI_RUN_VLLM_LIVE_TEST=1 \
python3 -m unittest discover -s tests -p 'test_vllm_live.py' -v
```

在 IDE 中运行
`VllmLiveDebugTest.test_real_agent_reads_file_and_feeds_result_back`，
环境变量设为 `PAICLI_RUN_VLLM_LIVE_TEST=1`。从 `answer = agent.run(...)`
单步进入，可依次看到两次模型请求、`read_file` 执行、
`tool_call_id` 回灌和最终结束分支。

### 2026-08-01 实测记录

- `/health` 和 `/v1/models` 返回成功。
- PaiCLI `chat` 检查返回 `PAICLI_OK`。
- PaiCLI `tools` 检查生成了结构化 `probe_echo` 调用。
- 完整 Agent 流程真实执行了 `read_file(README.md)`，回灌后回答第一行。
- 图片输入成功识别了测试图表，证明多模态请求链路可用。
- 63 个日常自动化测试全部通过；额外的真实 A40 测试需显式开启。
