# Phase01 测试笔记

## 1. 测试解决什么问题

测试不是自动知道什么结果正确，而是由开发者明确规定：

```text
给定什么条件
  -> 执行什么代码
  -> 预期得到什么结果
```

如果预期结果与实际结果不同，测试就会失败。失败可能表示：

1. 业务代码存在问题。
2. 测试中的预期写错了。
3. 测试环境或导入配置存在问题。

## 2. unittest 与 pytest

当前项目使用 Python 标准库 `unittest`。

| 框架 | 测试写法 | 断言 |
|---|---|---|
| `unittest` | `TestCase` 类中的 `test_` 方法 | `self.assertEqual()` |
| `pytest` | 可以直接写 `test_` 函数 | `assert` |

`unittest discover` 默认发现：

```python
class FileTest(unittest.TestCase):
    def test_read_file(self) -> None:
        ...
```

普通函数不会按照当前项目的 `unittest` 方式执行：

```python
def test_read_file():
    ...
```

普通函数中也不存在 `self`，因此不能调用：

```python
self.assertEqual(...)
```

## 3. Arrange、Act、Assert

一个测试通常分成三部分：

```python
class FileTest(unittest.TestCase):
    def test_read_file(self) -> None:
        # Arrange：准备环境和输入
        registry = ToolRegistry(...)

        # Act：执行目标代码
        result = registry.execute(...)

        # Assert：验证实际结果
        self.assertEqual("expected", result)
```

测试方法名应该直接描述预期行为：

```python
test_read_file_returns_file_content
test_unknown_tool_returns_error
test_path_cannot_escape_project_root
```

## 4. 为什么使用 TemporaryDirectory

文件测试不能依赖开发者电脑上已有的文件，因此使用临时目录：

```python
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
```

它的特点：

- 每次测试获得独立目录。
- 不会污染真实项目。
- 离开 `with` 后自动删除。
- 测试可以重复运行。

## 5. 第一个文件读取测试

```python
class FileTest(unittest.TestCase):
    def test_read_file_returns_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Arrange
            root = Path(directory)
            (root / "hello.txt").write_text(
                "你好，测试",
                encoding="utf-8",
            )
            registry = ToolRegistry(root)

            # Act
            result = registry.execute(
                "read_file",
                '{"path":"hello.txt"}',
            )

            # Assert
            self.assertEqual("你好，测试", result)
```

`assertEqual()` 通常按照“预期、实际”的顺序书写：

```python
self.assertEqual(expected, actual)
```

顺序写反不一定导致测试失败，但失败信息会更难阅读。

## 6. 测试错误返回

### 不存在的工具

```python
def test_unknown_tool_returns_error(self) -> None:
    registry = ToolRegistry(".")

    result = registry.execute("missing_tool", "{}")

    self.assertIn("unknown tool", result)
```

### 不存在的文件

```python
def test_missing_file_returns_error(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        registry = ToolRegistry(directory)

        result = registry.execute(
            "read_file",
            '{"path":"missing.txt"}',
        )

        self.assertIn("not a file", result)
```

测试必须与实际错误协议一致。实现返回：

```text
Tool error: not a file: missing.txt
```

所以断言 `"File not found"` 会失败。

## 7. 测试路径穿越

```python
def test_path_cannot_escape_project_root(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        registry = ToolRegistry(directory)

        result = registry.execute(
            "read_file",
            '{"path":"../outside.txt"}',
        )

        self.assertIn("path escapes", result)
```

假设临时目录为：

```text
/tmp/project
```

路径规范化过程：

```text
/tmp/project/../outside.txt
  -> /tmp/outside.txt
```

目标路径已经不属于 `/tmp/project`，因此 `_safe_path()` 抛出异常。

这个测试验证的是安全边界，不是文件是否存在。

## 8. 测试文件写入副作用

工具返回成功字符串并不一定表示文件真的写入了，所以需要同时验证：

1. 返回值。
2. 磁盘上的实际内容。

```python
def test_write_file_creates_file(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = ToolRegistry(root)

        result = registry.execute(
            "write_file",
            '{"path":"notes/hello.txt","content":"hello"}',
        )

        self.assertEqual("Wrote notes/hello.txt", result)
        self.assertEqual(
            "hello",
            (root / "notes" / "hello.txt").read_text(encoding="utf-8"),
        )
```

## 9. FakeClient 为什么准备两个响应

Agent 测试不能依赖真实模型，因此提前准备两次模型响应：

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
  -> 返回预设工具调用

Agent 执行 read_file
Agent 把工具结果写入 history

第二次 chat()
  -> 返回预设最终答案
```

测试中的第二条响应是预设的；真实环境中的第二条响应由模型根据工具结果生成。

FakeClient 的价值：

- 不需要 API Key。
- 不访问网络。
- 响应稳定且可以重复。
- 可以检查 Agent 每次发送的消息。

## 10. 验证工具结果已经回灌

`FakeClient` 会保存每次收到的请求：

```python
self.requests.append((list(messages), list(tools)))
```

可以分别检查两次模型请求：

```python
first_messages, _ = client.requests[0]
self.assertEqual(
    ["system", "user"],
    [message["role"] for message in first_messages],
)

second_messages, _ = client.requests[1]
self.assertEqual(
    ["system", "user", "assistant", "tool"],
    [message["role"] for message in second_messages],
)

self.assertEqual(
    "hello from tool",
    second_messages[-1]["content"],
)
```

最终完整历史是：

```text
system -> user -> assistant(tool_call) -> tool(result) -> assistant(answer)
```

## 11. IDE 为什么有时运行 1 个测试

IDE 的运行数量取决于点击层级：

- 点击测试方法旁的运行按钮：运行 1 个测试。
- 点击测试类旁的运行按钮：运行类中所有测试。
- 运行测试文件：运行文件中的所有测试。
- 运行 `tests` 目录：运行整个目录。

IDE 还会保存上一次 Run Configuration，所以快捷键重新运行时，目标可能只是
某个测试方法。

## 12. 常见问题

### IDE 自动导入无关模块

下面这些导入与测试无关：

```python
from cgitb import reset
from idlelib.window import registry
from unittest import result
```

其中 `idlelib.window` 还会加载 `tkinter`，可能产生：

```text
ModuleNotFoundError: No module named '_tkinter'
```

没有使用的导入应该删除。

### 测试写了但没有执行

如果 `unittest` 输出的测试数量不包含新测试，检查：

- 类是否继承 `unittest.TestCase`。
- 方法名是否以 `test_` 开头。
- 文件名是否以 `test` 开头。
- 是否错误地把 unittest 写成了 pytest 普通函数。

### assertIn 的字符串与实际结果不一致

先打印或查看真实返回值：

```python
print(result)
```

再判断是业务实现错误，还是测试预期错误。

## 13. 常用命令

运行整个测试目录：

```bash
python3 -m unittest discover -s tests -v
```

只运行一个测试文件：

```bash
python3 -m unittest discover -s tests -p "test_files.py" -v
```

只运行一个测试方法：

```bash
python3 -m unittest \
  tests.test_files.FileTest.test_write_file_creates_file \
  -v
```

## 14. 测试自检

学完后应该能够回答：

1. Arrange、Act、Assert 分别是什么？
2. 为什么文件测试使用临时目录？
3. `unittest` 为什么通常需要 `TestCase` 类？
4. 为什么既要验证返回值，也要验证文件副作用？
5. 路径穿越测试验证的是什么？
6. FakeClient 为什么需要两个预设响应？
7. 如何证明工具结果已经进入第二次模型请求？
