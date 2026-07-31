# Phase01 学习笔记：ReAct 与工具调用

## 1. 本期目标

Phase01 只解决一个核心问题：

> 模型需要读取文件或执行操作时，Agent 如何调用本地工具，并把工具结果交还给模型？

完整流程：

```text
用户输入
  -> Agent 调用模型
  -> 模型返回普通回答或 tool_calls
  -> Agent 执行工具
  -> Agent 把工具结果写入 history
  -> Agent 再次调用模型
  -> 模型返回最终回答
```

模型只负责决定“调用什么工具”，真正执行本地代码的是 Agent。

## 2. 核心文件

| 文件 | 职责 |
|---|---|
| `paicli/agent.py` | 控制 ReAct 循环 |
| `paicli/tools.py` | 注册并执行本地工具 |
| `paicli/llm_client.py` | 调用模型并统一响应结构 |
| `paicli/__main__.py` | 读取配置并启动 CLI |
| `tests/test_agent.py` | 使用 FakeClient 测试 Agent |
| `tests/test_tools.py` | 测试文件工具与安全边界 |

推荐阅读顺序：

```text
test_agent.py
  -> Agent.run()
  -> ToolRegistry.execute()
  -> ToolCall / ChatResponse / LlmClient
  -> OpenAICompatibleClient
  -> __main__.py
```

## 3. 三个核心模型类型

### ToolCall

表示模型要求 Agent 执行的一次工具调用：

```python
ToolCall(
    id="call-1",
    name="read_file",
    arguments='{"path":"hello.txt"}',
)
```

- `id`：本次工具调用的唯一标识。
- `name`：需要执行的工具名。
- `arguments`：JSON 字符串形式的工具参数。

### ChatResponse

统一表示模型的一次响应：

```python
ChatResponse(
    content="",
    tool_calls=(ToolCall(...),),
)
```

模型可能：

1. 返回普通文本，此时 `tool_calls` 为空。
2. 要求调用工具，此时 `content` 可以为空。

### LlmClient

`LlmClient` 是 Agent 依赖的协议：

```python
class LlmClient(Protocol):
    def chat(self, messages, tools) -> ChatResponse:
        ...
```

`FakeClient` 和 `OpenAICompatibleClient` 都能交给 Agent，是因为它们都提供相同
的 `chat()` 输入输出约定。

```text
FakeClient                 \
                            -> LlmClient -> Agent
OpenAICompatibleClient     /
```

Agent 不关心客户端是否访问真实模型，只关心它能否返回 `ChatResponse`。

## 4. Agent.run() 主流程

### 保存用户消息

```python
self.history.append({
    "role": "user",
    "content": user_input,
})
```

### 调用模型

```python
response = self.client.chat(
    self.history,
    self.tools.definitions(),
)
```

发送给模型的是：

- 完整消息历史 `history`。
- 当前可用工具的 JSON Schema。

### 保存模型响应

`response` 是方便 Python 处理的 `ChatResponse` 对象。

`assistant_message` 是符合模型协议、能够放入历史的字典：

```python
assistant_message = {
    "role": "assistant",
    "content": response.content,
}
```

如果模型要求调用工具，还要保存 `tool_calls`：

```python
assistant_message["tool_calls"] = [
    call.as_message_dict()
    for call in response.tool_calls
]
```

### 判断是否结束

```python
if not response.tool_calls:
    return response.content
```

没有工具调用，表示模型已经给出最终回答。

### 执行工具

```python
result = self.tools.execute(
    call.name,
    call.arguments,
)
```

Agent 根据工具名在 `ToolRegistry` 中找到本地处理函数。

### 回灌工具结果

```python
self.history.append({
    "role": "tool",
    "tool_call_id": call.id,
    "name": call.name,
    "content": result,
})
```

`tool_call_id` 必须与模型发起调用时的 `id` 相同，否则模型无法判断结果属于
哪次调用。

## 5. 消息历史

一次完整的工具调用会产生以下消息链：

```text
system
  -> user
  -> assistant(tool_call)
  -> tool(result)
  -> assistant(answer)
```

示例：

```text
system：你是一个编程 Agent
user：hello.txt 写了什么？
assistant：请调用 read_file
tool：hello from tool
assistant：文件内容是 hello from tool
```

第二次请求模型时，消息历史暂时只有前四条。模型返回最终答案后，第五条才会
加入完整历史。

## 6. ToolRegistry

`ToolRegistry` 同时保存两种信息：

```text
给模型看的：工具名、描述、参数 JSON Schema
给程序用的：真正执行操作的 handler
```

### ToolSpec

```python
ToolSpec(
    name="read_file",
    description="Read a UTF-8 text file.",
    parameters={...},
    handler=self._read_file,
)
```

模型只能看到前三项，不能直接接触 `handler`。

### execute()

```text
工具名
  -> 查找 ToolSpec
  -> 解析 JSON 参数
  -> 调用 handler
  -> 返回字符串结果
```

工具错误不会直接终止 Agent，而是转换成字符串回灌模型：

```text
Tool error: ...
```

这样模型可以根据错误修改参数后再次调用。

## 7. 路径安全

文件工具只能访问 `project_root` 内部。

```python
candidate = (self.project_root / raw_path).resolve()

if not candidate.is_relative_to(self.project_root):
    raise ValueError("path escapes the project root")
```

例如：

```text
project_root = /tmp/project
raw_path     = ../outside.txt

resolve 后：
/tmp/outside.txt
```

`/tmp/outside.txt` 不属于 `/tmp/project`，因此被拒绝。

这个检查防止的是路径穿越，而不是检查文件是否存在。

## 8. on_event

`on_event` 是展示运行过程的回调：

```python
self.on_event("tool", "read_file ...")
self.on_event("result", "hello from tool")
self.on_event("answer", "最终回答")
```

它可以让 CLI 打印工具过程，但不会：

- 发送消息给模型。
- 修改 history。
- 改变 Agent 的执行判断。

测试中没有传入 `on_event` 时，默认使用什么也不做的函数。

## 9. FakeClient 测试

单元测试不调用真实模型，而是提前准备两个响应：

```python
client = FakeClient([
    ChatResponse(
        content="",
        tool_calls=(ToolCall(...),),
    ),
    ChatResponse(
        content="The file says hello from tool.",
    ),
])
```

执行过程：

```text
第一次 chat()
  -> pop 第一条响应
  -> 要求调用 read_file

Agent 执行 read_file 并回灌结果

第二次 chat()
  -> pop 第二条响应
  -> 返回最终答案
```

测试中的第二条响应是预设的；真实环境中的第二条响应由模型根据工具结果现场
生成。

FakeClient 的价值：

- 不需要 API Key。
- 不访问网络。
- 测试结果固定。
- 可以检查 Agent 每次发送了哪些消息。

## 10. unittest 基础

一个测试通常分为三步：

```text
Arrange：准备环境和输入
Act：执行目标代码
Assert：验证实际结果
```

示例：

```python
class FileTest(unittest.TestCase):
    def test_read_file(self) -> None:
        # Arrange
        registry = ToolRegistry(...)

        # Act
        result = registry.execute(...)

        # Assert
        self.assertEqual("expected", result)
```

当前项目使用 `unittest`，自动发现规则是：

- 测试类继承 `unittest.TestCase`。
- 测试方法以 `test_` 开头。

常用断言：

```python
self.assertEqual(expected, actual)
self.assertIn(part, whole)
self.assertTrue(condition)
self.assertFalse(condition)

with self.assertRaises(ValueError):
    operation()
```

## 11. 测试命令

运行 Phase01 全部测试：

```bash
python3 -m unittest discover -s tests -v
```

只运行 Agent 测试：

```bash
python3 -m unittest discover -s tests -p "test_agent.py" -v
```

只运行一个测试方法：

```bash
python3 -m unittest \
  tests.test_agent.AgentTest.test_tool_result_is_fed_back_before_final_answer \
  -v
```

## 12. Phase01 自检

学完后应该能够回答：

1. 为什么模型不能直接执行本地工具？
2. `ToolCall`、`ChatResponse` 和 `assistant_message` 有什么区别？
3. 为什么 assistant 的 `tool_calls` 必须保存进 history？
4. 为什么 tool 消息必须带相同的 `tool_call_id`？
5. 为什么工具错误应该转换成字符串回灌模型？
6. FakeClient 为什么要提前准备两个响应？
7. `on_event` 会不会改变 Agent 的执行逻辑？
8. `LlmClient Protocol` 为什么能让 FakeClient 替代真实客户端？

能够用自己的话回答这些问题，就可以进入 Phase02。
