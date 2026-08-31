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

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .llm_client import LlmClient
from .tool_contracts import ConcurrencyPolicy, ToolRisk, ToolSideEffect
from .tools import ToolRegistry, ToolSpec

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


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate content plus tool protocol metadata for one chat message.

    Image data URLs are not counted as base64 text. Providers account for image
    inputs with model-specific vision tokens; this dependency-free estimate
    reserves a fixed amount per image instead of treating a 10 MB attachment as
    millions of language tokens.
    """

    content_tokens = _estimate_content_tokens(message.get("content", ""))
    protocol = ""
    if message.get("tool_calls"):
        protocol += json.dumps(
            message["tool_calls"], ensure_ascii=False, sort_keys=True, default=str
        )
    if message.get("tool_call_id"):
        protocol += str(message["tool_call_id"])
    if message.get("name"):
        protocol += str(message["name"])
    # Four tokens roughly account for role and chat-template separators.
    protocol_tokens = estimate_tokens(protocol) if protocol else 0
    return content_tokens + protocol_tokens + 4


@dataclass(frozen=True)
class MemoryEntry:
    """一条可持久化的长期记忆；创建后不允许修改。

    The first three fields preserve the original JSONL constructor.  The
    remaining optional fields let the SQLite managed-memory implementation
    expose provenance and lifecycle metadata through the same read interface.
    """

    content: str
    tags: tuple[str, ...] = ()
    created_at: float = 0.0
    id: str = ""
    source: str = ""
    source_hash: str = ""
    status: str = "active"
    updated_at: float = 0.0
    confidence: float = 1.0
    kind: str = "fact"
    supersedes_id: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            # frozen=True 禁止普通赋值，初始化阶段需用 object.__setattr__ 补上时间。
            object.__setattr__(self, "created_at", time.time())
        if not self.updated_at:
            object.__setattr__(self, "updated_at", self.created_at)


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
        # The default React runtime shares one LongTermMemory instance between
        # retrieval and save_memory calls. Protect append/read from its worker
        # threads; cross-process coordination remains a later runtime concern.
        self._lock = threading.RLock()

    def save(self, content: str, tags: tuple[str, ...] = ()) -> MemoryEntry:
        normalized = str(content).strip()
        if not normalized:
            raise ValueError("memory content cannot be empty")
        entry = MemoryEntry(
            content=normalized,
            tags=tuple(str(tag).strip() for tag in tags if str(tag).strip()),
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # JSONL 是“每行一个 JSON 对象”；a 模式只追加，不覆盖以前的记忆。
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return entry

    def entries(self) -> list[MemoryEntry]:
        with self._lock:
            # 记忆文件还不存在时，把它当作空记忆库，而不是错误。
            if not self.path.is_file():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        result: list[MemoryEntry] = []
        for line in lines:
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


class ConversationHistoryCompactor:
    """Compact the actual model message history at protocol-safe boundaries.

    The primary split keeps the most recent three user rounds intact. If one
    very long user turn contains many tool rounds, a secondary split keeps a
    recent suffix but never starts that suffix in the middle of contiguous tool
    result messages. Summaries are cached because ``MemoryManager.prepare`` is
    called before every model request while the old prefix is often unchanged.
    """

    SUMMARY_PROMPT = """Summarize the conversation below for a coding agent.
Preserve the original user goal, hard constraints, decisions, files inspected
or changed, important tool outcomes, unresolved errors, and remaining work.
Do not claim that an action happened unless it appears in the transcript.
Return a compact factual summary only.

=== conversation ===
{conversation}
=== end ===
"""

    def __init__(
        self,
        client: LlmClient | None = None,
        *,
        summarize: SummaryFunction | None = None,
        retain_recent_rounds: int = 3,
        minimum_tail_messages: int = 8,
        max_summary_input_chars: int = 60_000,
        cache_size: int = 32,
    ) -> None:
        if retain_recent_rounds < 1:
            raise ValueError("retain_recent_rounds must be positive")
        if minimum_tail_messages < 1:
            raise ValueError("minimum_tail_messages must be positive")
        if max_summary_input_chars < 1:
            raise ValueError("max_summary_input_chars must be positive")
        if cache_size < 1:
            raise ValueError("cache_size must be positive")
        self.client = client
        self.summarize = summarize
        self.retain_recent_rounds = retain_recent_rounds
        self.minimum_tail_messages = minimum_tail_messages
        self.max_summary_input_chars = max_summary_input_chars
        self.cache_size = cache_size
        self._cache: OrderedDict[str, tuple[str, bool]] = OrderedDict()
        self.summary_calls = 0
        self.last_used_fallback = False

    def set_client(self, client: LlmClient | None) -> None:
        self.client = client

    def compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        copied = [dict(message) for message in messages]
        split_index = self._split_index(copied)
        system_end = self._leading_system_count(copied)
        if split_index is None or split_index <= system_end:
            return copied

        old_messages = copied[system_end:split_index]
        if not old_messages:
            return copied
        summary = self._cached_summary(old_messages)
        if not summary.strip():
            return copied

        summary_message = {
            "role": "system",
            "content": "Compressed conversation summary:\n" + summary.strip(),
        }
        return [
            *copied[:system_end],
            summary_message,
            *copied[split_index:],
        ]

    def _split_index(self, messages: list[dict[str, Any]]) -> int | None:
        system_end = self._leading_system_count(messages)
        user_indices = [
            index
            for index in range(system_end, len(messages))
            if messages[index].get("role") == "user"
        ]
        if len(user_indices) > self.retain_recent_rounds:
            return user_indices[-self.retain_recent_rounds]

        # A single long coding turn can still contain dozens of assistant/tool
        # cycles. Keep a recent suffix, moving the boundary past contiguous tool
        # results so assistant tool_calls and their results stay together.
        candidate = len(messages) - self.minimum_tail_messages
        if candidate <= system_end:
            return None
        while candidate < len(messages) and messages[candidate].get("role") == "tool":
            candidate += 1
        if candidate >= len(messages):
            return None
        return candidate

    @staticmethod
    def _leading_system_count(messages: list[dict[str, Any]]) -> int:
        count = 0
        for message in messages:
            if message.get("role") != "system":
                break
            count += 1
        return count

    def _cached_summary(self, messages: list[dict[str, Any]]) -> str:
        # Hash the complete old prefix, not only the truncated summary prompt;
        # otherwise two long but different histories could share stale cache.
        cache_material = json.dumps(
            messages,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        cache_key = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            summary, used_fallback = cached
            self.last_used_fallback = used_fallback
            return summary

        self.summary_calls += 1
        rendered = self._render_messages(messages)
        summary = self._generate_summary(messages, rendered)
        self._cache[cache_key] = (summary, self.last_used_fallback)
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return summary

    def _generate_summary(
        self,
        messages: list[dict[str, Any]],
        rendered: str,
    ) -> str:
        try:
            if self.summarize is not None:
                summary = self.summarize([dict(message) for message in messages])
            elif self.client is not None:
                prompt = self.SUMMARY_PROMPT.format(
                    conversation=rendered[: self.max_summary_input_chars]
                )
                response = self.client.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You compress coding-agent history. Output only "
                                "a factual summary, never hidden reasoning."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    [],
                )
                summary = response.content
            else:
                raise RuntimeError("summary model is not configured")
            if summary and summary.strip():
                self.last_used_fallback = False
                return summary.strip()
        except Exception:
            # Compression is an availability optimization. A failed summary
            # request must not break the main Agent loop.
            pass

        self.last_used_fallback = True
        return self._fallback_summary(messages)

    def _render_messages(self, messages: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        total = 0
        for message in messages:
            role = str(message.get("role", "unknown")).upper()
            content = message.get("content", "")
            rendered_content = _render_content_for_summary(content)
            lines = [f"{role}: {rendered_content}"]
            for call in message.get("tool_calls") or []:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                lines.append(
                    "TOOL_CALL "
                    + str(function.get("name", "unknown"))
                    + ": "
                    + str(function.get("arguments", "{}"))
                )
            if message.get("tool_call_id"):
                lines.append("TOOL_CALL_ID: " + str(message["tool_call_id"]))
            block = "\n".join(lines).strip() + "\n\n"
            if total + len(block) > self.max_summary_input_chars:
                remaining = max(0, self.max_summary_input_chars - total)
                blocks.append(block[:remaining] + "\n...(summary input truncated)\n")
                break
            blocks.append(block)
            total += len(block)
        return "".join(blocks)

    @staticmethod
    def _fallback_summary(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            role = str(message.get("role", "unknown"))
            content = message.get("content", "")
            rendered = _render_content_for_summary(content)
            rendered = rendered.replace("\n", " ").strip()
            if rendered:
                lines.append(f"{role}: {rendered[:400]}")
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                lines.append(
                    "assistant tool_call: "
                    + str(function.get("name", "unknown"))
                    + " "
                    + str(function.get("arguments", "{}"))[:300]
                )
        return "\n".join(lines)


class MemoryManager:
    """为一次模型调用选择近期消息，并注入相关长期记忆。"""

    def __init__(
        self,
        *,
        max_tokens: int = 4_000,
        long_term: LongTermMemory | None = None,
        compressor: ContextCompressor | None = None,
        history_compactor: ConversationHistoryCompactor | None = None,
        long_term_context_tokens: int = 1_000,
    ) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if long_term_context_tokens < 1:
            raise ValueError("long_term_context_tokens must be positive")
        self.max_tokens = max_tokens
        self.long_term_context_tokens = long_term_context_tokens
        self.long_term = long_term
        self.compressor = compressor or ContextCompressor()
        self.history_compactor = history_compactor
        self.last_compacted = False
        self.last_estimated_tokens = 0
        self.last_prepared_tokens = 0

    def prepare(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 全程操作副本，保证压缩和插入记忆不会修改 Agent.history。
        prepared = [dict(message) for message in messages]
        total = sum(estimate_message_tokens(message) for message in prepared)
        self.last_estimated_tokens = total
        self.last_compacted = False
        if total > self.max_tokens:
            before = total
            if self.history_compactor is not None:
                prepared = self.history_compactor.compact(prepared)
            else:
                # 兼容旧学习阶段：确定性截断摘要仍可显式注入使用。
                prepared = self.compressor.compact(prepared)
            after = sum(estimate_message_tokens(message) for message in prepared)
            self.last_compacted = after < before

        if self.long_term:
            # 用最后一条 user 消息作为检索词，因为它最能代表用户当前问题。
            query = next(
                (
                    _query_text(message.get("content", ""))
                    for message in reversed(messages)
                    if message.get("role") == "user"
                ),
                "",
            )
            memories = self.long_term.search(query)
            memory_context = self._render_memory_context(memories)
            if memory_context:
                # 保留开头 System 层的稳定顺序，再注入相关长期记忆。
                insertion = _leading_system_count(prepared)
                prepared.insert(
                    insertion,
                    {
                        "role": "system",
                        "content": memory_context,
                    },
                )
        self.last_prepared_tokens = sum(
            estimate_message_tokens(message) for message in prepared
        )
        return prepared

    def _render_memory_context(self, memories: list[MemoryEntry]) -> str:
        if not memories:
            return ""
        header = "Relevant memory:\n"
        used = estimate_tokens(header)
        lines: list[str] = []
        for entry in memories:
            remaining = self.long_term_context_tokens - used
            if remaining <= 4:
                break
            # Reserve a few estimated tokens for the bullet marker and line
            # separator before truncating the memory body.
            content = _truncate_to_token_budget(entry.content, remaining - 4)
            if not content:
                continue
            line = f"- {content}"
            line_tokens = estimate_tokens(line)
            if used + line_tokens > self.long_term_context_tokens:
                break
            lines.append(line)
            used += line_tokens
        return header + "\n".join(lines) if lines else ""

    def set_summary_client(self, client: LlmClient | None) -> None:
        if self.history_compactor is not None:
            self.history_compactor.set_client(client)

    def set_max_tokens(self, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self.max_tokens = max_tokens

    def set_long_term_context_tokens(self, token_budget: int) -> None:
        if token_budget < 1:
            raise ValueError("long-term memory token budget must be positive")
        self.long_term_context_tokens = token_budget

    def status(self) -> str:
        long_term_count = len(self.long_term.entries()) if self.long_term else 0
        return (
            f"trigger={self.max_tokens} estimated={self.last_estimated_tokens} "
            f"prepared={self.last_prepared_tokens} compacted={self.last_compacted} "
            f"memory_context_limit={self.long_term_context_tokens} "
            f"long_term={long_term_count}"
        )


def _estimate_content_tokens(content: object) -> int:
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                total += estimate_tokens(str(part))
                continue
            part_type = part.get("type")
            if part_type == "text":
                total += estimate_tokens(str(part.get("text", "")))
            elif part_type in {"image_url", "input_image"}:
                # Conservative fixed reserve; exact accounting is provider and
                # image-resolution specific and belongs in returned API usage.
                total += 256
            else:
                total += estimate_tokens(
                    json.dumps(part, ensure_ascii=False, default=str)
                )
        return max(1, total)
    return estimate_tokens(json.dumps(content, ensure_ascii=False, default=str))


def _render_content_for_summary(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        image_count = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text", "")).strip()
                if text:
                    parts.append(text)
            elif isinstance(part, dict) and part.get("type") in {
                "image_url",
                "input_image",
            }:
                image_count += 1
            else:
                parts.append(json.dumps(part, ensure_ascii=False, default=str))
        if image_count:
            parts.append(f"[{image_count} image attachment(s) omitted from summary]")
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False, default=str)


def _truncate_to_token_budget(content: str, token_budget: int) -> str:
    if token_budget < 1 or not content:
        return ""
    if estimate_tokens(content) <= token_budget:
        return content
    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(content[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return content[:low].rstrip()


def _query_text(content: object) -> str:
    return _render_content_for_summary(content)


def _leading_system_count(messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in messages:
        if message.get("role") != "system":
            break
        count += 1
    return count


def register_memory_tool(
    registry: ToolRegistry,
    long_term: Any,
) -> None:
    """把“明确保存长期记忆”注册成 Agent 可调用的工具。

    这里故意不会把每句对话都自动写进长期记忆。只有当模型明确调用
    save_memory 时才持久化，可以减少模型猜测、临时信息和敏感数据被误存。
    """

    def save_memory(arguments: dict[str, Any]) -> str:
        # content 是必填项；去掉空白后仍为空时不允许写入。
        content = str(arguments["content"]).strip()
        if not content:
            raise ValueError("memory content cannot be empty")
        # JSON 数组在内部转为不可变 tuple，与 MemoryEntry.tags 的类型一致。
        tags = tuple(str(tag) for tag in arguments.get("tags", []))
        save_record = getattr(long_term, "save_record", None)
        if callable(save_record):
            save_record(
                content,
                tags=tags,
                kind=str(arguments.get("kind", "fact")),
                source=str(arguments.get("source", "")),
                source_hash=str(arguments.get("source_hash", "")),
                confidence=float(arguments.get("confidence", 1.0)),
                supersedes_id=str(arguments.get("supersedes_id", "")),
            )
        else:
            long_term.save(content, tags)
        return "Memory saved."

    # ToolSpec 会同时被用于：告诉模型工具定义，以及把调用路由到 save_memory。
    registry.register(
        ToolSpec(
            "save_memory",
            "Persist an explicit user preference or durable project fact.",
            registry.object_schema(
                {
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_000,
                    },
                    "tags": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string", "maxLength": 100},
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "preference", "experience", "decision"],
                    },
                    "source": {"type": "string", "maxLength": 500},
                    "source_hash": {"type": "string", "maxLength": 200},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "supersedes_id": {"type": "string", "maxLength": 100},
                },
                required=["content"],
            ),
            save_memory,
            risk=ToolRisk.SAFE,
            # JSONL append is a deliberate state change and should not race
            # another save_memory call from the same model response.
            side_effect=ToolSideEffect.FILE_WRITE,
            concurrency=ConcurrencyPolicy.SERIAL,
        )
    )
