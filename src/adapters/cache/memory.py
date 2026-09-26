"""Coldline.

===================

File:              src/adapters/cache/memory.py
Component:         Adapter — In-memory status cache
Purpose:           Provide the dependency-free StatusCache the unit and contract tests compose.
Interacts With:    domain.status_cache, the API read-through, the worker invalidation, tests
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Test double, time to live, cache hit and miss
Tools:             Python 3.12

This double keeps the same contract as ``RedisStatusCache`` (keyed by exception id, a
copy expires ``ttl_seconds`` after it is stored, ``delete`` removes it) inside one
process, with an injectable clock so a test can move time forward. It records what was
stored, so a test can read the TTL a caller passed. It touches no Prometheus counter:
the two cache counters belong to the Redis adapter the running stack composes.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from domain.contracts import ExceptionRecord
from domain.status_cache import CachedStatus


@dataclass(frozen=True)
class StoredCopy:
    """One copy this double holds, with when it was stored and for how long."""

    record: ExceptionRecord
    stored_at: float
    ttl_seconds: int


class InMemoryStatusCache:
    """Hold status copies in a dictionary with the Redis adapter's expiry semantics."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        """Start empty, reading time from ``clock`` (epoch seconds)."""
        self._clock = clock
        self.entries: dict[str, StoredCopy] = {}
        self.hits = 0
        self.misses = 0
        self.deleted: list[str] = []

    async def get(self, exception_id: str) -> CachedStatus | None:
        """Return the unexpired copy for one exception, or None."""
        copy = self.entries.get(exception_id)
        now = self._clock()
        if copy is not None and now >= copy.stored_at + copy.ttl_seconds:
            del self.entries[exception_id]
            copy = None
        if copy is None:
            self.misses += 1
            return None
        self.hits += 1
        return CachedStatus(
            record=copy.record,
            stored_at=datetime.fromtimestamp(copy.stored_at, UTC),
            age_seconds=max(0.0, now - copy.stored_at),
        )

    async def set(self, exception_id: str, record: ExceptionRecord, *, ttl_seconds: int) -> None:
        """Store one copy that expires ``ttl_seconds`` after now."""
        if isinstance(ttl_seconds, bool) or ttl_seconds < 1:
            raise ValueError("ttl_seconds must be a whole number of seconds of at least 1")
        self.entries[exception_id] = StoredCopy(record, self._clock(), ttl_seconds)

    async def delete(self, exception_id: str) -> None:
        """Remove the copy for one exception and remember that it was asked for."""
        self.deleted.append(exception_id)
        self.entries.pop(exception_id, None)
