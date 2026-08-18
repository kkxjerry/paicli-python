# PaiCLI Python

[![CI](https://github.com/kkxjerry/paicli-python/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/kkxjerry/paicli-python/actions/workflows/ci.yml)

PaiCLI Java 项目的 22 期 Python 学习实现。每一期都有独立 Git 提交和
`phase-xx-cn` 中文注释标签，可从最小 ReAct 循环逐步读到完整 Agent 工程。

这不是逐行翻译 Java，而是保留每一期的核心设计，用 Python 标准库实现
可运行、可测试的最小版本。

## 从第一期开始

```bash
git switch --detach phase-01-cn
python3 -m unittest discover -s tests -v
```

看完后进入第二期：

```bash
git diff phase-01-cn..phase-02-cn
git switch --detach phase-02-cn
python3 -m unittest discover -s tests -v
```

回到完整版本：

```bash
git switch develop
```

`phase-xx` 保留原始精简实现，`phase-xx-cn` 是功能相同、补齐中文注释的
学习版。第一次阅读建议使用 `-cn` 标签。

不要只数项目总行数。每次只看当前阶段新增的文件和测试：

```bash
git diff --stat phase-06-cn..phase-07-cn
git diff phase-06-cn..phase-07-cn -- paicli/agent.py paicli/tools.py
```

完整阶段索引见 [PHASES.md](PHASES.md)。

## 核心主链路

`paicli/agent.py` 中的 `Agent.run()` 始终是主干：

```text
用户输入
  -> 组装上下文
  -> 调用模型
  -> 收到 tool_calls
  -> 并行执行工具
  -> 回灌 tool 结果
  -> 没有 tool_calls 时结束
```

其他模块都在这条主链路周围解决一个具体问题：

- `planning.py`、`multi_agent.py`：复杂任务如何拆分和协作。
- `memory.py`、`rag.py`、`context.py`：模型应该看到哪些上下文。
- `policy.py`、`snapshot.py`：写文件和执行命令如何可控、可恢复。
- `mcp.py`、`mcp_resources.py`、`skills.py`：能力如何扩展。
- `runtime.py`、`rendering.py`、`interaction.py`：如何成为可用的 CLI 产品。

## 测试

项目核心只使用 Python 标准库。完整测试不需要 API Key，也不访问外部网络：

```bash
python3 -m unittest discover -s tests -v
```

## 连接模型

```bash
cp .env.example .env
```

旧的通用 OpenAI-compatible 配置：

```dotenv
PAICLI_API_KEY=your-key
PAICLI_MODEL=your-model
PAICLI_BASE_URL=https://your-provider.example/v1
```

或者使用第 8 期的 provider 工厂：

```dotenv
DEEPSEEK_API_KEY=your-key
DEEPSEEK_MODEL=deepseek-chat
```

```bash
python3 -m paicli --provider deepseek
```

先不运行 Agent，只检查真实模型对话和 Tool Calling：

```bash
python3 -m paicli --provider deepseek --check-model chat
python3 -m paicli --provider deepseek --check-model tools
```

A40 上已部署 `Qwen3.5-9B` vLLM 服务，服务只监听服务器回环地址。
通过 SSH 隧道连接后，将 provider 换成 `vllm`。详细说明见
[真实模型与 vLLM 接入笔记](docs/real-model-and-vllm.md)。

图片输入使用第 21 期语法：

```bash
python3 -m paicli -p '解释这张截图 @image:screen.png'
```

Shell 默认关闭，需要时显式开启：

```bash
python3 -m paicli --allow-shell
```

## 安全边界

- 文件、图片和快照路径不能越过 `--project-root`。
- Shell 默认关闭，第 6 期还会拒绝明显破坏性命令。
- Web 工具拒绝本地地址、私网 IP 和非 HTTP(S) 协议。
- Runtime API 只监听 `127.0.0.1`。
- Chrome DevTools MCP 配置只生成启动参数，不会自行启动浏览器或进程。
