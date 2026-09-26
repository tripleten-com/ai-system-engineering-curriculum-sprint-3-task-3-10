"""Coldline.

===================

File:              src/adapters/cache/__init__.py
Component:         Cache adapters — Package exports
Purpose:           Expose the exception status cache adapters.
Interacts With:    Domain contracts and the local Redis role
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Read-through cache, time to live, invalidation
Tools:             Python 3.12, Redis
"""

from adapters.cache.memory import InMemoryStatusCache
from adapters.cache.redis_status_cache import (
    STATUS_CACHE_HITS,
    STATUS_CACHE_MISSES,
    RedisStatusCache,
    create_redis_client,
)

__all__ = [
    "STATUS_CACHE_HITS",
    "STATUS_CACHE_MISSES",
    "InMemoryStatusCache",
    "RedisStatusCache",
    "create_redis_client",
]
