"""Persistent hybrid code retrieval with source-evidence spans.

Ranking fuses deterministic symbol matches, SQLite FTS5/BM25, lexical cosine,
and an optional embedding channel through Reciprocal Rank Fusion.  The default
requires no external service; an embedding client can be supplied explicitly.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from .tool_contracts import (
    ConcurrencyPolicy,
    ToolResult,
    ToolRisk,
    ToolSideEffect,
)
from .tools import ToolGateway, ToolRegistry, ToolSpec


@dataclass(frozen=True)
class CodeChunk:
    # First five fields preserve the original learning-phase constructor.
    path: str
    symbol: str
    start_line: int
    end_line: int
    content: str
    id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        digest = self.content_hash or hashlib.sha256(
            self.content.encode("utf-8", errors="replace")
        ).hexdigest()
        identifier = self.id or hashlib.sha256(
            f"{self.path}:{self.start_line}:{self.end_line}:{digest}".encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "content_hash", digest)
        object.__setattr__(self, "id", identifier)

    @property
    def file_path(self) -> str:
        return self.path

    @property
    def name(self) -> str:
        return self.symbol

    @property
    def file(self) -> str:
        return self.path

    @property
    def text(self) -> str:
        return self.content

    @property
    def terms(self) -> Counter[str]:
        return Counter(_tokenize(f"{self.path} {self.symbol} {self.content}"))


@dataclass(frozen=True)
class SearchResult:
    chunk: CodeChunk
    score: float
    channels: tuple[str, ...] = ()

    # Compatibility convenience: callers may treat a result like its chunk.
    @property
    def path(self) -> str:
        return self.chunk.path

    @property
    def file_path(self) -> str:
        return self.chunk.path

    @property
    def symbol(self) -> str:
        return self.chunk.symbol

    @property
    def start_line(self) -> int:
        return self.chunk.start_line

    @property
    def end_line(self) -> int:
        return self.chunk.end_line

    @property
    def content(self) -> str:
        return self.chunk.content

    @property
    def similarity(self) -> float:
        return self.score

    def __iter__(self):
        yield self.chunk
        yield self.score


class EmbeddingClient(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class OpenAICompatibleEmbeddingClient:
    """Optional `/embeddings` client; not enabled or billed implicitly."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        *,
        timeout_seconds: float = 120,
    ) -> None:
        if not api_key or not model or not base_url:
            raise ValueError("embedding api_key, model, and base_url are required")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(
                {"model": self.model, "input": list(texts)},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                root = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"embedding request failed: {exc}") from exc
        data = sorted(root.get("data", []), key=lambda item: int(item.get("index", 0)))
        vectors = [
            [float(value) for value in item.get("embedding", [])]
            for item in data
        ]
        if len(vectors) != len(texts) or any(not vector for vector in vectors):
            raise RuntimeError("embedding response length or vector shape is invalid")
        return vectors


class CodeChunker:
    """AST-aware Python chunking plus bounded line chunks for other files."""

    SUPPORTED_SUFFIXES = {
        ".py",
        ".java",
        ".kt",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".scala",
        ".sql",
        ".md",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
    }

    def __init__(self, *, max_lines: int = 120, overlap_lines: int = 20) -> None:
        if max_lines < 10:
            raise ValueError("max_lines must be at least 10")
        if overlap_lines < 0 or overlap_lines >= max_lines:
            raise ValueError("overlap_lines must be between 0 and max_lines-1")
        self.max_lines = max_lines
        self.overlap_lines = overlap_lines

    def chunk_file(
        self,
        path: str | Path,
        content: str | None = None,
        *,
        relative_path: str | None = None,
    ) -> list[CodeChunk]:
        source_path = Path(path)
        text = (
            source_path.read_text(encoding="utf-8", errors="replace")
            if content is None
            else content
        )
        display = relative_path or source_path.as_posix()
        if source_path.suffix.lower() == ".py":
            chunks = self._python_chunks(display, text)
            if chunks:
                return chunks
        return self._line_chunks(display, text)

    chunk = chunk_file

    def _python_chunks(self, path: str, content: str) -> list[CodeChunk]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        lines = content.splitlines(keepends=True)
        chunks: list[CodeChunk] = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            start = max(1, int(getattr(node, "lineno", 1)))
            end = max(start, int(getattr(node, "end_lineno", start)))
            symbol = node.name
            chunks.append(
                CodeChunk(
                    path,
                    symbol,
                    start,
                    end,
                    "".join(lines[start - 1 : end]),
                )
            )
        # Keep module-level imports/constants as evidence when definitions exist.
        first_definition = min((chunk.start_line for chunk in chunks), default=0)
        if first_definition > 1:
            prefix = "".join(lines[: first_definition - 1])
            if prefix.strip():
                chunks.insert(
                    0,
                    CodeChunk(path, "<module>", 1, first_definition - 1, prefix),
                )
        return chunks

    def _line_chunks(self, path: str, content: str) -> list[CodeChunk]:
        lines = content.splitlines(keepends=True)
        if not lines:
            return [CodeChunk(path, "<file>", 1, 1, "")]
        step = self.max_lines - self.overlap_lines
        chunks: list[CodeChunk] = []
        for start_index in range(0, len(lines), step):
            end_index = min(len(lines), start_index + self.max_lines)
            chunks.append(
                CodeChunk(
                    path,
                    f"<lines {start_index + 1}-{end_index}>",
                    start_index + 1,
                    end_index,
                    "".join(lines[start_index:end_index]),
                )
            )
            if end_index == len(lines):
                break
        return chunks


class VectorStore:
    """Backward-compatible in-memory lexical store used by older examples."""

    def __init__(self) -> None:
        self.chunks: list[CodeChunk] = []

    def add(self, chunk: CodeChunk) -> None:
        self.chunks.append(chunk)

    def extend(self, chunks: Iterable[CodeChunk]) -> None:
        self.chunks.extend(chunks)

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        query_vector = Counter(_tokenize(query))
        scored = [
            SearchResult(chunk, _cosine(query_vector, chunk.terms), ("lexical",))
            for chunk in self.chunks
        ]
        return sorted(scored, key=lambda item: item.score, reverse=True)[:top_k]


class CodeIndex:
    """Incremental SQLite code index with optional dense embeddings."""

    IGNORED_DIRECTORIES = {
        ".git",
        ".paicli",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        "dist",
        "build",
        "target",
    }

    def __init__(
        self,
        project_root: str | Path = ".",
        database_path: str | Path | None = None,
        *,
        chunker: CodeChunker | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.database_path = (
            str(Path(database_path).expanduser())
            if database_path not in {None, ":memory:"}
            else ":memory:"
        )
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.chunker = chunker or CodeChunker()
        self.embedding_client = embedding_client
        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._fts_available = True
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    indexed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    terms_json TEXT NOT NULL,
                    embedding_json TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
                CREATE INDEX IF NOT EXISTS idx_chunks_symbol ON chunks(symbol);
                """
            )
            try:
                self._connection.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                       chunk_id UNINDEXED, path, symbol, content,
                       tokenize='unicode61 remove_diacritics 2'
                    )"""
                )
            except sqlite3.OperationalError:
                self._fts_available = False

    def build(self) -> dict[str, int]:
        files = list(self._iter_files())
        current_paths = {path.relative_to(self.project_root).as_posix() for path in files}
        with self._lock:
            known = {
                str(row["path"]): str(row["content_hash"])
                for row in self._connection.execute(
                    "SELECT path, content_hash FROM files"
                ).fetchall()
            }
        removed = sorted(set(known) - current_paths)
        changed: list[tuple[Path, str, str]] = []
        for path in files:
            relative = path.relative_to(self.project_root).as_posix()
            content = path.read_text(encoding="utf-8", errors="replace")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if known.get(relative) != digest:
                changed.append((path, content, digest))

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for relative in removed:
                    self._delete_path(relative)
                    self._connection.execute("DELETE FROM files WHERE path=?", (relative,))
                for path, content, digest in changed:
                    relative = path.relative_to(self.project_root).as_posix()
                    self._delete_path(relative)
                    chunks = self.chunker.chunk_file(
                        path,
                        content,
                        relative_path=relative,
                    )
                    embeddings = self._embeddings(chunks)
                    for index, chunk in enumerate(chunks):
                        embedding = embeddings[index] if embeddings else None
                        self._insert_chunk(chunk, embedding)
                    self._connection.execute(
                        """INSERT INTO files(path, content_hash, indexed_at)
                           VALUES (?, ?, strftime('%s','now'))
                           ON CONFLICT(path) DO UPDATE SET
                           content_hash=excluded.content_hash,
                           indexed_at=excluded.indexed_at""",
                        (relative, digest),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return {
            "files": len(files),
            "changed": len(changed),
            "removed": len(removed),
            "chunks": self.chunk_count(),
        }

    index_project = build
    index = build

    def rebuild(self) -> int:
        """Backward-compatible full/incremental rebuild returning chunk count."""

        return int(self.build()["chunks"])

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        terms = _tokenize(query)
        if not terms:
            return []
        with self._lock:
            rows = self._connection.execute("SELECT * FROM chunks").fetchall()
        chunks = {_chunk(row).id: _chunk(row) for row in rows}
        channels: dict[str, list[tuple[str, float]]] = {}

        normalized_query = _normalize_identifier(query)
        strict_identifier = bool(
            "_" in query
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", query.strip())
        )
        if strict_identifier:
            chunks = {
                chunk_id: chunk
                for chunk_id, chunk in chunks.items()
                if normalized_query
                in _normalize_identifier(
                    f"{chunk.path} {chunk.symbol} {chunk.content}"
                )
            }
            if not chunks:
                return []
        exact = []
        for chunk in chunks.values():
            symbol = _normalize_identifier(chunk.symbol)
            if symbol == normalized_query:
                exact.append((chunk.id, 10.0))
            elif normalized_query and normalized_query in symbol:
                exact.append((chunk.id, 5.0))
        exact.sort(key=lambda item: item[1], reverse=True)
        if exact:
            channels["symbol"] = exact

        fts = self._fts_search(terms, limit=max(top_k * 8, 20))
        if fts:
            channels["fts"] = fts

        query_vector = Counter(terms)
        lexical = [
            (
                chunk.id,
                _cosine(query_vector, Counter(_tokenize(
                    f"{chunk.path} {chunk.symbol} {chunk.content}"
                ))),
            )
            for chunk in chunks.values()
        ]
        lexical = [item for item in lexical if item[1] > 0]
        lexical.sort(key=lambda item: item[1], reverse=True)
        if lexical:
            channels["lexical"] = lexical[: max(top_k * 8, 20)]

        if self.embedding_client is not None and chunks:
            query_embedding = self.embedding_client.embed([query])[0]
            dense: list[tuple[str, float]] = []
            for row in rows:
                raw = str(row["embedding_json"] or "")
                if not raw:
                    continue
                dense.append(
                    (
                        str(row["id"]),
                        _dense_cosine(query_embedding, json.loads(raw)),
                    )
                )
            dense.sort(key=lambda item: item[1], reverse=True)
            if dense:
                channels["dense"] = dense[: max(top_k * 8, 20)]

        fused: dict[str, float] = {}
        membership: dict[str, list[str]] = {}
        weights = {"symbol": 2.5, "fts": 1.5, "dense": 1.5, "lexical": 1.0}
        for channel, ranked in channels.items():
            for rank, (chunk_id, raw_score) in enumerate(ranked, start=1):
                if chunk_id not in chunks:
                    continue
                fused[chunk_id] = fused.get(chunk_id, 0.0) + (
                    weights[channel] / (60 + rank)
                )
                # A tiny deterministic tie-breaker preserves channel confidence.
                fused[chunk_id] += max(0.0, raw_score) * 1e-6
                membership.setdefault(chunk_id, []).append(channel)
        ordered = sorted(
            fused,
            key=lambda chunk_id: (
                fused[chunk_id],
                -chunks[chunk_id].start_line,
                chunks[chunk_id].path,
            ),
            reverse=True,
        )
        return [
            SearchResult(
                chunks[chunk_id],
                fused[chunk_id],
                tuple(membership.get(chunk_id, ())),
            )
            for chunk_id in ordered[:top_k]
        ]

    def register_tool(self, registry: ToolRegistry) -> None:
        def search_code(arguments: dict[str, Any]) -> str:
            results = self.search(
                str(arguments["query"]),
                int(arguments.get("top_k", 5)),
            )
            if not results:
                return "No indexed code matched the query."
            blocks = []
            for result in results:
                chunk = result.chunk
                blocks.append(
                    f"SOURCE {chunk.path}:L{chunk.start_line}-L{chunk.end_line}\n"
                    f"SYMBOL {chunk.symbol}\n"
                    f"CHANNELS {', '.join(result.channels)}\n"
                    f"{chunk.content}"
                )
            return "\n\n---\n\n".join(blocks)

        if "search_code" in registry.names():
            return
        registry.register(
            ToolSpec(
                "search_code",
                "Search the persistent project index and return source code evidence with line ranges.",
                registry.object_schema(
                    {
                        "query": {"type": "string", "minLength": 1},
                        "top_k": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    required=["query"],
                ),
                search_code,
                risk=ToolRisk.SAFE,
                side_effect=ToolSideEffect.READ_ONLY,
                concurrency=ConcurrencyPolicy.PARALLEL,
            )
        )

    register_as_tool = register_tool

    def chunk_count(self) -> int:
        with self._lock:
            return int(
                self._connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            )

    def stats(self) -> dict[str, object]:
        with self._lock:
            files = int(
                self._connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            )
        return {
            "files": files,
            "chunks": self.chunk_count(),
            "fts": self._fts_available,
            "dense": self.embedding_client is not None,
            "database": self.database_path,
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _iter_files(self) -> Iterable[Path]:
        for path in self.project_root.rglob("*"):
            if not path.is_file():
                continue
            relative_parts = path.relative_to(self.project_root).parts
            if any(part in self.IGNORED_DIRECTORIES for part in relative_parts):
                continue
            if path.suffix.lower() not in self.chunker.SUPPORTED_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
            except OSError:
                continue
            yield path

    def _embeddings(self, chunks: list[CodeChunk]) -> list[list[float]]:
        if self.embedding_client is None or not chunks:
            return []
        return self.embedding_client.embed(
            [f"{chunk.path}\n{chunk.symbol}\n{chunk.content}" for chunk in chunks]
        )

    def _insert_chunk(
        self,
        chunk: CodeChunk,
        embedding: list[float] | None,
    ) -> None:
        self._connection.execute(
            """INSERT INTO chunks
               (id, path, symbol, start_line, end_line, content, content_hash,
                terms_json, embedding_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                chunk.id,
                chunk.path,
                chunk.symbol,
                chunk.start_line,
                chunk.end_line,
                chunk.content,
                chunk.content_hash,
                json.dumps(chunk.terms, ensure_ascii=False),
                json.dumps(embedding) if embedding is not None else "",
            ),
        )
        if self._fts_available:
            self._connection.execute(
                """INSERT INTO chunks_fts(chunk_id, path, symbol, content)
                   VALUES (?, ?, ?, ?)""",
                (chunk.id, chunk.path, chunk.symbol, chunk.content),
            )

    def _delete_path(self, path: str) -> None:
        if self._fts_available:
            self._connection.execute("DELETE FROM chunks_fts WHERE path=?", (path,))
        self._connection.execute("DELETE FROM chunks WHERE path=?", (path,))

    def _fts_search(self, terms: list[str], *, limit: int) -> list[tuple[str, float]]:
        if not self._fts_available:
            return []
        expression = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:20])
        try:
            with self._lock:
                rows = self._connection.execute(
                    """SELECT chunk_id, bm25(chunks_fts) rank
                       FROM chunks_fts WHERE chunks_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (expression, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            (str(row["chunk_id"]), 1.0 / (1.0 + abs(float(row["rank"]))))
            for row in rows
        ]


class IndexRefreshingToolGateway:
    """Keep the canonical code index synchronized after repository mutations."""

    def __init__(self, gateway: ToolGateway, index: CodeIndex) -> None:
        self.gateway = gateway
        self.index = index
        self.last_refresh_error = ""

    def definitions(self) -> list[dict[str, Any]]:
        return self.gateway.definitions()

    def names(self) -> list[str]:
        return self.gateway.names()

    def spec(self, name: str) -> ToolSpec | None:
        return self.gateway.spec(name)

    def validate_arguments(self, name: str, arguments_json: str) -> dict[str, Any]:
        validator = getattr(self.gateway, "validate_arguments", None)
        if not callable(validator):
            raise AttributeError("wrapped tool gateway does not validate arguments")
        return validator(name, arguments_json)

    def execute(self, name: str, arguments_json: str) -> str:
        return self.execute_result(name, arguments_json).content

    def execute_result(
        self,
        name: str,
        arguments_json: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        if timeout_seconds is None:
            result = self.gateway.execute_result(name, arguments_json)
        else:
            result = self.gateway.execute_many_results(
                [(name, arguments_json)],
                timeout_seconds=timeout_seconds,
            )[0]
        return self._refresh_after((result,))[0]

    def execute_many(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> list[str]:
        return [
            result.content
            for result in self.execute_many_results(
                calls,
                timeout_seconds=timeout_seconds,
            )
        ]

    def execute_many_results(
        self,
        calls: list[tuple[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> list[ToolResult]:
        results = self.gateway.execute_many_results(
            calls,
            timeout_seconds=timeout_seconds,
        )
        return list(self._refresh_after(tuple(results)))

    def _refresh_after(
        self,
        results: tuple[ToolResult, ...],
    ) -> tuple[ToolResult, ...]:
        mutated = [
            index
            for index, result in enumerate(results)
            if result.ok and result.changed_files
        ]
        if not mutated:
            return results
        try:
            self.index.build()
        except Exception as exc:
            self.last_refresh_error = f"{type(exc).__name__}: {exc}"
            target = mutated[-1]
            values = list(results)
            values[target] = replace(
                values[target],
                content=(
                    values[target].content
                    + "\nCode index refresh warning: "
                    + self.last_refresh_error
                ),
            )
            return tuple(values)
        self.last_refresh_error = ""
        return results

    def __getattr__(self, name: str) -> Any:
        return getattr(self.gateway, name)


def _chunk(row: sqlite3.Row) -> CodeChunk:
    return CodeChunk(
        str(row["path"]),
        str(row["symbol"]),
        int(row["start_line"]),
        int(row["end_line"]),
        str(row["content"]),
        str(row["id"]),
        str(row["content_hash"]),
    )


def _tokenize(value: str) -> list[str]:
    # Split camelCase/snake_case/path identifiers while retaining CJK terms.
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value))
    raw = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", expanded.lower())
    result: list[str] = []
    for token in raw:
        result.append(token)
        if "_" in token:
            result.extend(part for part in token.split("_") if part)
    return result


def _normalize_identifier(value: str) -> str:
    return "".join(_tokenize(value))


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(value * right.get(key, 0) for key, value in left.items())
    denominator = math.sqrt(sum(value * value for value in left.values())) * math.sqrt(
        sum(value * value for value in right.values())
    )
    return numerator / denominator if denominator else 0.0


def _dense_cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(
        sum(b * b for b in right)
    )
    return numerator / denominator if denominator else 0.0


__all__ = [
    "CodeChunk",
    "CodeChunker",
    "CodeIndex",
    "EmbeddingClient",
    "IndexRefreshingToolGateway",
    "OpenAICompatibleEmbeddingClient",
    "SearchResult",
    "VectorStore",
]
