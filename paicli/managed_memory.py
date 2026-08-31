"""Versioned SQLite long-term memory with provenance and conflict controls.

Two public adapters share one schema:

``ManagedLongTermMemory`` keeps the Phase-14 unverified/verified workflow used
by :class:`paicli.memory.MemoryManager`, while ``ManagedMemoryStore`` exposes a
richer active/superseded record API used by the final harness.  Keeping both
names avoids forcing old callers to migrate in lockstep.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Iterable

from .memory import MemoryEntry


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    STALE = "stale"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class MemoryKind(str, Enum):
    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    EXPERIENCE = "experience"


class ManagedLongTermMemory:
    """Long-term memory interface compatible with ``MemoryManager``."""

    default_status = MemoryStatus.UNVERIFIED

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
        self._initialize()

    def _initialize(self) -> None:
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
                    confidence REAL NOT NULL DEFAULT 1.0,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    supersedes_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            self._ensure_column("confidence", "REAL NOT NULL DEFAULT 1.0")
            self._ensure_column("kind", "TEXT NOT NULL DEFAULT 'fact'")
            self._ensure_column("supersedes_id", "TEXT NOT NULL DEFAULT ''")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source)"
            )

    def _ensure_column(self, name: str, declaration: str) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(memories)")
        }
        if name not in columns:
            self._connection.execute(
                f"ALTER TABLE memories ADD COLUMN {name} {declaration}"
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
        status = MemoryStatus.VERIFIED if verified else self.default_status
        return self._upsert(
            content,
            tags,
            source=source,
            source_hash=source_hash,
            status=status,
            confidence=1.0,
            kind=MemoryKind.FACT,
        )

    def _upsert(
        self,
        content: str,
        tags: Iterable[str] = (),
        *,
        source: str = "",
        source_hash: str = "",
        status: MemoryStatus,
        confidence: float = 1.0,
        kind: MemoryKind = MemoryKind.FACT,
        supersedes_id: str = "",
    ) -> MemoryEntry:
        normalized = _normalize(content)
        if not normalized:
            raise ValueError("memory content cannot be empty")
        if not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("memory confidence must be between 0 and 1")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        clean_tags = tuple(
            dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip())
        )
        now = time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memories WHERE normalized_hash=?",
                (digest,),
            ).fetchone()
            if row is None:
                memory_id = "mem_" + uuid.uuid4().hex
                self._connection.execute(
                    """INSERT INTO memories(
                           id, content, normalized_hash, tags_json, status,
                           source, source_hash, confidence, kind, supersedes_id,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        memory_id,
                        content.strip(),
                        digest,
                        json.dumps(clean_tags, ensure_ascii=False),
                        status.value,
                        source,
                        source_hash,
                        float(confidence),
                        kind.value,
                        supersedes_id,
                        now,
                        now,
                    ),
                )
            else:
                memory_id = str(row["id"])
                previous_tags = tuple(json.loads(row["tags_json"] or "[]"))
                merged_tags = tuple(dict.fromkeys((*previous_tags, *clean_tags)))
                previous_status = _status(str(row["status"]))
                effective_status = status
                if previous_status is MemoryStatus.VERIFIED and status in {
                    MemoryStatus.UNVERIFIED,
                    MemoryStatus.ACTIVE,
                }:
                    effective_status = MemoryStatus.VERIFIED
                self._connection.execute(
                    """UPDATE memories SET
                           content=?, tags_json=?, status=?, source=?,
                           source_hash=?, confidence=?, kind=?, supersedes_id=?,
                           updated_at=?
                       WHERE id=?""",
                    (
                        content.strip(),
                        json.dumps(merged_tags, ensure_ascii=False),
                        effective_status.value,
                        source or str(row["source"]),
                        source_hash or str(row["source_hash"]),
                        max(float(row["confidence"] or 0.0), float(confidence)),
                        kind.value,
                        supersedes_id or str(row["supersedes_id"]),
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
        include_superseded: bool = False,
    ) -> list[MemoryEntry]:
        statuses = [
            MemoryStatus.ACTIVE,
            MemoryStatus.VERIFIED,
            MemoryStatus.UNVERIFIED,
        ]
        if include_stale:
            statuses.append(MemoryStatus.STALE)
        if include_superseded:
            statuses.append(MemoryStatus.SUPERSEDED)
        if include_deleted:
            statuses.append(MemoryStatus.DELETED)
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM memories WHERE status IN ({placeholders}) "
                "ORDER BY updated_at DESC",
                tuple(item.value for item in statuses),
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
                   WHERE status IN (?, ?, ?)
                   ORDER BY updated_at DESC""",
                (
                    MemoryStatus.ACTIVE.value,
                    MemoryStatus.VERIFIED.value,
                    MemoryStatus.UNVERIFIED.value,
                ),
            ).fetchall()

        def score(row: sqlite3.Row) -> tuple[float, float]:
            tags = " ".join(json.loads(row["tags_json"] or "[]"))
            haystack = _terms(str(row["content"]) + " " + tags)
            overlap = len(terms & haystack)
            verification_bonus = (
                0.5 if _status(str(row["status"])) is MemoryStatus.VERIFIED else 0.0
            )
            confidence_bonus = float(row["confidence"] or 0.0) * 0.1
            return overlap + verification_bonus + confidence_bonus, float(
                row["updated_at"]
            )

        matches = [row for row in rows if len(terms & _row_terms(row)) > 0]
        matches.sort(key=score, reverse=True)
        return [_entry(row) for row in matches[:limit]]

    def verify(self, memory_id: str) -> MemoryEntry:
        return self._set_status(memory_id, MemoryStatus.VERIFIED)

    def delete(self, memory_id: str) -> MemoryEntry:
        return self._set_status(memory_id, MemoryStatus.DELETED)

    def mark_stale(self, source: str, current_source_hash: str) -> int:
        if not source:
            raise ValueError("source cannot be empty")
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE memories SET status=?, updated_at=?
                   WHERE source=? AND source_hash<>?
                     AND status NOT IN (?, ?)""",
                (
                    MemoryStatus.STALE.value,
                    time.time(),
                    source,
                    current_source_hash,
                    MemoryStatus.DELETED.value,
                    MemoryStatus.SUPERSEDED.value,
                ),
            )
            return int(cursor.rowcount)

    def status(self, memory_id: str) -> MemoryStatus:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown memory: {memory_id}")
        return _status(str(row["status"]))

    def stats(self) -> dict[str, int]:
        counts = {status.value: 0 for status in MemoryStatus}
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM memories GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        return counts

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _set_status(self, memory_id: str, status: MemoryStatus) -> MemoryEntry:
        with self._lock:
            updated = self._connection.execute(
                "UPDATE memories SET status=?, updated_at=? WHERE id=?",
                (status.value, time.time(), memory_id),
            ).rowcount
        if not updated:
            raise KeyError(f"unknown memory: {memory_id}")
        return self.get(memory_id)


class ManagedMemoryStore(ManagedLongTermMemory):
    """Richer active-memory API with explicit supersession/conflict handling."""

    default_status = MemoryStatus.ACTIVE

    def save_record(
        self,
        content: str,
        tags: tuple[str, ...] = (),
        *,
        kind: MemoryKind = MemoryKind.FACT,
        confidence: float = 1.0,
        source: str = "",
        source_hash: str = "",
        supersedes_id: str = "",
    ) -> MemoryEntry:
        if source and source_hash:
            self.mark_stale(source, source_hash)
        record = self._upsert(
            content,
            tags,
            source=source,
            source_hash=source_hash,
            status=MemoryStatus.ACTIVE,
            confidence=confidence,
            kind=MemoryKind(kind),
            supersedes_id=supersedes_id,
        )
        if supersedes_id and supersedes_id != record.id:
            self._set_status(supersedes_id, MemoryStatus.SUPERSEDED)
        return self.get(record.id)

    def save(
        self,
        content: str,
        tags: tuple[str, ...] = (),
        **kwargs: object,
    ) -> MemoryEntry:
        return self.save_record(
            content,
            tags,
            kind=MemoryKind(str(kwargs.get("kind", MemoryKind.FACT.value))),
            confidence=float(kwargs.get("confidence", 1.0)),
            source=str(kwargs.get("source", "")),
            source_hash=str(kwargs.get("source_hash", "")),
            supersedes_id=str(kwargs.get("supersedes_id", "")),
        )

    def resolve_conflict(
        self,
        winner_id: str,
        superseded_ids: Iterable[str],
    ) -> MemoryEntry:
        winner = self._set_status(winner_id, MemoryStatus.ACTIVE)
        for memory_id in superseded_ids:
            if memory_id != winner_id:
                self._set_status(memory_id, MemoryStatus.SUPERSEDED)
        return winner

    def import_jsonl(self, path: str | Path) -> int:
        source = Path(path)
        imported = 0
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            self.save_record(
                str(payload["content"]),
                tuple(str(tag) for tag in payload.get("tags", ())),
                kind=MemoryKind(str(payload.get("kind", MemoryKind.FACT.value))),
                confidence=float(payload.get("confidence", 1.0)),
                source=str(payload.get("source", "")),
                source_hash=str(payload.get("source_hash", "")),
            )
            imported += 1
        return imported


def _entry(row: sqlite3.Row) -> MemoryEntry:
    values = {
        "content": str(row["content"]),
        "tags": tuple(json.loads(row["tags_json"] or "[]")),
        "created_at": float(row["created_at"]),
        "id": str(row["id"]),
        "source": str(row["source"]),
        "source_hash": str(row["source_hash"]),
        "status": _status(str(row["status"])),
        "updated_at": float(row["updated_at"]),
        "confidence": float(row["confidence"] or 0.0),
        "kind": _kind(str(row["kind"])),
        "supersedes_id": str(row["supersedes_id"] or ""),
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


def _row_terms(row: sqlite3.Row) -> set[str]:
    tags = " ".join(json.loads(row["tags_json"] or "[]"))
    return _terms(str(row["content"]) + " " + tags)


def _status(value: str) -> MemoryStatus:
    try:
        return MemoryStatus(value)
    except ValueError:
        return MemoryStatus.ACTIVE


def _kind(value: str) -> MemoryKind:
    try:
        return MemoryKind(value)
    except ValueError:
        return MemoryKind.FACT


__all__ = [
    "ManagedLongTermMemory",
    "ManagedMemoryStore",
    "MemoryKind",
    "MemoryStatus",
]
