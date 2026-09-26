"""Coldline.

===================

File:              src/adapters/cache/redis_status_cache.py
Component:         Adapter — Redis status cache
Purpose:           Keep short-lived copies of exception status records in Redis, counting reads.
Interacts With:    Redis, domain.status_cache, Prometheus, the read-through, the invalidation
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Read-through cache, time to live, cache hit and miss, invalidation
Tools:             Python 3.12, Redis, Prometheus, OpenTelemetry

Supplied and protected. Three operations, each keyed by exception id:

- ``get`` returns the cached copy or ``None``. Every call is counted once, as a hit
  (a copy was there) or a miss (it was not), on the two Prometheus counters below.
- ``set`` stores one copy with a TTL: Redis discards it ``ttl_seconds`` after it is
  written (``SET ... EX``). The TTL is a whole number of seconds of at least 1; the
  read-through passes the value from ``config/cache.yaml``.
- ``delete`` removes the copy, so the next ``get`` misses and the read-through goes
  back to the store. Deleting an absent copy is not an error.

The counters are registered when this module is imported, which both composition
roots do, so ``/metrics`` lists them from the first scrape. Only the API reads
through the cache, so the worker's own copy of the two counters stays at zero.
"""

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from opentelemetry import trace
from prometheus_client import Counter
from redis.asyncio import Redis

from domain.contracts import ExceptionRecord
from domain.status_cache import CachedStatus

_TRACER = trace.get_tracer(__name__)
KEY_PREFIX = "coldline:status:"
STATUS_CACHE_HITS = Counter(
    "coldline_status_cache_hits_total",
    "Count exception status cache reads that found a copy.",
)
STATUS_CACHE_MISSES = Counter(
    "coldline_status_cache_misses_total",
    "Count exception status cache reads that found no copy.",
)


def create_redis_client(url: str) -> Redis:
    """Return one Redis client for the cache role the stack already runs.

    The client connects lazily, on its first command, so a composition root can
    build it before its event loop is running.
    """
    client: Redis = Redis.from_url(url, decode_responses=True)
    return client


def status_key(exception_id: str) -> str:
    """Return the Redis key that holds one exception's cached status copy."""
    return f"{KEY_PREFIX}{exception_id}"


def encode_copy(record: ExceptionRecord, stored_at: float) -> str:
    """Serialize one copy together with the moment it was stored, as JSON."""
    return json.dumps({"record": record.model_dump(mode="json"), "stored_at": stored_at})


def decode_copy(payload: str, *, now: float) -> CachedStatus:
    """Deserialize one copy and compute its age at ``now`` (epoch seconds)."""
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError("cached status copy is not one JSON object")
    stored_at = float(document["stored_at"])
    return CachedStatus(
        record=ExceptionRecord.model_validate(document["record"]),
        stored_at=datetime.fromtimestamp(stored_at, UTC),
        age_seconds=max(0.0, now - stored_at),
    )


class RedisStatusCache:
    """Translate the provider-neutral ``StatusCache`` contract to Redis strings."""

    def __init__(self, client: Redis, *, clock: Callable[[], float] = time.time) -> None:
        """Bind the adapter to one Redis client and one clock (epoch seconds)."""
        self._client = client
        self._clock = clock

    async def get(self, exception_id: str) -> CachedStatus | None:
        """Return the cached copy for one exception, counting the read as a hit or a miss."""
        with _TRACER.start_as_current_span("status_cache.get"):
            payload = await self._client.get(status_key(exception_id))
        if payload is None:
            STATUS_CACHE_MISSES.inc()
            return None
        STATUS_CACHE_HITS.inc()
        return decode_copy(cast(str, payload), now=self._clock())

    async def set(self, exception_id: str, record: ExceptionRecord, *, ttl_seconds: int) -> None:
        """Store one copy that Redis discards ``ttl_seconds`` after it is written."""
        if isinstance(ttl_seconds, bool) or ttl_seconds < 1:
            raise ValueError("ttl_seconds must be a whole number of seconds of at least 1")
        with _TRACER.start_as_current_span("status_cache.set"):
            await self._client.set(
                status_key(exception_id), encode_copy(record, self._clock()), ex=ttl_seconds
            )

    async def delete(self, exception_id: str) -> None:
        """Remove the cached copy for one exception, if there is one."""
        with _TRACER.start_as_current_span("status_cache.delete"):
            await self._client.delete(status_key(exception_id))

    async def aclose(self) -> None:
        """Release the Redis connections this adapter owns."""
        await self._client.aclose()
