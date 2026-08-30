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
  -> 没有 tool_calls 时进入 completion policy
  -> 验证通过才结束，否则反馈后继续
```

其他模块都在这条主链路周围解决一个具体问题：

Phase 0-5 的 Java 行为同步已经把执行内核和三类基础设施收敛为可复用组件：

- 默认硬轮数由 20 调整为 50。
- 连续 3 轮相同的工具调用和观察结果会判定为停滞。
- 可通过 `--token-budget` 设置单次运行的硬 Token 上限；默认不限制。
- OpenAI-compatible `usage` 会累计到结构化 `AgentOutcome`。
- `Agent.run()` 继续返回字符串；编排代码可用 `Agent.run_outcome()` 读取结束原因、Token 和改动文件。
- 模型返回空内容且没有工具调用时，不再直接视为成功完成。
- 工具参数在执行端做 JSON Schema 校验，结果用 `ToolResult` 区分参数错误、策略拒绝、超时和执行错误。
- 同轮工具按资源读写冲突分批；同一路径读写或写写不会再无条件并发。
- 默认 CLI 已接入上下文、LLM 历史摘要、长期记忆、`save_memory` 和写后 LSP 诊断。
- `LlmPlanner` 可为复杂任务生成计划，`PlanValidator` 与 `DagScheduler` 负责统一 DAG 校验、拓扑序和批次。

完整对齐边界见 [Java → Python behavior parity ledger](docs/java-parity.md)，实现留痕见
[Phase 3-5 implementation notes](docs/phase-03-05-implementation.md)。

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

循环保护默认是 50 轮上限、连续 3 轮无进展停止；自动化任务还可以设置硬 Token 上限：

```bash
python3 -m paicli --max-steps 50 --stagnation-window 3 --token-budget 50000
```

默认长期记忆写入 `~/.paicli/memory.jsonl`，也可以指定路径或关闭：

```bash
python3 -m paicli --memory-file .paicli/memory.jsonl
python3 -m paicli --no-memory
```

Phase 5 已提供可直接调用的 LLM Planner，但 `/plan` 与 `/team` 尚未接入 CLI：

```python
from paicli import LlmPlanner

planner = LlmPlanner(client)
plan = planner.create_plan("Inspect the implementation, edit it, and run tests")
print(plan.render())
```

## 安全边界

- 文件、图片和快照路径不能越过 `--project-root`。
- Shell 默认关闭，第 6 期还会拒绝明显破坏性命令。
- Web 工具拒绝本地地址、私网 IP 和非 HTTP(S) 协议。
- Runtime API 只监听 `127.0.0.1`。
- Chrome DevTools MCP 配置只生成启动参数，不会自行启动浏览器或进程。
