# Phase 04 学习笔记：RAG 与代码检索

## 这一期解决什么

Agent 不可能每次都把整个项目的代码发给模型。Phase 04 先建立代码索引，模型需要某段代码时再调用 `search_code`。

```text
项目代码
   |
   v
分割成 CodeChunk
   |
   v
转成词频向量并存入内存
   |
   v
Agent 调用 search_code
   |
   v
返回最相关的代码块
   |
   v
工具结果回灌给模型
```

Phase 03 的 Memory 主要保存“过去发生的事”；Phase 04 的 RAG 主要从“当前项目代码”中找资料。

## 第一步：代码分块

Python 文件使用 `ast.parse()` 获得语法树，然后将顶层类和顶层函数分别建成 `CodeChunk`。

```python
def calculate_invoice_total(items):
    ...

def send_email(address):
    ...
```

会变成两块：

```text
CodeChunk(symbol="calculate_invoice_total")
CodeChunk(symbol="send_email")
```

非 Python 文件、有语法错误的 Python 文件、不包含顶层类/函数的 Python 文件，都会整体作为一块。

## 第二步：分词与向量

`tokenize()` 会拆分驼峰命名和下划线命名：

```text
calculateInvoice_total
        |
        v
calculate, invoice, total
```

`Counter` 保存每个词的出现次数：

```python
Counter({"calculate": 1, "invoice": 1, "total": 1})
```

这就是本期的“向量”。它不是 Embedding，所以只能发现词面重合。

## 第三步：余弦相似度

`VectorStore._cosine()` 比较查询向量和代码块向量的方向。共同单词越多，分数通常越高。

```text
查询：calculate invoice total
块 A：calculate_invoice_total  -> 三个词命中
块 B：send_email               -> 零个词命中
```

因此块 A 排在第一位，块 B 因为分数为 0 被过滤。

## 第四步：注册为 Agent 工具

`CodeIndex.register_tool()` 将检索函数包装成 `ToolSpec`：

```text
工具名：search_code
参数：query、top_k
返回：路径、行号、符号、分数和代码内容
```

模型不会直接调用 `CodeIndex.search()`，它只会生成 `search_code` 工具请求，然后交给 `ToolRegistry.execute()` 执行。

## 两个测试

1. `test_indexes_symbols_and_retrieves_relevant_code`
   验证“Python 分块 + 建索引 + 相似度排序”。
2. `test_registers_search_code_as_agent_tool`
   验证“将检索注册为工具 + 通过 ToolRegistry 执行”。

## 当前没有实现的部分

- 没有 Embedding 模型，不理解同义词和真正语义。
- 索引只在内存中，程序退出后丢失。
- 每次 `rebuild()` 都全量扫描，没有增量更新。
- Python 只按顶层类/函数分块，其他语言整个文件作为一块。
- CLI 没有自动创建 `CodeIndex` 和注册 `search_code`，需要手动接入。
- 没有测试真实 Agent 是否会主动选择 `search_code`。

因此，Phase 04 仍然是一个可运行的教学级 RAG 骨架，不是完整的生产检索系统。
