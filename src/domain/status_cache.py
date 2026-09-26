"""Coldline.

===================

File:              src/domain/status_cache.py
Component:         Domain — Status cache contract
Purpose:           Define the provider-neutral contract for a cached exception status copy.
Interacts With:    The API read-through, the worker invalidation, and the cache adapters
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Read-through cache, time to live, staleness, invalidation
Tools:             Python 3.12, Pydantic
"""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from domain.contracts import ExceptionRecord


class CachedStatus(BaseModel):
    """Carry one cached copy of an exception record and how old it is.

    ``stored_at`` is when the copy was written and ``age_seconds`` how long ago
    that was at the moment it was read back. A copy is a snapshot: the store may
    have moved on since ``stored_at``, and that gap is what Task 3.10 measures.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: ExceptionRecord
    stored_at: datetime
    age_seconds: float = Field(ge=0.0)


class StatusCache(Protocol):
    """Keep short-lived copies of exception status records, keyed by exception id.

    This is an internal collaborator like ``ExceptionRepository``, not one of the
    five application ports: it caches the store's answer, it does not provide a
    capability of its own. Every operation is keyed by the exception id, so the
    copy the read-through stores is the copy the invalidation deletes.
    """

    async def get(self, exception_id: str) -> CachedStatus | None:
        """Return the cached copy for one exception, or None when there is none."""
        ...

    async def set(self, exception_id: str, record: ExceptionRecord, *, ttl_seconds: int) -> None:
        """Store one copy that expires ``ttl_seconds`` after it is written."""
        ...

    async def delete(self, exception_id: str) -> None:
        """Remove the cached copy for one exception; an absent copy is not an error."""
        ...
