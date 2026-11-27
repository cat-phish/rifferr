"""Cached AI answers (AI-CORE-16, -17).

The key covers everything that decides the answer. The stored value is the
schema-validated answer, before the task's own checks, so a change that only affects
those checks re-validates without a new request.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from rifferr.appdb import AppDB


def cache_key(
    *,
    task: str,
    prompt_version: int,
    provider_type: str,
    model: str,
    temperature: float,
    system: str,
    user: str,
    output_schema: dict[str, Any],
) -> str:
    """SHA-256 over every input that decides the answer (AI-CORE-16)."""
    parts = {
        "task": task,
        "prompt_version": prompt_version,
        "provider_type": provider_type,
        "model": model,
        "temperature": temperature,
        "system": system,
        "user": user,
        "schema": output_schema,
    }
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class CacheEntry:
    task: str
    provider_type: str
    model: str
    prompt_version: int
    answer: dict[str, Any]


class AICacheStore(Protocol):
    def get(self, profile: str, key: str, *, now: datetime, ttl: timedelta) -> CacheEntry | None:
        """A live entry, marked as used; ``None`` when missing or expired."""
        ...

    def has(self, profile: str, key: str, *, now: datetime, ttl: timedelta) -> bool:
        """Whether a live entry exists, without marking it used (preview)."""
        ...

    def put(self, profile: str, key: str, entry: CacheEntry, *, now: datetime) -> None: ...

    def prune(self, profile: str, *, now: datetime, ttl: timedelta, max_entries: int) -> int:
        """Drop expired entries, then the least recently used beyond ``max_entries``."""
        ...


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


class AppAICache:
    """``AICacheStore`` on the app DB's ``ai_cache`` table."""

    def __init__(self, db: AppDB) -> None:
        self.db = db

    @property
    def _conn(self) -> sqlite3.Connection:
        return self.db._conn  # pyright: ignore[reportPrivateUsage]

    def get(self, profile: str, key: str, *, now: datetime, ttl: timedelta) -> CacheEntry | None:
        row = self._conn.execute(
            "SELECT task, provider_type, model, prompt_version, answer, created_at FROM ai_cache"
            " WHERE profile = ? AND key = ?",
            (profile, key),
        ).fetchone()
        if row is None or row["created_at"] < _iso(now - ttl):
            return None
        self._conn.execute(
            "UPDATE ai_cache SET last_used_at = ? WHERE profile = ? AND key = ?",
            (_iso(now), profile, key),
        )
        return CacheEntry(
            task=row["task"],
            provider_type=row["provider_type"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            answer=json.loads(row["answer"]),
        )

    def has(self, profile: str, key: str, *, now: datetime, ttl: timedelta) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM ai_cache WHERE profile = ? AND key = ? AND created_at >= ?",
            (profile, key, _iso(now - ttl)),
        ).fetchone()
        return row is not None

    def put(self, profile: str, key: str, entry: CacheEntry, *, now: datetime) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO ai_cache (profile, key, task, provider_type, model,"
            " prompt_version, answer, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                profile,
                key,
                entry.task,
                entry.provider_type,
                entry.model,
                entry.prompt_version,
                json.dumps(entry.answer, ensure_ascii=False),
                _iso(now),
                _iso(now),
            ),
        )

    def prune(self, profile: str, *, now: datetime, ttl: timedelta, max_entries: int) -> int:
        expired = self._conn.execute(
            "DELETE FROM ai_cache WHERE profile = ? AND created_at < ?",
            (profile, _iso(now - ttl)),
        ).rowcount
        over = self._conn.execute(
            "DELETE FROM ai_cache WHERE profile = ? AND key IN (SELECT key FROM ai_cache"
            " WHERE profile = ? ORDER BY last_used_at DESC, key LIMIT -1 OFFSET ?)",
            (profile, profile, max_entries),
        ).rowcount
        return expired + over


@dataclass
class MemoryAICache:
    """In-memory ``AICacheStore`` for tests."""

    rows: dict[tuple[str, str], tuple[CacheEntry, datetime, datetime]] = field(
        default_factory=dict[tuple[str, str], tuple[CacheEntry, datetime, datetime]]
    )

    def get(self, profile: str, key: str, *, now: datetime, ttl: timedelta) -> CacheEntry | None:
        row = self.rows.get((profile, key))
        if row is None or row[1] < now - ttl:
            return None
        self.rows[(profile, key)] = (row[0], row[1], now)
        return row[0]

    def has(self, profile: str, key: str, *, now: datetime, ttl: timedelta) -> bool:
        row = self.rows.get((profile, key))
        return row is not None and row[1] >= now - ttl

    def put(self, profile: str, key: str, entry: CacheEntry, *, now: datetime) -> None:
        self.rows[(profile, key)] = (entry, now, now)

    def prune(self, profile: str, *, now: datetime, ttl: timedelta, max_entries: int) -> int:
        mine = [(k, v) for k, v in self.rows.items() if k[0] == profile]
        dropped = 0
        for k, v in mine:
            if v[1] < now - ttl:
                del self.rows[k]
                dropped += 1
        live = sorted(
            ((k, v) for k, v in self.rows.items() if k[0] == profile),
            key=lambda kv: kv[1][2],
            reverse=True,
        )
        for k, _ in live[max_entries:]:
            del self.rows[k]
            dropped += 1
        return dropped
