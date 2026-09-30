"""Coldline.

===================

File:              src/api/status_cache.py
Component:         API — Exception status read-through (Task 3.10 wiring point)
Purpose:           Answer one status read from the cache when a copy exists, else from the store.
Interacts With:    src/api/routes.py, src/adapters/cache/redis_status_cache.py, config/cache.yaml
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Read-through cache, time to live, cache hit and miss, staleness
Tools:             Python 3.12, PyYAML

The status route calls ``read_exception_status`` on every ``GET /api/v1/exceptions/{id}``
and passes it three things: the exception id, the cache adapter (``get``, ``set``,
``delete``, keyed by exception id) and the exception repository (the store). What the
route returns to the caller is the record inside the ``StatusRead`` this function hands
back, plus two response headers that say whether the read was a hit or a miss and how
old the copy it served was. ``poe cache-probe`` reads those two headers.

Task 3.10, Step 1, is the body of ``read_exception_status``. The TTL it stores a copy
with is ``status_ttl_seconds()``, read once per process from ``config/cache.yaml``.
"""

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml

from domain.contracts import ExceptionRecord
from domain.repositories import ExceptionRepository
from domain.status_cache import CachedStatus, StatusCache

CACHE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config/cache.yaml"
# The published range for status_ttl_seconds, in whole seconds, inclusive at both ends.
# config/cache.yaml repeats it in its comments; the config check and the API's own
# start-up validation both enforce it from here.
STATUS_TTL_MINIMUM_SECONDS = 5
STATUS_TTL_MAXIMUM_SECONDS = 120
# The two response headers the status route adds to every successful read.
CACHE_OUTCOME_HEADER = "X-Coldline-Cache"
CACHE_AGE_HEADER = "X-Coldline-Cache-Age-Seconds"


class ReadOutcome(StrEnum):
    """Say where one status read was answered from."""

    HIT = "hit"
    """The cache held a copy and the copy was returned; the store was not read."""

    MISS = "miss"
    """The cache held no copy; the store was read."""


@dataclass(frozen=True)
class StatusRead:
    """Carry one answered status read: the record, where it came from, and the copy's age."""

    record: ExceptionRecord
    outcome: ReadOutcome
    age_seconds: float

    @classmethod
    def hit(cls, cached: CachedStatus) -> "StatusRead":
        """Build the read a cache hit produces: the cached record, as old as the copy is."""
        return cls(record=cached.record, outcome=ReadOutcome.HIT, age_seconds=cached.age_seconds)

    @classmethod
    def miss(cls, record: ExceptionRecord) -> "StatusRead":
        """Build the read a cache miss produces: the store's record, zero seconds old."""
        return cls(record=record, outcome=ReadOutcome.MISS, age_seconds=0.0)


class CacheConfigurationError(ValueError):
    """Report a config/cache.yaml that the read-through cannot use."""


def load_status_ttl_seconds(path: Path = CACHE_CONFIG_PATH) -> int:
    """Read ``status_ttl_seconds`` from the cache configuration and enforce its range.

    The file holds exactly that one key. A missing key, a second key, a value that
    is not a whole number, or a value outside the published range is refused, so a
    typo never turns into a cache that keeps copies for an unintended time.
    """
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CacheConfigurationError(f"{path.name} is unreadable: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"status_ttl_seconds"}:
        raise CacheConfigurationError(f"{path.name} must hold exactly one key, status_ttl_seconds")
    value = document["status_ttl_seconds"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise CacheConfigurationError("status_ttl_seconds must be a whole number of seconds")
    if not STATUS_TTL_MINIMUM_SECONDS <= value <= STATUS_TTL_MAXIMUM_SECONDS:
        raise CacheConfigurationError(
            f"status_ttl_seconds must be from {STATUS_TTL_MINIMUM_SECONDS} to "
            f"{STATUS_TTL_MAXIMUM_SECONDS} seconds; got {value}"
        )
    return value


@lru_cache(maxsize=1)
def status_ttl_seconds() -> int:
    """Return the configured TTL, read once per process from config/cache.yaml."""
    return load_status_ttl_seconds()


async def read_exception_status(
    exception_id: str, cache: StatusCache, repository: ExceptionRepository
) -> StatusRead | None:
    """Answer one status read: from the cache on a hit, from the store on a miss.

    Arguments, exactly as the status route passes them:

    - ``exception_id``: the id from the request path; every cache operation is keyed by it.
    - ``cache``: the cache adapter, with ``get``, ``set(..., ttl_seconds=...)`` and ``delete``.
    - ``repository``: the exception store; its ``get`` returns the record or ``None``.

    Return ``StatusRead.hit(cached)`` when the cache holds a copy, ``StatusRead.miss(record)``
    after reading the store, or ``None`` when the store has no such record (the route then
    answers 404). Do not cache a missing record: a copy can never outlive a record that
    does not exist yet.
    """
    cached = await cache.get(exception_id)
    if cached is not None:
        return StatusRead.hit(cached)
    record = await repository.get(exception_id)
    if record is None:
        # A record the store has never seen stays a store read and a 404, so no copy
        # can outlive it and hide the record that arrives a moment later.
        return None
    await cache.set(exception_id, record, ttl_seconds=status_ttl_seconds())
    return StatusRead.miss(record)
