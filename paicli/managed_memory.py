"""Versioned SQLite long-term memory with provenance and staleness controls."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable

from .memory import MemoryEntry


class MemoryStatus:
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    STALE = "stale"
    DELETED = "deleted"


class ManagedLongTermMemory:
    """Long-term memory interface compatible with ``MemoryManager``.

    Writes are deduplicated by normalized content. A source identifier and hash
    allow a caller to invalidate facts when a file or commit changes. Model-
    generated facts default to unverified; retrieval includes them with a lower
    score until an explicit verification operation promotes them.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    normalized_hash TEXT NOT NULL UNIQUE,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    source_hash TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source)"
            )

    def save(
        self,
        content: str,
        tags: tuple[str, ...] = (),
        *,
        source: str = "",
        source_hash: str = "",
        verified: bool = False,
    ) -> MemoryEntry:
        normalized = _normalize(content)
        if not normalized:
            raise ValueError("memory content cannot be empty")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        clean_tags = tuple(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
        now = time.time()
        status = MemoryStatus.VERIFIED if verified else MemoryStatus.UNVERIFIED
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memories WHERE normalized_hash=?",
                (digest,),
            ).fetchone()
            if row is None:
                memory_id = "mem_" + uuid.uuid4().hex
                self._connection.execute(
                    """INSERT INTO memories
                       (id, content, normalized_hash, tags_json, status, source,
                        source_hash, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        memory_id,
                        content.strip(),
                        digest,
                        json.dumps(clean_tags, ensure_ascii=False),
                        status,
                        source,
                        source_hash,
                        now,
                        now,
                    ),
                )
            else:
                memory_id = str(row["id"])
                previous_tags = tuple(json.loads(row["tags_json"] or "[]"))
                merged_tags = tuple(dict.fromkeys((*previous_tags, *clean_tags)))
                promoted = (
                    MemoryStatus.VERIFIED
                    if verified or row["status"] == MemoryStatus.VERIFIED
                    else status
                )
                self._connection.execute(
                    """UPDATE memories SET content=?, tags_json=?, status=?,
                       source=?, source_hash=?, updated_at=? WHERE id=?""",
                    (
                        content.strip(),
                        json.dumps(merged_tags, ensure_ascii=False),
                        promoted,
                        source or str(row["source"]),
                        source_hash or str(row["source_hash"]),
                        now,
                        memory_id,
                    ),
                )
        return self.get(memory_id)

    def get(self, memory_id: str) -> MemoryEntry:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown memory: {memory_id}")
        return _entry(row)

    def entries(
        self,
        *,
        include_stale: bool = False,
        include_deleted: bool = False,
    ) -> list[MemoryEntry]:
        statuses = [MemoryStatus.VERIFIED, MemoryStatus.UNVERIFIED]
        if include_stale:
            statuses.append(MemoryStatus.STALE)
        if include_deleted:
            statuses.append(MemoryStatus.DELETED)
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM memories WHERE status IN ({placeholders}) "
                "ORDER BY updated_at DESC",
                statuses,
            ).fetchall()
        return [_entry(row) for row in rows]

    def search(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        if limit < 1:
            return []
        terms = _terms(query)
        if not terms:
            return []
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM memories
                   WHERE status IN (?, ?)
                   ORDER BY updated_at DESC""",
                (MemoryStatus.VERIFIED, MemoryStatus.UNVERIFIED),
            ).fetchall()

        def score(row: sqlite3.Row) -> tuple[float, float]:
            tags = " ".join(json.loads(row["tags_json"] or "[]"))
            haystack = _terms(str(row["content"]) + " " + tags)
            overlap = len(terms & haystack)
            verification_bonus = (
                0.5 if row["status"] == MemoryStatus.VERIFIED else 0.0
            )
            return overlap + verification_bonus, float(row["updated_at"])

        matches = [row for row in rows if score(row)[0] > 0]
        matches.sort(key=score, reverse=True)
        return [_entry(row) for row in matches[:limit]]

    def verify(self, memory_id: str) -> MemoryEntry:
        return self._set_status(memory_id, MemoryStatus.VERIFIED)

    def delete(self, memory_id: str) -> MemoryEntry:
        return self._set_status(memory_id, MemoryStatus.DELETED)

    def mark_stale(self, source: str, current_source_hash: str) -> int:
        """Invalidate source-backed memories whose source version changed."""

        if not source:
            raise ValueError("source cannot be empty")
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE memories SET status=?, updated_at=?
                   WHERE source=? AND source_hash<>? AND status<>?""",
                (
                    MemoryStatus.STALE,
                    time.time(),
                    source,
                    current_source_hash,
                    MemoryStatus.DELETED,
                ),
            )
            return int(cursor.rowcount)

    def status(self, memory_id: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown memory: {memory_id}")
        return str(row["status"])

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _set_status(self, memory_id: str, status: str) -> MemoryEntry:
        with self._lock:
            updated = self._connection.execute(
                "UPDATE memories SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), memory_id),
            ).rowcount
        if not updated:
            raise KeyError(f"unknown memory: {memory_id}")
        return self.get(memory_id)


def _entry(row: sqlite3.Row) -> MemoryEntry:
    # The original MemoryEntry's first three fields remain compatible. New
    # provenance attributes are attached only if that dataclass exposes them.
    values = {
        "content": str(row["content"]),
        "tags": tuple(json.loads(row["tags_json"] or "[]")),
        "created_at": float(row["created_at"]),
        "id": str(row["id"]),
        "source": str(row["source"]),
        "source_hash": str(row["source_hash"]),
        "status": str(row["status"]),
        "updated_at": float(row["updated_at"]),
    }
    fields = getattr(MemoryEntry, "__dataclass_fields__", {})
    return MemoryEntry(**{key: value for key, value in values.items() if key in fields})


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def _terms(value: str) -> set[str]:
    stop = {"a", "an", "and", "is", "of", "the", "to", "what", "which"}
    return {
        term
        for term in re.findall(r"[\w.-]+", value.lower())
        if term not in stop
    }


__all__ = [
    "ManagedLongTermMemory",
    "MemoryStatus",
]
