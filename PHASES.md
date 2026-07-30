# 22 期学习索引

每一期都先读测试，再读实现。测试会直接告诉你模块的输入、输出和边界。

| 期数 | 核心能力 | 先看测试 | 再看实现 |
|---:|---|---|---|
| 01 | ReAct + Tool Call（推荐 `phase-01-cn`） | `tests/test_agent.py` | `paicli/agent.py`、`tools.py` |
| 02 | Plan-and-Execute + DAG | `tests/test_planning.py` | `paicli/planning.py` |
| 03 | 短期/长期记忆 + 压缩 | `tests/test_memory.py` | `paicli/memory.py` |
| 04 | 代码分块 + RAG 检索 | `tests/test_rag.py` | `paicli/rag.py` |
| 05 | Multi-Agent + 角色通信 | `tests/test_multi_agent.py` | `paicli/multi_agent.py` |
| 06 | HITL + 命令策略 + 审计 | `tests/test_policy.py` | `paicli/policy.py` |
| 07 | 并行工具执行 | `tests/test_parallel_tools.py` | `paicli/agent.py`、`tools.py` |
| 08 | 多模型 Provider 工厂 | `tests/test_llm_factory.py` | `paicli/llm_client.py` |
| 09 | Web 搜索、抓取和 SSRF 防护 | `tests/test_web_tools.py` | `paicli/web_tools.py` |
| 10 | MCP JSON-RPC + 两种 Transport | `tests/test_mcp.py` | `paicli/mcp.py` |
| 11 | MCP Resources、@Mention、取消 | `tests/test_mcp_resources.py` | `paicli/mcp_resources.py`、`runtime.py` |
| 12 | 长上下文 + Token Budget | `tests/test_context.py` | `paicli/context.py` |
| 13 | Chrome DevTools MCP | `tests/test_browser.py` | `paicli/browser.py` |
| 14 | CDP 会话复用 + 显式记忆 | `tests/test_phase14.py` | `paicli/browser.py`、`memory.py` |
| 15 | Skill 发现和按需加载 | `tests/test_skills.py` | `paicli/skills.py` |
| 16 | Renderer + Inline TUI | `tests/test_rendering.py` | `paicli/rendering.py` |
| 17 | 编辑后 LSP 诊断 | `tests/test_lsp.py` | `paicli/lsp.py` |
| 18 | 可恢复文件快照 | `tests/test_snapshot.py` | `paicli/snapshot.py` |
| 19 | 分层 Prompt | `tests/test_prompts.py` | `paicli/prompts.py` |
| 20 | 后台任务 + Runtime API | `tests/test_runtime.py` | `paicli/runtime.py` |
| 21 | 图片引用 + 多模态消息 | `tests/test_images.py` | `paicli/images.py` |
| 22 | Slash 命令、历史、状态栏 | `tests/test_interaction.py` | `paicli/interaction.py`、`__main__.py` |

## 推荐学习路线

### 第一段：Agent 主循环

先看 `phase-01`、`phase-02`、`phase-07`。回答三个问题：

1. 模型为什么不是直接执行工具？
2. 为什么工具结果必须重新放回消息历史？
3. 哪些任务可以并行，哪些任务必须等待依赖？

### 第二段：上下文工程

再看 `phase-03`、`phase-04`、`phase-12`、`phase-19`。重点区分：

- Memory 保存过去发生的事。
- RAG 从代码库检索当前问题相关内容。
- Context 决定这一轮允许放多少内容。
- Prompt 决定不同来源的上下文如何稳定排序。

### 第三段：扩展与安全

看 `phase-06`、`phase-09`、`phase-10`、`phase-11`、`phase-15`：

- Tool 是项目内部函数。
- MCP 是外部能力协议。
- Skill 是按需加载的操作说明。
- HITL、路径围栏、网络策略负责限制副作用。

### 第四段：工程产品化

最后看 `phase-16` 到 `phase-22`。这些阶段不再改变 ReAct 的本质，而是在补：

- 诊断、恢复和后台执行能力。
- 图片等输入形式。
- CLI 状态、命令、历史与可读输出。

## 常用比较命令

查看某期新增文件：

```bash
git diff --name-status phase-14..phase-15
```

查看某期代码量：

```bash
git diff --stat phase-14..phase-15
```

不切换分支直接读取某一期文件：

```bash
git show phase-03:paicli/memory.py
```

运行当前标签的测试：

```bash
python3 -m unittest discover -s tests -v
```
