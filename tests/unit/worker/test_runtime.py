"""Coldline.

===================

File:              tests/unit/worker/test_runtime.py
Component:         Unit tests — Test Runtime
Purpose:           Tests for worker-loop resilience at transport boundaries and its invalidation.
Interacts With:    One isolated source responsibility
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Fast feedback, failure paths, state invariants, cache invalidation
Tools:             Python 3.12, pytest, OpenTelemetry

The invalidation test here checks what the loop supplies, the call and its placement,
not the student's body: it replaces ``invalidate_exception_status`` with a recorder for
the duration of the test, so it passes on a fresh starter and on a completed one alike.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from opentelemetry import trace

from adapters.cache import InMemoryStatusCache
from domain.contracts import ExceptionJob, JobDelivery, SensorReading
from domain.status_cache import StatusCache
from worker import runtime
from worker.runtime import run_loop
from worker.use_cases import ProcessingDisposition


def _delivery(message_id: str) -> JobDelivery:
    """Build one valid delivery for a runtime-loop test."""
    return JobDelivery(
        message_id=message_id,
        delivery_count=1,
        job=ExceptionJob(
            exception_id=f"exc-{message_id}",
            accepted_at=datetime.now(UTC),
            reading=SensorReading(
                reading_id=f"reading-{message_id}",
                shipment_id="SHP-TEST",
                temperature_c=9.0,
                allowed_min_c=2.0,
                allowed_max_c=8.0,
                recorded_at=datetime.now(UTC),
            ),
        ),
    )


class ScriptedQueue:
    """Return scripted deliveries and record acknowledgement behavior."""

    def __init__(self, message_ids: tuple[str, ...] = ("1-0", "2-0")) -> None:
        """Prepare the ordered deliveries."""
        self.deliveries = [_delivery(message_id) for message_id in message_ids]
        self.acknowledged: list[str] = []
        self.events: list[str] = []

    async def claim_stale(self, *, minimum_idle_ms: int) -> JobDelivery | None:
        """Return no stale delivery in this focused test."""
        return None

    async def read(self, *, block_ms: int = 1000) -> JobDelivery | None:
        """Return the next delivery or yield while the test cancels the loop."""
        if self.deliveries:
            return self.deliveries.pop(0)
        await asyncio.sleep(60)
        return None

    async def acknowledge(self, message_id: str) -> None:
        """Record one terminal acknowledgement."""
        self.acknowledged.append(message_id)
        self.events.append(f"ack {message_id}")

    async def queue_depth(self) -> int:
        """Return a bounded diagnostic value."""
        return len(self.deliveries)

    async def pending_count(self) -> int:
        """Return a bounded diagnostic value."""
        return 0

    async def publish(self, job: ExceptionJob) -> str:
        """Satisfy the full JobQueue contract; publishing is unused here."""
        return "unused"


class FailThenSucceedApplication:
    """Raise once, then prove that the worker loop continued."""

    def __init__(self) -> None:
        """Create a completion signal and call counter."""
        self.calls = 0
        self.completed = asyncio.Event()

    async def process(self, job: ExceptionJob, *, delivery_count: int) -> ProcessingDisposition:
        """Raise for the first delivery and acknowledge the second."""
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated repository driver error")
        self.completed.set()
        return ProcessingDisposition.ACK


class FailTransportOnceQueue(ScriptedQueue):
    """Raise one transient transport error before returning a delivery."""

    def __init__(self) -> None:
        """Prepare one transport failure and the inherited deliveries."""
        super().__init__()
        self.claim_calls = 0

    async def claim_stale(self, *, minimum_idle_ms: int) -> JobDelivery | None:
        """Fail the first transport call and recover on the next loop."""
        self.claim_calls += 1
        if self.claim_calls == 1:
            raise ConnectionError("queue connection reset")
        return None


class SucceedingApplication:
    """Signal when a delivery reaches the application after transport recovery."""

    def __init__(self) -> None:
        """Create a completion signal."""
        self.completed = asyncio.Event()

    async def process(self, job: ExceptionJob, *, delivery_count: int) -> ProcessingDisposition:
        """Acknowledge one delivery and signal the test."""
        self.completed.set()
        return ProcessingDisposition.ACK


class ScriptedApplication:
    """Return one scripted disposition per delivery and signal when all are spent."""

    def __init__(self, dispositions: tuple[ProcessingDisposition, ...]) -> None:
        """Keep the dispositions to return, in order."""
        self.dispositions = list(dispositions)
        self.completed = asyncio.Event()

    async def process(self, job: ExceptionJob, *, delivery_count: int) -> ProcessingDisposition:
        """Return the next disposition; signal once the last one has been returned."""
        disposition = self.dispositions.pop(0)
        if not self.dispositions:
            self.completed.set()
        return disposition


@pytest.mark.asyncio
async def test_worker_loop_survives_one_unhandled_delivery_error() -> None:
    """One adapter failure must leave that job pending without killing the worker."""
    queue = ScriptedQueue()
    application = FailThenSucceedApplication()
    loop = asyncio.create_task(
        run_loop(
            queue,
            application,  # type: ignore[arg-type] - focused boundary fake
            trace.get_tracer("test"),
            stale_message_ms=30_000,
            status_cache=InMemoryStatusCache(),
            transport_error_backoff_seconds=0,
        )
    )

    await asyncio.wait_for(application.completed.wait(), timeout=1)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop

    assert application.calls == 2
    assert queue.acknowledged == ["2-0"]


@pytest.mark.asyncio
async def test_worker_loop_survives_one_transport_error() -> None:
    """A transient queue read failure must not terminate the worker process."""
    queue = FailTransportOnceQueue()
    application = SucceedingApplication()
    loop = asyncio.create_task(
        run_loop(
            queue,
            application,  # type: ignore[arg-type] - focused boundary fake
            trace.get_tracer("test"),
            stale_message_ms=30_000,
            status_cache=InMemoryStatusCache(),
            transport_error_backoff_seconds=0,
        )
    )

    await asyncio.wait_for(application.completed.wait(), timeout=2)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop

    assert queue.claim_calls >= 2
    assert queue.acknowledged[0] == "1-0"


@pytest.mark.asyncio
async def test_worker_loop_invalidates_after_a_terminal_write_and_before_the_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop calls the invalidation exactly after an ACK'd result, before acknowledging.

    ACK is the disposition the use case returns after writing COMPLETED or FAILED.
    ACK_EXISTING and RETRY wrote no result (or an intermediate one), so no call is made.
    """
    queue = ScriptedQueue(("1-0", "2-0", "3-0"))
    status_cache = InMemoryStatusCache()
    application = ScriptedApplication(
        (
            ProcessingDisposition.ACK,
            ProcessingDisposition.ACK_EXISTING,
            ProcessingDisposition.RETRY,
        )
    )
    invalidated: list[tuple[str, StatusCache]] = []

    async def record(exception_id: str, cache: StatusCache) -> None:
        invalidated.append((exception_id, cache))
        queue.events.append(f"invalidate {exception_id}")

    monkeypatch.setattr(runtime, "invalidate_exception_status", record)
    loop = asyncio.create_task(
        run_loop(
            queue,
            application,  # type: ignore[arg-type] - focused boundary fake
            trace.get_tracer("test"),
            stale_message_ms=30_000,
            status_cache=status_cache,
            transport_error_backoff_seconds=0,
        )
    )

    await asyncio.wait_for(application.completed.wait(), timeout=2)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop

    assert invalidated == [("exc-1-0", status_cache)]
    assert queue.events == ["invalidate exc-1-0", "ack 1-0", "ack 2-0"]
