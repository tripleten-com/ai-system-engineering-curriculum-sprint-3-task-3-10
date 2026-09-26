"""Coldline.

===================

File:              tests/unit/adapters/test_status_cache.py
Component:         Unit tests — Status cache adapters
Purpose:           Unit tests for the Redis status cache adapter and its in-memory double.
Interacts With:    One isolated source responsibility
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Fast feedback, time to live, cache hit and miss, counters
Tools:             Python 3.12, pytest, Prometheus
"""

from datetime import UTC, datetime

import pytest
from prometheus_client import REGISTRY

from adapters.cache import InMemoryStatusCache, RedisStatusCache
from adapters.cache.redis_status_cache import decode_copy, encode_copy, status_key
from domain.contracts import ExceptionRecord, ExceptionState, SensorReading

NOW = datetime(2026, 9, 25, tzinfo=UTC)
RECORD = ExceptionRecord(
    exception_id="exc-unit-cache-001",
    reading=SensorReading(
        reading_id="reading-unit-cache",
        shipment_id="shipment-unit-cache",
        temperature_c=9.6,
        allowed_min_c=2.0,
        allowed_max_c=8.0,
        recorded_at=NOW,
    ),
    state=ExceptionState.QUEUED,
    accepted_at=NOW,
    updated_at=NOW,
)


class Clock:
    """Hold a settable time shared by the fake Redis and the adapter under test."""

    def __init__(self) -> None:
        """Start at a fixed epoch second."""
        self.now = 1_000.0

    def __call__(self) -> float:
        """Return the current test time."""
        return self.now


class FakeRedis:
    """Answer GET, SET EX, and DEL like Redis strings, expiring keys by the shared clock."""

    def __init__(self, clock: Clock) -> None:
        """Start empty."""
        self._clock = clock
        self.values: dict[str, tuple[str, float | None]] = {}
        self.closed = False

    async def get(self, name: str) -> str | None:
        """Return the value unless the key is absent or expired."""
        entry = self.values.get(name)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and self._clock() >= expires_at:
            del self.values[name]
            return None
        return value

    async def set(self, name: str, value: str, *, ex: int | None = None) -> bool:
        """Store the value, expiring it ``ex`` seconds from now when given."""
        self.values[name] = (value, None if ex is None else self._clock() + ex)
        return True

    async def delete(self, *names: str) -> int:
        """Remove the keys and return how many existed."""
        removed = sum(1 for name in names if self.values.pop(name, None) is not None)
        return removed

    async def aclose(self) -> None:
        """Record that the client was closed."""
        self.closed = True


def _counter(name: str) -> float:
    """Read one counter's current value from the default registry."""
    value = REGISTRY.get_sample_value(name)
    return 0.0 if value is None else value


@pytest.mark.asyncio
async def test_redis_cache_counts_a_miss_when_no_copy_exists() -> None:
    """An absent key is a miss, counted once, and returns None."""
    clock = Clock()
    adapter = RedisStatusCache(FakeRedis(clock), clock=clock)  # type: ignore[arg-type]
    misses_before = _counter("coldline_status_cache_misses_total")
    hits_before = _counter("coldline_status_cache_hits_total")

    assert await adapter.get(RECORD.exception_id) is None

    assert _counter("coldline_status_cache_misses_total") == misses_before + 1
    assert _counter("coldline_status_cache_hits_total") == hits_before


@pytest.mark.asyncio
async def test_redis_cache_round_trips_a_copy_and_reports_its_age() -> None:
    """A stored copy comes back intact, as a hit, with its age measured from when it was set."""
    clock = Clock()
    redis = FakeRedis(clock)
    adapter = RedisStatusCache(redis, clock=clock)  # type: ignore[arg-type]
    hits_before = _counter("coldline_status_cache_hits_total")

    await adapter.set(RECORD.exception_id, RECORD, ttl_seconds=30)
    clock.now += 2.25
    cached = await adapter.get(RECORD.exception_id)

    assert cached is not None
    assert cached.record == RECORD
    assert cached.age_seconds == pytest.approx(2.25)
    assert cached.stored_at == datetime.fromtimestamp(1_000.0, UTC)
    assert status_key(RECORD.exception_id) in redis.values
    assert _counter("coldline_status_cache_hits_total") == hits_before + 1


@pytest.mark.asyncio
async def test_redis_cache_copy_expires_after_its_ttl() -> None:
    """Once the TTL has passed the copy is gone and the read is a miss."""
    clock = Clock()
    adapter = RedisStatusCache(FakeRedis(clock), clock=clock)  # type: ignore[arg-type]

    await adapter.set(RECORD.exception_id, RECORD, ttl_seconds=5)
    clock.now += 4.9
    assert await adapter.get(RECORD.exception_id) is not None
    clock.now += 0.2

    assert await adapter.get(RECORD.exception_id) is None


@pytest.mark.asyncio
async def test_redis_cache_delete_removes_the_copy_and_tolerates_absence() -> None:
    """Delete removes an existing copy; deleting again, or an unknown id, is not an error."""
    clock = Clock()
    adapter = RedisStatusCache(FakeRedis(clock), clock=clock)  # type: ignore[arg-type]
    await adapter.set(RECORD.exception_id, RECORD, ttl_seconds=30)

    await adapter.delete(RECORD.exception_id)
    await adapter.delete(RECORD.exception_id)
    await adapter.delete("exc-never-stored")

    assert await adapter.get(RECORD.exception_id) is None


@pytest.mark.asyncio
async def test_redis_cache_rejects_a_ttl_below_one_second() -> None:
    """A copy that would never expire, or a boolean, is refused before it reaches Redis."""
    clock = Clock()
    redis = FakeRedis(clock)
    adapter = RedisStatusCache(redis, clock=clock)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="at least 1"):
        await adapter.set(RECORD.exception_id, RECORD, ttl_seconds=0)
    with pytest.raises(ValueError, match="at least 1"):
        await adapter.set(RECORD.exception_id, RECORD, ttl_seconds=True)

    assert redis.values == {}


@pytest.mark.asyncio
async def test_redis_cache_closes_its_client() -> None:
    """Closing the adapter releases the client it was given."""
    clock = Clock()
    redis = FakeRedis(clock)

    await RedisStatusCache(redis, clock=clock).aclose()  # type: ignore[arg-type]

    assert redis.closed


def test_encode_and_decode_preserve_the_record_and_the_stored_time() -> None:
    """The JSON copy carries the whole record and when it was stored."""
    payload = encode_copy(RECORD, 1_000.0)

    cached = decode_copy(payload, now=1_003.0)

    assert cached.record == RECORD
    assert cached.age_seconds == pytest.approx(3.0)
    with pytest.raises(ValueError, match="one JSON object"):
        decode_copy("[]", now=1_003.0)


@pytest.mark.asyncio
async def test_in_memory_cache_mirrors_the_redis_semantics() -> None:
    """The double stores with a TTL, expires, deletes, and counts its own hits and misses."""
    clock = Clock()
    double = InMemoryStatusCache(clock=clock)

    assert await double.get(RECORD.exception_id) is None
    await double.set(RECORD.exception_id, RECORD, ttl_seconds=10)
    clock.now += 1.0
    cached = await double.get(RECORD.exception_id)
    assert cached is not None and cached.age_seconds == pytest.approx(1.0)
    assert double.entries[RECORD.exception_id].ttl_seconds == 10
    clock.now += 9.5
    assert await double.get(RECORD.exception_id) is None
    await double.set(RECORD.exception_id, RECORD, ttl_seconds=10)
    await double.delete(RECORD.exception_id)

    assert double.entries == {}
    assert double.deleted == [RECORD.exception_id]
    assert (double.hits, double.misses) == (1, 2)
    with pytest.raises(ValueError, match="at least 1"):
        await double.set(RECORD.exception_id, RECORD, ttl_seconds=0)
