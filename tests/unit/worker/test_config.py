"""Coldline.

===================

File:              tests/unit/worker/test_config.py
Component:         Unit tests — Test Config
Purpose:           Unit tests for the worker configuration boundary.
Interacts With:    One isolated source responsibility
Sprint/Task:       Sprint 1 — Project 1
Concepts:          Fast feedback, failure paths, state invariants
Tools:             Python 3.12, pytest, Redis, Pydantic
"""

import pytest
from pydantic import ValidationError

from worker.config import WorkerSettings


def test_worker_settings_require_dependency_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker must not silently invent working dependency credentials."""
    monkeypatch.delenv("COLDLINE_DATABASE_URL", raising=False)
    monkeypatch.delenv("COLDLINE_REDIS_URL", raising=False)
    monkeypatch.delenv("COLDLINE_OTEL_ENDPOINT", raising=False)

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)


def test_worker_settings_keep_the_bounded_retry_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose values must retain three attempts and 30-second stale claiming."""
    monkeypatch.setenv("COLDLINE_DATABASE_URL", "postgresql://user:pass@postgres:5432/coldline")
    monkeypatch.setenv("COLDLINE_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("COLDLINE_OTEL_ENDPOINT", "http://jaeger:4317")

    settings = WorkerSettings(_env_file=None)

    assert settings.maximum_attempts == 3
    assert settings.stale_message_ms == 30_000
    assert settings.model_latency_ms == 250
