# Phase 03 学习笔记：Memory 与上下文压缩

## 先分清期数

- Phase 01：ReAct 与工具调用。
- Phase 02：Plan-and-Execute 与 DAG（有向无环图）。
- Phase 03：短期记忆、长期记忆与上下文压缩。

DAG 是 Phase 02，Phase 03 开始解决“对话越来越长，不能全部发给模型”的问题。

## 总体流程

```text
用户发来新问题
       |
       v
Agent 把问题加入完整 history
       |
       v
MemoryManager.prepare(history)
       |
       +-- 超过 token 预算？ -- 是 --> 旧消息压缩为摘要
       |
       +-- 存在长期记忆？ -- 是 --> 根据最新用户问题检索
       |                                  |
       |                                  v
       |                         插入 Relevant memory
       v
生成本轮 messages
       |
       v
LlmClient.chat(messages, tools)
```

`history` 和 `messages` 不是同一个概念：

- `history`：Agent 保存的完整对话历史。
- `messages`：经过压缩和记忆注入后，本轮真正发给模型的内容。

## 四个核心类

### 1. ConversationMemory

负责保留最近的消息。超过 token 上限时，从最旧的消息开始删除。

```text
[message 1, message 2, message 3]
     超过预算
           |
           v
[message 2, message 3]
```

这是 FIFO：先进来的旧消息先离开。

### 2. LongTermMemory

把记忆持久化到 JSONL 文件。每行是一条独立 JSON：

```json
{"content": "The project uses SQLite", "tags": ["database"], "created_at": 123.0}
```

检索时同时搜索 `content` 和 `tags`，按关键词命中数排序。它还不是向量检索，只是本期用于学习的词法匹配。

### 3. ContextCompressor

将旧消息替换为一条摘要，同时原样保留最近 `keep_last` 条消息：

```text
[old 1, old 2, old 3, recent 1, recent 2]
                  |
                  v
[summary(old 1..3), recent 1, recent 2]
```

默认摘要器只是文本拼接和截断，没有调用大模型。

### 4. MemoryManager

它是总调度器：

1. 复制原始消息，避免修改 `Agent.history`。
2. 估算 token，超预算就调用 `ContextCompressor`。
3. 找到最后一条用户消息作为检索词。
4. 从 `LongTermMemory` 取出相关记忆。
5. 将相关记忆作为 system 消息插入模型上下文。

## 测试阅读顺序

1. `test_long_term_memory_persists_and_retrieves`：先理解保存和检索。
2. `test_context_is_compacted_when_over_budget`：再理解 token 超限后的压缩。
3. `test_token_estimate_handles_chinese_and_ascii`：最后看粗略 token 估算。

## 看完后应该能回答

1. 为什么不能每次都把全部对话发给模型？
2. 为什么要保留最近消息的原文？
3. `history` 和本轮的 `messages` 有什么区别？
4. 长期记忆为什么需要持久化？
5. 关键词检索与向量检索的能力边界有什么不同？
