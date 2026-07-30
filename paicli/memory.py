"""Phase 3: short-term, long-term, and compressed memory."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .tools import ToolRegistry, ToolSpec

SummaryFunction = Callable[[list[dict[str, Any]]], str]


def estimate_tokens(value: str) -> int:
    """Cheap deterministic estimate that works reasonably for mixed text."""

    ascii_words = len(re.findall(r"[A-Za-z0-9_]+", value))
    non_ascii = sum(1 for character in value if ord(character) > 127)
    punctuation = len(re.findall(r"[^\w\s]", value))
    return max(1, ascii_words + non_ascii + punctuation // 2)


@dataclass(frozen=True)
class MemoryEntry:
    content: str
    tags: tuple[str, ...] = ()
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(self, "created_at", time.time())


class ConversationMemory:
    def __init__(self, max_tokens: int = 4_000) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self.max_tokens = max_tokens
        self.messages: list[dict[str, Any]] = []

    def add(self, message: dict[str, Any]) -> None:
        self.messages.append(dict(message))
        while self.token_count() > self.max_tokens and len(self.messages) > 1:
            self.messages.pop(0)

    def token_count(self) -> int:
        return sum(
            estimate_tokens(str(message.get("content", "")))
            for message in self.messages
        )


class LongTermMemory:
    """Append-only JSONL memory with small lexical retrieval."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, content: str, tags: tuple[str, ...] = ()) -> MemoryEntry:
        entry = MemoryEntry(content=content, tags=tags)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return entry

    def entries(self) -> list[MemoryEntry]:
        if not self.path.is_file():
            return []
        result: list[MemoryEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            result.append(
                MemoryEntry(
                    content=item["content"],
                    tags=tuple(item.get("tags", ())),
                    created_at=float(item["created_at"]),
                )
            )
        return result

    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
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
            haystack = set(
                re.findall(r"\w+", f"{entry.content} {' '.join(entry.tags)}".lower())
            )
            return len(terms.intersection(haystack)), entry.created_at

        matches = [entry for entry in self.entries() if score(entry)[0] > 0]
        return sorted(matches, key=score, reverse=True)[:limit]


class ContextCompressor:
    def __init__(self, summarize: SummaryFunction | None = None) -> None:
        self.summarize = summarize or self._default_summary

    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_last: int = 6,
    ) -> list[dict[str, Any]]:
        if len(messages) <= keep_last:
            return [dict(message) for message in messages]
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
        lines = []
        for message in messages:
            content = str(message.get("content", "")).replace("\n", " ").strip()
            if content:
                lines.append(f"{message.get('role', 'unknown')}: {content[:200]}")
        return "\n".join(lines)


class MemoryManager:
    """Selects recent messages and relevant durable memories for a model call."""

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
        prepared = [dict(message) for message in messages]
        total = sum(
            estimate_tokens(str(message.get("content", ""))) for message in prepared
        )
        if total > self.max_tokens:
            prepared = self.compressor.compact(prepared)

        if self.long_term:
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
                prepared.insert(
                    1,
                    {
                        "role": "system",
                        "content": "Relevant memory:\n"
                        + "\n".join(f"- {entry.content}" for entry in memories),
                    },
                )
        return prepared


def register_memory_tool(
    registry: ToolRegistry,
    long_term: LongTermMemory,
) -> None:
    """Expose explicit durable memory without saving model guesses implicitly."""

    def save_memory(arguments: dict[str, Any]) -> str:
        content = str(arguments["content"]).strip()
        if not content:
            raise ValueError("memory content cannot be empty")
        tags = tuple(str(tag) for tag in arguments.get("tags", []))
        long_term.save(content, tags)
        return "Memory saved."

    registry.register(
        ToolSpec(
            "save_memory",
            "Persist an explicit user preference or durable project fact.",
            registry.object_schema(
                {
                    "content": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                required=["content"],
            ),
            save_memory,
        )
    )
