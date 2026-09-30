"""Coldline.

===================

File:              src/worker/status_invalidation.py
Component:         Worker — Exception status invalidation (Task 3.10 wiring point)
Purpose:           Remove the cached status copy of the exception the worker just updated.
Interacts With:    src/worker/runtime.py, src/adapters/cache/redis_status_cache.py
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Cache invalidation, staleness, write-triggered freshness
Tools:             Python 3.12

The worker loop calls ``invalidate_exception_status`` right after the use case writes
a processing result to the store (a completion or a failure alike) and before it
acknowledges the delivery. It passes two things: the exception id the result belongs
to and the cache adapter. Removing the copy here moves the freshness guarantee from
the clock to the write: the next status read misses and comes back from the store.

The worker's intermediate write, ``QUEUED`` to ``PROCESSING`` before the provider
call, is not followed by a call here. A copy stored before it can therefore lag the
store until its TTL runs out, which is one of the things the freshness window in
``submission.yaml`` asks you to state.

Task 3.10, Step 3, is the body of ``invalidate_exception_status``.
"""

from domain.status_cache import StatusCache


async def invalidate_exception_status(exception_id: str, cache: StatusCache) -> None:
    """Delete the cached status copy for the exception whose result was just written.

    Arguments, exactly as the worker loop passes them:

    - ``exception_id``: the exception the worker just moved to a terminal state.
    - ``cache``: the same cache adapter the read-through stores copies in, with
      ``get``, ``set`` and ``delete``, every operation keyed by exception id.
    """
    await cache.delete(exception_id)
