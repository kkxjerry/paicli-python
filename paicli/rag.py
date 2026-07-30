"""Phase 4: lightweight code indexing and lexical vector retrieval."""

from __future__ import annotations

import ast
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .tools import ToolRegistry, ToolSpec


def tokenize(text: str) -> Counter[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).replace("_", " ")
    parts = re.findall(r"[A-Za-z][A-Za-z0-9]*|[\u4e00-\u9fff]", expanded.lower())
    return Counter(parts)


@dataclass(frozen=True)
class CodeChunk:
    path: str
    symbol: str
    start_line: int
    end_line: int
    content: str


@dataclass(frozen=True)
class SearchResult:
    chunk: CodeChunk
    score: float


class CodeChunker:
    def chunk(self, path: Path, root: Path) -> list[CodeChunk]:
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = str(path.relative_to(root))
        if path.suffix != ".py":
            return [CodeChunk(relative, path.name, 1, len(text.splitlines()), text)]

        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [CodeChunk(relative, path.name, 1, len(text.splitlines()), text)]

        lines = text.splitlines()
        chunks: list[CodeChunk] = []
        for node in tree.body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            end = getattr(node, "end_lineno", node.lineno)
            chunks.append(
                CodeChunk(
                    relative,
                    node.name,
                    node.lineno,
                    end,
                    "\n".join(lines[node.lineno - 1 : end]),
                )
            )
        return chunks or [
            CodeChunk(relative, path.name, 1, len(lines), text)
        ]


class VectorStore:
    """In-memory sparse vectors using cosine similarity."""

    def __init__(self) -> None:
        self._items: list[tuple[CodeChunk, Counter[str]]] = []

    def replace(self, chunks: Iterable[CodeChunk]) -> None:
        self._items = [(chunk, tokenize(chunk.content)) for chunk in chunks]

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        query_vector = tokenize(query)
        scored = [
            SearchResult(chunk, self._cosine(query_vector, vector))
            for chunk, vector in self._items
        ]
        return sorted(
            (result for result in scored if result.score > 0),
            key=lambda result: result.score,
            reverse=True,
        )[:limit]

    @staticmethod
    def _cosine(left: Counter[str], right: Counter[str]) -> float:
        numerator = sum(value * right.get(term, 0) for term, value in left.items())
        left_norm = math.sqrt(sum(value * value for value in left.values()))
        right_norm = math.sqrt(sum(value * value for value in right.values()))
        if not left_norm or not right_norm:
            return 0.0
        return numerator / (left_norm * right_norm)


class CodeIndex:
    SUPPORTED_SUFFIXES = {".py", ".java", ".js", ".ts", ".md"}
    IGNORED_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__"}

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.chunker = CodeChunker()
        self.store = VectorStore()
        self.chunk_count = 0

    def rebuild(self) -> int:
        chunks: list[CodeChunk] = []
        for path in self.root.rglob("*"):
            if (
                path.is_file()
                and path.suffix in self.SUPPORTED_SUFFIXES
                and not self.IGNORED_DIRECTORIES.intersection(path.parts)
            ):
                chunks.extend(self.chunker.chunk(path, self.root))
        self.store.replace(chunks)
        self.chunk_count = len(chunks)
        return self.chunk_count

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        return self.store.search(query, limit)

    def register_tool(self, registry: ToolRegistry) -> None:
        def search_code(arguments: dict[str, object]) -> str:
            query = str(arguments["query"])
            limit = int(arguments.get("top_k", 5))
            results = self.search(query, limit)
            if not results:
                return "No matching code found."
            return "\n\n".join(
                f"{item.chunk.path}:{item.chunk.start_line} "
                f"[{item.chunk.symbol}] score={item.score:.3f}\n"
                f"{item.chunk.content[:1200]}"
                for item in results
            )

        registry.register(
            ToolSpec(
                name="search_code",
                description="Search indexed project code by meaning or identifier.",
                parameters=registry.object_schema(
                    {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    required=["query"],
                ),
                handler=search_code,
            )
        )
