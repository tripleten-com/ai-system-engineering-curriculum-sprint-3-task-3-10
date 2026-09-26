"""Coldline.

===================

File:              tests/unit/api/test_status_cache_route.py
Component:         Unit tests — Status route cache headers and configuration loader
Purpose:           Unit tests for the supplied status route, StatusRead, and the TTL loader.
Interacts With:    One isolated source responsibility
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Fast feedback, HTTP boundary, configuration validation
Tools:             Python 3.12, pytest, httpx

These tests exercise what Task 3.10 supplies and never the student's two bodies: the
route reports an outcome and an age whichever way the read-through answers, so they
pass on a fresh starter and on a completed one alike.
"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from adapters.cache import InMemoryStatusCache
from api.document_service import DocumentService
from api.retrieval_workflow import RetrievalWorkflow
from api.routes import create_app
from api.status_cache import (
    CACHE_AGE_HEADER,
    CACHE_OUTCOME_HEADER,
    STATUS_TTL_MAXIMUM_SECONDS,
    STATUS_TTL_MINIMUM_SECONDS,
    CacheConfigurationError,
    ReadOutcome,
    StatusRead,
    load_status_ttl_seconds,
)
from api.use_cases import ReadingApplication
from domain.contracts import ExceptionRecord, ExceptionState, SensorReading
from domain.status_cache import CachedStatus
from tests.doubles import StubRetriever

NOW = datetime(2026, 9, 25, tzinfo=UTC)
PAYLOAD = {
    "reading_id": "reading-route-cache",
    "shipment_id": "shipment-route-cache",
    "temperature_c": 9.6,
    "allowed_min_c": 2.0,
    "allowed_max_c": 8.0,
    "recorded_at": "2026-09-25T00:00:00Z",
}


class MemoryRepository:
    """Persist records for the route tests."""

    def __init__(self) -> None:
        """Initialize empty test state."""
        self.records: dict[str, ExceptionRecord] = {}

    async def get(self, exception_id: str) -> ExceptionRecord | None:
        """Return a stored record."""
        return self.records.get(exception_id)

    async def create(self, record: ExceptionRecord) -> ExceptionRecord:
        """Create or return a record."""
        return self.records.setdefault(record.exception_id, record)

    async def transition(
        self,
        exception_id: str,
        expected: set[ExceptionState],
        target: ExceptionState,
        *,
        summary: str | None = None,
        failure_reason: str | None = None,
    ) -> ExceptionRecord:
        """Apply one test transition."""
        current = self.records[exception_id]
        assert current.state in expected
        updated = current.model_copy(update={"state": target, "updated_at": NOW})
        self.records[exception_id] = updated
        return updated


class MemoryQueue:
    """Record published jobs for the route tests."""

    async def publish(self, job: object) -> str:
        """Record one publication."""
        return "1-0"


async def _objects(prefix: str) -> list[str]:
    """Return no corpus objects; the route under test never lists them."""
    return []


def _app(status_cache: InMemoryStatusCache) -> object:
    """Compose the HTTP layer with in-memory collaborators and the given cache."""
    repository = MemoryRepository()
    application = ReadingApplication(repository, MemoryQueue(), clock=lambda: NOW)
    return create_app(
        application,
        repository,
        RetrievalWorkflow(StubRetriever(()), top_k=5, dense_weight=0.5, token_budget=64),
        _objects,
        DocumentService(lambda: None),
        status_cache=status_cache,
    )


@pytest.mark.asyncio
async def test_status_route_reports_the_cache_outcome_and_age_headers() -> None:
    """Every successful status read carries the outcome (hit or miss) and the copy's age."""
    app = _app(InMemoryStatusCache())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        accepted = await client.post("/api/v1/readings", json=PAYLOAD)
        first = await client.get(accepted.json()["status_url"])
        second = await client.get(accepted.json()["status_url"])

    for response in (first, second):
        assert response.status_code == 200
        assert response.json()["state"] == "QUEUED"
        assert response.headers[CACHE_OUTCOME_HEADER] in {"hit", "miss"}
        assert float(response.headers[CACHE_AGE_HEADER]) >= 0.0
    assert first.headers[CACHE_OUTCOME_HEADER] == "miss", "nothing is cached before the first read"


@pytest.mark.asyncio
async def test_status_route_answers_404_for_an_unknown_exception() -> None:
    """An exception the store has never seen is a 404, with no cache headers."""
    app = _app(InMemoryStatusCache())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/exceptions/exc-unknown")

    assert response.status_code == 404
    assert CACHE_OUTCOME_HEADER not in response.headers


def test_status_read_helpers_carry_the_outcome_and_the_age() -> None:
    """A hit carries the copy's age; a miss is the store's record, zero seconds old."""
    record = ExceptionRecord(
        exception_id="exc-helper",
        reading=SensorReading.model_validate(PAYLOAD),
        state=ExceptionState.QUEUED,
        accepted_at=NOW,
        updated_at=NOW,
    )
    cached = CachedStatus(record=record, stored_at=NOW, age_seconds=4.5)

    hit = StatusRead.hit(cached)
    miss = StatusRead.miss(record)

    assert (hit.record, hit.outcome, hit.age_seconds) == (record, ReadOutcome.HIT, 4.5)
    assert (miss.record, miss.outcome, miss.age_seconds) == (record, ReadOutcome.MISS, 0.0)


@pytest.mark.parametrize(
    "text,message",
    [
        ("status_ttl_seconds: 30\nrefresh_seconds: 5\n", "exactly one key"),
        ("refresh_seconds: 5\n", "exactly one key"),
        ("- 30\n", "exactly one key"),
        ("status_ttl_seconds: thirty\n", "whole number"),
        ("status_ttl_seconds: 30.5\n", "whole number"),
        ("status_ttl_seconds: true\n", "whole number"),
        (f"status_ttl_seconds: {STATUS_TTL_MINIMUM_SECONDS - 1}\n", "must be from"),
        (f"status_ttl_seconds: {STATUS_TTL_MAXIMUM_SECONDS + 1}\n", "must be from"),
    ],
    ids=[
        "second-key",
        "wrong-key",
        "not-a-mapping",
        "prose",
        "fraction",
        "boolean",
        "below-range",
        "above-range",
    ],
)
def test_config_loader_refuses_a_file_the_read_through_cannot_use(
    tmp_path: Path, text: str, message: str
) -> None:
    """A second key, a non-integer, or a value outside the range is refused, and says why."""
    path = tmp_path / "cache.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(CacheConfigurationError, match=message):
        load_status_ttl_seconds(path)


def test_config_loader_accepts_the_supplied_configuration() -> None:
    """The shipped config/cache.yaml loads to a whole number inside the published range."""
    value = load_status_ttl_seconds()

    assert isinstance(value, int)
    assert STATUS_TTL_MINIMUM_SECONDS <= value <= STATUS_TTL_MAXIMUM_SECONDS
