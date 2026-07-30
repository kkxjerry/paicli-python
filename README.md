# PaiCLI Python Phase 1

一个用于学习的最小 ReAct 编程 Agent。它只实现第一期主链路，不包含
Plan、Memory、RAG、MCP、Skill、Multi-Agent 或 TUI。

## 主流程

```text
用户输入
  -> Agent 把消息和工具定义发送给 LLM
  -> LLM 返回普通回答或 tool_calls
  -> ToolRegistry 执行工具
  -> Agent 把 tool 结果追加到历史
  -> 再次调用 LLM
  -> LLM 不再调用工具时结束
```

核心循环位于 `paicli/agent.py`：

```python
for _step in range(1, self.max_steps + 1):
    response = self.client.chat(self.history, self.tools.definitions())

    if not response.tool_calls:
        return response.content

    for call in response.tool_calls:
        result = self.tools.execute(call.name, call.arguments)
        self.history.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": result,
        })
```

## 阅读顺序

1. `paicli/agent.py`：先理解完整 ReAct 循环。
2. `tests/test_agent.py`：用假模型观察一次工具调用如何回灌。
3. `paicli/tools.py`：理解工具的 schema 与 handler 如何绑定。
4. `paicli/llm_client.py`：最后看 HTTP 请求和供应商协议。
5. `paicli/__main__.py`：CLI 只是外壳。

## 运行测试

项目核心只使用 Python 标准库：

```bash
python3 -m unittest discover -s tests -v
```

测试使用 `FakeClient`，不需要 API Key，也不会访问网络。

## 连接模型

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
PAICLI_API_KEY=your-key
PAICLI_MODEL=your-model
PAICLI_BASE_URL=https://your-provider.example/v1
```

运行交互模式：

```bash
python3 -m paicli
```

运行单次任务：

```bash
python3 -m paicli -p "读取 README.md 并用一句话总结"
```

Shell 工具默认关闭。明确需要时才能启用：

```bash
python3 -m paicli --allow-shell
```

## 第一阶段只需要回答的问题

- 为什么必须把 assistant 的 `tool_calls` 保存进历史？
- 为什么 tool 消息必须带回相同的 `tool_call_id`？
- 模型如何从 JSON Schema 得知参数格式？
- 为什么没有工具调用就代表 ReAct 可以结束？
- 为什么工具错误也应该作为字符串回灌模型？
