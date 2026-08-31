"""Persistent lexical + embedding code retrieval with source evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from .rag import CodeChunk, CodeChunker
from .tools import (
    ConcurrencyPolicy,
    ToolRegistry,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingClient:
    """OpenAI-compatible `/embeddings` client, including DashScope."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        *,
        dimensions: int | None = None,
        timeout_seconds: float = 120,
    ) -> None:
        if not api_key or not model or not base_url:
            raise ValueError("embedding api_key, model, and base_url are required")
        if dimensions is not None and dimensions < 1:
            raise ValueError("embedding dimensions must be positive")
        if timeout_seconds <= 0:
            raise ValueError("embedding timeout must be positive")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.dimensions = dimensions
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_dashscope_env(
        cls,
        environ: dict[str, str] | None = None,
    ) -> OpenAIEmbeddingClient:
        values = os.environ if environ is None else environ
        key = values.get("DASHSCOPE_API_KEY", "").strip()
        if not key:
            raise ValueError("DASHSCOPE_API_KEY is missing")
        model = values.get(
            "DASHSCOPE_EMBEDDING_MODEL",
            "text-embedding-v4",
        ).strip()
        base = values.get(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).strip()
        dimensions_raw = values.get("DASHSCOPE_EMBEDDING_DIMENSIONS", "").strip()
        dimensions = int(dimensions_raw) if dimensions_raw else None
        timeout = float(values.get("DASHSCOPE_TIMEOUT_SECONDS", "120"))
        return cls(key, model, base, dimensions=dimensions, timeout_seconds=timeout)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise EmbeddingError(
                f"embedding API returned HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise EmbeddingError(f"embedding API request failed: {exc.reason}") from exc
        try:
            root = json.loads(raw)
            values = sorted(root["data"], key=lambda item: int(item["index"]))
            embeddings = [
                [float(number) for number in item["embedding"]]
                for item in values
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EmbeddingError(f"invalid embedding response: {raw[:500]}") from exc
        if len(embeddings) != len(texts):
            raise EmbeddingError(
                f"embedding count mismatch: expected {len(texts)}, got {len(embeddings)}"
            )
        return embeddings


class HashEmbeddingClient:
    """Deterministic local embedding for tests/offline hybrid retrieval."""

    model = "local-hash-v1"

    def __init__(self, dimensions: int = 128) -> None:
        if dimensions < 8:
            raise ValueError("hash embedding dimensions must be at least 8")
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in _terms(text):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(value * value for value in vector))
            vectors.append(
                [value / norm for value in vector]
                if norm
                else vector
            )
        return vectors


@dataclass(frozen=True)
class HybridSearchResult:
    chunk: CodeChunk
    score: float
    lexical_score: float = 0.0
    dense_score: float = 0.0
    exact_identifier: bool = False


class HybridCodeIndex:
    """Persistent chunks + SQLite FTS5 + optional dense retrieval + RRF."""

    SUPPORTED_SUFFIXES = {".py", ".java", ".js", ".ts", ".tsx", ".md"}
    IGNORED_DIRECTORIES = {
        ".git",
        ".paicli",
        ".venv",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "target",
    }

    def __init__(
        self,
        root: str | Path,
        database: str | Path | None = None,
        *,
        embedding_client: EmbeddingClient | None = None,
        batch_size: int = 32,
    ) -> None:
        self.root = Path(root).resolve()
        self.database = Path(database or self.root / ".paicli" / "code-index.db")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        if batch_size < 1:
            raise ValueError("embedding batch size must be positive")
        self.embedding_client = embedding_client
        self.batch_size = batch_size
        self.chunker = CodeChunker()
        self._connection = sqlite3.connect(
            self.database,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def rebuild(self) -> int:
        chunks: list[tuple[CodeChunk, str, str]] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root)
            if self.IGNORED_DIRECTORIES.intersection(relative.parts):
                continue
            if path.suffix.lower() not in self.SUPPORTED_SUFFIXES:
                continue
            file_bytes = path.read_bytes()
            file_hash = hashlib.sha256(file_bytes).hexdigest()
            for chunk in self.chunker.chunk(path, self.root):
                chunk_hash = hashlib.sha256(
                    (
                        chunk.path
                        + "\0"
                        + chunk.symbol
                        + "\0"
                        + chunk.content
                    ).encode("utf-8", errors="replace")
                ).hexdigest()
                chunks.append((chunk, file_hash, chunk_hash))

        existing_embeddings: dict[str, str] = {}
        with self._lock:
            for row in self._connection.execute(
                "SELECT chunk_hash, embedding_json FROM chunks "
                "WHERE embedding_json IS NOT NULL"
            ):
                existing_embeddings[str(row["chunk_hash"])] = str(
                    row["embedding_json"]
                )

        embeddings: dict[str, str] = dict(existing_embeddings)
        missing = [
            item for item in chunks
            if item[2] not in embeddings and self.embedding_client is not None
        ]
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            vectors = self.embedding_client.embed(
                [_embedding_text(chunk) for chunk, _file_hash, _chunk_hash in batch]
            )
            for (_chunk, _file_hash, chunk_hash), vector in zip(
                batch,
                vectors,
                strict=True,
            ):
                embeddings[chunk_hash] = json.dumps(vector, separators=(",", ":"))

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute("DELETE FROM chunks")
                self._connection.execute("DELETE FROM chunks_fts")
                now = time.time()
                for chunk, file_hash, chunk_hash in chunks:
                    cursor = self._connection.execute(
                        """
                        INSERT INTO chunks(
                            path, symbol, start_line, end_line, content,
                            file_hash, chunk_hash, embedding_json,
                            embedding_model, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk.path,
                            chunk.symbol,
                            chunk.start_line,
                            chunk.end_line,
                            chunk.content,
                            file_hash,
                            chunk_hash,
                            embeddings.get(chunk_hash),
                            getattr(self.embedding_client, "model", "")
                            if chunk_hash in embeddings
                            else "",
                            now,
                        ),
                    )
                    chunk_id = int(cursor.lastrowid)
                    self._connection.execute(
                        "INSERT INTO chunks_fts(chunk_id, path, symbol, content) "
                        "VALUES (?, ?, ?, ?)",
                        (chunk_id, chunk.path, chunk.symbol, chunk.content),
                    )
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return len(chunks)

    def search(self, query: str, limit: int = 5) -> list[HybridSearchResult]:
        normalized = query.strip()
        if not normalized or limit < 1:
            return []
        candidate_limit = max(limit * 6, 30)
        lexical = self._lexical_search(normalized, candidate_limit)
        dense = self._dense_search(normalized, candidate_limit)
        ranks: dict[int, float] = {}
        details: dict[int, dict[str, Any]] = {}
        for rank, (row, score) in enumerate(lexical, 1):
            chunk_id = int(row["id"])
            ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            details.setdefault(chunk_id, {})["row"] = row
            details[chunk_id]["lexical"] = score
        for rank, (row, score) in enumerate(dense, 1):
            chunk_id = int(row["id"])
            ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            details.setdefault(chunk_id, {})["row"] = row
            details[chunk_id]["dense"] = score
        query_terms = set(_terms(normalized))
        for chunk_id, item in details.items():
            row = item["row"]
            identifiers = set(_terms(str(row["symbol"])))
            exact = bool(query_terms and query_terms.intersection(identifiers))
            item["exact"] = exact
            if exact:
                ranks[chunk_id] += 0.05
        ordered = sorted(ranks, key=ranks.get, reverse=True)[:limit]
        return [
            HybridSearchResult(
                chunk=_row_chunk(details[chunk_id]["row"]),
                score=ranks[chunk_id],
                lexical_score=float(details[chunk_id].get("lexical", 0.0)),
                dense_score=float(details[chunk_id].get("dense", 0.0)),
                exact_identifier=bool(details[chunk_id].get("exact", False)),
            )
            for chunk_id in ordered
        ]

    def register_tool(self, registry: ToolRegistry) -> None:
        if "search_code" in registry.names():
            return

        def search_code(arguments: dict[str, object]) -> str:
            results = self.search(
                str(arguments["query"]),
                int(arguments.get("top_k", 5)),
            )
            if not results:
                return "No matching code evidence found."
            return "\n\n".join(
                f"SOURCE {item.chunk.path}:L{item.chunk.start_line}-L{item.chunk.end_line} "
                f"[{item.chunk.symbol}] hybrid={item.score:.4f} "
                f"lexical={item.lexical_score:.4f} dense={item.dense_score:.4f}\n"
                f"{item.chunk.content[:2400]}"
                for item in results
            )

        registry.register(
            ToolSpec(
                "search_code",
                "Hybrid lexical/embedding search over indexed project code. "
                "Returns source file, line range, symbol, scores and evidence.",
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

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS chunks,
                       COUNT(DISTINCT path) AS files,
                       SUM(CASE WHEN embedding_json IS NOT NULL THEN 1 ELSE 0 END)
                           AS embedded
                FROM chunks
                """
            ).fetchone()
        return {
            "chunks": int(row["chunks"] or 0),
            "files": int(row["files"] or 0),
            "embedded": int(row["embedded"] or 0),
            "embedding_model": str(
                getattr(self.embedding_client, "model", "")
            ),
        }

    def _lexical_search(
        self,
        query: str,
        limit: int,
    ) -> list[tuple[sqlite3.Row, float]]:
        expression = _fts_query(query)
        if not expression:
            return []
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT c.*, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.chunk_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (expression, limit),
            ).fetchall()
        return [
            (row, 1.0 / (1.0 + max(0.0, float(row["rank"]))))
            for row in rows
        ]

    def _dense_search(
        self,
        query: str,
        limit: int,
    ) -> list[tuple[sqlite3.Row, float]]:
        if self.embedding_client is None:
            return []
        vector = self.embedding_client.embed([query])[0]
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM chunks WHERE embedding_json IS NOT NULL"
            ).fetchall()
        scored = [
            (row, _cosine(vector, json.loads(row["embedding_json"])))
            for row in rows
        ]
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS chunks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    chunk_hash TEXT NOT NULL UNIQUE,
                    embedding_json TEXT,
                    embedding_model TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    path,
                    symbol,
                    content,
                    tokenize = 'unicode61'
                );
                """
            )


def _embedding_text(chunk: CodeChunk) -> str:
    return (
        f"path: {chunk.path}\nsymbol: {chunk.symbol}\n"
        f"lines: {chunk.start_line}-{chunk.end_line}\n{chunk.content}"
    )[:16_000]


def _row_chunk(row: sqlite3.Row) -> CodeChunk:
    return CodeChunk(
        str(row["path"]),
        str(row["symbol"]),
        int(row["start_line"]),
        int(row["end_line"]),
        str(row["content"]),
    )


def _terms(text: str) -> list[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).replace("_", " ")
    return re.findall(r"[A-Za-z][A-Za-z0-9]*|[\u4e00-\u9fff]+", expanded.lower())


def _fts_query(query: str) -> str:
    terms = _terms(query)
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _cosine(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    if len(left_values) != len(right_values) or not left_values:
        return 0.0
    numerator = sum(a * b for a, b in zip(left_values, right_values, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "HashEmbeddingClient",
    "HybridCodeIndex",
    "HybridSearchResult",
    "OpenAIEmbeddingClient",
]
