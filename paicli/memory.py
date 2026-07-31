"""Phase 3：短期记忆、长期记忆和上下文压缩。

本期的模型调用流程：

    完整对话历史 history
              |
              v
    MemoryManager.prepare()
       |                |
       | 超过 token 预算  | 用最后一条用户消息检索
       v                v
    压缩旧消息       相关长期记忆
       |                |
       +-------+--------+
               v
          本轮模型输入

重点：prepare() 只准备本轮发给模型的消息，不会破坏 Agent.history。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

# 摘要函数的协议：接收多条旧消息，返回一段摘要文本。
SummaryFunction = Callable[[list[dict[str, Any]]], str]


def estimate_tokens(value: str) -> int:
    """用简单规则估算 token，不追求与某个模型的 tokenizer 完全一致。"""

    # 连续的英文、数字或下划线按一个词计算，例如 hello_world 计 1。
    ascii_words = len(re.findall(r"[A-Za-z0-9_]+", value))
    # 中文等非 ASCII 字符按字符数估算。
    non_ascii = sum(1 for character in value if ord(character) > 127)
    # 标点也占 token，但这里用“每两个约算一个”的粗略规则。
    punctuation = len(re.findall(r"[^\w\s]", value))
    # 即使是空字符串也至少返回 1，避免预算计算完全不增长。
    return max(1, ascii_words + non_ascii + punctuation // 2)


@dataclass(frozen=True)
class MemoryEntry:
    """一条可持久化的长期记忆；创建后不允许修改。"""

    content: str
    tags: tuple[str, ...] = ()
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            # frozen=True 禁止普通赋值，初始化阶段需用 object.__setattr__ 补上时间。
            object.__setattr__(self, "created_at", time.time())


class ConversationMemory:
    """按 token 上限保留最近消息的短期记忆（FIFO）。"""

    def __init__(self, max_tokens: int = 4_000) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self.max_tokens = max_tokens
        self.messages: list[dict[str, Any]] = []

    def add(self, message: dict[str, Any]) -> None:
        # 保存副本，避免调用者之后修改原字典时污染记忆。
        self.messages.append(dict(message))
        # 超出预算就从最旧的消息开始删除，但至少保留最新一条。
        while self.token_count() > self.max_tokens and len(self.messages) > 1:
            self.messages.pop(0)

    def token_count(self) -> int:
        # role 等元数据暂不计入，本期只估算 content。
        return sum(
            estimate_tokens(str(message.get("content", "")))
            for message in self.messages
        )


class LongTermMemory:
    """JSONL 追加式长期记忆，使用简单关键词交集进行检索。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, content: str, tags: tuple[str, ...] = ()) -> MemoryEntry:
        entry = MemoryEntry(content=content, tags=tags)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # JSONL 是“每行一个 JSON 对象”；a 模式只追加，不覆盖以前的记忆。
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return entry

    def entries(self) -> list[MemoryEntry]:
        # 记忆文件还不存在时，把它当作空记忆库，而不是错误。
        if not self.path.is_file():
            return []
        result: list[MemoryEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            # JSON 中的 tags 是 list，还原 MemoryEntry 时再转回 tuple。
            result.append(
                MemoryEntry(
                    content=item["content"],
                    tags=tuple(item.get("tags", ())),
                    created_at=float(item["created_at"]),
                )
            )
        return result

    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        # 过滤常见英文虚词，防止“the/is”这类词制造大量无意义匹配。
        stop_words = {
            "a",
            "an",
            "and",
            "does",
            "is",
            "of",
            "the",
            "to",
            "use",
            "uses",
            "what",
            "which",
        }
        terms = {
            term
            for term in re.findall(r"\w+", query.lower())
            if term not in stop_words
        }

        def score(entry: MemoryEntry) -> tuple[int, float]:
            # 记忆内容和标签一起参与检索；测试中的 database 就是通过 tag 命中。
            haystack = set(
                re.findall(r"\w+", f"{entry.content} {' '.join(entry.tags)}".lower())
            )
            # 先按命中词数排序，同分时 created_at 越新越靠前。
            return len(terms.intersection(haystack)), entry.created_at

        # 完全没有关键词交集的记忆不返回。
        matches = [entry for entry in self.entries() if score(entry)[0] > 0]
        return sorted(matches, key=score, reverse=True)[:limit]


class ContextCompressor:
    """把较旧消息合并成摘要，同时原样保留最近消息。"""

    def __init__(self, summarize: SummaryFunction | None = None) -> None:
        self.summarize = summarize or self._default_summary

    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_last: int = 6,
    ) -> list[dict[str, Any]]:
        # 消息本来就不多时不需压缩，但仍返回副本。
        if len(messages) <= keep_last:
            return [dict(message) for message in messages]
        # old 进入摘要；最后 keep_last 条保留原文，避免丢失最近细节。
        old = messages[:-keep_last]
        summary = self.summarize(old)
        return [
            {
                "role": "system",
                "content": f"Summary of earlier conversation:\n{summary}",
            },
            *[dict(message) for message in messages[-keep_last:]],
        ]

    @staticmethod
    def _default_summary(messages: list[dict[str, Any]]) -> str:
        # 本期不调用 LLM 生成摘要，只是拼接 role 和截断后的 content。
        lines = []
        for message in messages:
            content = str(message.get("content", "")).replace("\n", " ").strip()
            if content:
                lines.append(f"{message.get('role', 'unknown')}: {content[:200]}")
        return "\n".join(lines)


class MemoryManager:
    """为一次模型调用选择近期消息，并注入相关长期记忆。"""

    def __init__(
        self,
        *,
        max_tokens: int = 4_000,
        long_term: LongTermMemory | None = None,
        compressor: ContextCompressor | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.long_term = long_term
        self.compressor = compressor or ContextCompressor()

    def prepare(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 全程操作副本，保证压缩和插入记忆不会修改 Agent.history。
        prepared = [dict(message) for message in messages]
        total = sum(
            estimate_tokens(str(message.get("content", ""))) for message in prepared
        )
        if total > self.max_tokens:
            # 超过本轮上下文预算后，用“旧消息摘要 + 最近消息原文”代替全量历史。
            prepared = self.compressor.compact(prepared)

        if self.long_term:
            # 用最后一条 user 消息作为检索词，因为它最能代表用户当前问题。
            query = next(
                (
                    str(message.get("content", ""))
                    for message in reversed(messages)
                    if message.get("role") == "user"
                ),
                "",
            )
            memories = self.long_term.search(query)
            if memories:
                # 放在位置 1，通常就是系统提示词之后、对话消息之前。
                prepared.insert(
                    1,
                    {
                        "role": "system",
                        "content": "Relevant memory:\n"
                        + "\n".join(f"- {entry.content}" for entry in memories),
                    },
                )
        return prepared
