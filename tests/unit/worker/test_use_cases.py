"""Coldline.

===================

File:              tests/unit/worker/test_use_cases.py
Component:         Unit tests — Test Use Cases
Purpose:           Unit tests for exception-worker application behavior.
Interacts With:    One isolated source responsibility
Sprint/Task:       Sprint 1 — Project 1
Concepts:          Fast feedback, failure paths, state invariants
Tools:             Python 3.12, pytest
"""

from datetime import UTC, datetime

import pytest

from domain.contracts import (
    ExceptionJob,
    ExceptionRecord,
    ExceptionState,
    ModelSummary,
    SensorReading,
)
from domain.errors import TerminalProviderError
from worker.use_cases import ProcessingDisposition, WorkerApplication


class MemoryRepository:
    """Store one exception record for worker tests."""

    def __init__(self, record: ExceptionRecord) -> None:
        """Initialize the repository with one durable record."""
        self.record = record

    async def get(self, exception_id: str) -> ExceptionRecord | None:
        """Return the record when identities match."""
        return self.record if exception_id == self.record.exception_id else None

    async def create(self, record: ExceptionRecord) -> ExceptionRecord:
        """Reject unexpected creation in worker tests."""
        raise AssertionError("worker must not create exception records")

    async def transition(
        self,
        exception_id: str,
        expected: set[ExceptionState],
        target: ExceptionState,
        *,
        summary: str | None = None,
        failure_reason: str | None = None,
    ) -> ExceptionRecord:
        """Apply one state transition."""
        assert self.record.state in expected
        self.record = self.record.model_copy(
            update={
                "state": target,
                "summary": summary if summary is not None else self.record.summary,
                "failure_reason": failure_reason,
                "updated_at": NOW,
            }
        )
        return self.record


class RecordingProvider:
    """Return a fixed summary or raise a fixed error."""

    def __init__(self, *, fail: bool = False) -> None:
        """Configure a recording or failing provider."""
        self.fail = fail
        self.calls = 0

    async def summarize(self, request: object) -> ModelSummary:
        """Record one provider call."""
        self.calls += 1
        if self.fail:
            raise TimeoutError("provider timeout")
        return ModelSummary(summary="bounded synthetic summary", provider="deterministic-local")


class TerminalRecordingProvider:
    """Raise a pre-classified terminal failure, as a bounded resilience wrapper would."""

    def __init__(self) -> None:
        """Track how many times the worker called the provider."""
        self.calls = 0

    async def summarize(self, request: object) -> ModelSummary:
        """Record one call and raise a terminal, non-retryable failure."""
        self.calls += 1
        raise TerminalProviderError("request rejected as permanently invalid")


NOW = datetime(2026, 8, 28, tzinfo=UTC)
READING = SensorReading(
    reading_id="reading-syn-001",
    shipment_id="shipment-syn-001",
    temperature_c=9.2,
    allowed_min_c=2.0,
    allowed_max_c=8.0,
    recorded_at=NOW,
)
JOB = ExceptionJob(exception_id="exc-001", reading=READING, accepted_at=NOW)


def queued_record() -> ExceptionRecord:
    """Return the durable starting record for a worker test."""
    return ExceptionRecord(
        exception_id=JOB.exception_id,
        reading=READING,
        state=ExceptionState.QUEUED,
        accepted_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_successful_job_reaches_completed_terminal_state() -> None:
    """A provider success must be persisted before acknowledgement."""
    repository = MemoryRepository(queued_record())
    application = WorkerApplication(repository, RecordingProvider(), clock=lambda: NOW)

    disposition = await application.process(JOB, delivery_count=1)

    assert disposition is ProcessingDisposition.ACK
    assert repository.record.state is ExceptionState.COMPLETED
    assert repository.record.summary == "bounded synthetic summary"


@pytest.mark.asyncio
async def test_completed_duplicate_is_acknowledged_without_provider_call() -> None:
    """A duplicate delivery must not repeat a completed provider effect."""
    record = queued_record().model_copy(
        update={"state": ExceptionState.COMPLETED, "summary": "already complete"}
    )
    repository = MemoryRepository(record)
    provider = RecordingProvider()
    application = WorkerApplication(repository, provider, clock=lambda: NOW)

    disposition = await application.process(JOB, delivery_count=2)

    assert disposition is ProcessingDisposition.ACK_EXISTING
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_delivery_without_a_durable_record_is_named_explicitly() -> None:
    """A broken persistence-before-publish invariant must not look like a safe replay."""
    repository = MemoryRepository(queued_record())
    provider = RecordingProvider()
    application = WorkerApplication(repository, provider, clock=lambda: NOW)
    missing_job = JOB.model_copy(update={"exception_id": "exc-missing"})

    disposition = await application.process(missing_job, delivery_count=1)

    assert disposition is ProcessingDisposition.ACK_MISSING
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_inflight_duplicate_waits_without_repeating_provider_work() -> None:
    """A second delivery must not run the provider while the first is processing."""
    repository = MemoryRepository(
        queued_record().model_copy(update={"state": ExceptionState.PROCESSING})
    )
    provider = RecordingProvider()
    application = WorkerApplication(repository, provider, clock=lambda: NOW)

    disposition = await application.process(JOB, delivery_count=2)

    assert disposition is ProcessingDisposition.RETRY
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_third_inflight_delivery_records_terminal_failure() -> None:
    """A crash-stranded processing record must not remain pending forever."""
    repository = MemoryRepository(
        queued_record().model_copy(update={"state": ExceptionState.PROCESSING})
    )
    provider = RecordingProvider()
    application = WorkerApplication(repository, provider, clock=lambda: NOW)

    disposition = await application.process(JOB, delivery_count=3)

    assert disposition is ProcessingDisposition.ACK
    assert repository.record.state is ExceptionState.FAILED
    assert repository.record.failure_reason == "processing_attempts_exhausted"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_provider_failure_retries_before_terminal_attempt() -> None:
    """A non-final provider failure must leave the message pending for retry."""
    repository = MemoryRepository(queued_record())
    application = WorkerApplication(repository, RecordingProvider(fail=True), clock=lambda: NOW)

    disposition = await application.process(JOB, delivery_count=2)

    assert disposition is ProcessingDisposition.RETRY
    assert repository.record.state is ExceptionState.QUEUED


@pytest.mark.asyncio
async def test_third_provider_failure_is_recorded_and_acknowledged() -> None:
    """The third failure must become an observable terminal outcome."""
    repository = MemoryRepository(queued_record())
    application = WorkerApplication(repository, RecordingProvider(fail=True), clock=lambda: NOW)

    disposition = await application.process(JOB, delivery_count=3)

    assert disposition is ProcessingDisposition.ACK
    assert repository.record.state is ExceptionState.FAILED
    assert repository.record.failure_reason == "model_provider_exhausted"


@pytest.mark.assessed
@pytest.mark.asyncio
async def test_terminal_provider_failure_is_recorded_on_the_first_delivery() -> None:
    """A terminal failure must not wait for the retryable-exhaustion budget."""
    repository = MemoryRepository(queued_record())
    provider = TerminalRecordingProvider()
    application = WorkerApplication(repository, provider, clock=lambda: NOW)

    disposition = await application.process(JOB, delivery_count=1)

    assert disposition is ProcessingDisposition.ACK
    assert repository.record.state is ExceptionState.FAILED
    assert repository.record.failure_reason == "model_provider_terminal_failure"
    assert provider.calls == 1


@pytest.mark.assessed
@pytest.mark.asyncio
async def test_terminal_provider_failure_at_the_final_delivery_is_still_distinct() -> None:
    """Even at the last delivery, a terminal outcome keeps its own failure reason.

    A retryable failure exhausted at the final delivery also reaches FAILED/ACK,
    so disposition and state alone cannot tell the two outcomes apart there. The
    failure reason is the only observable that must still distinguish them.
    """
    repository = MemoryRepository(queued_record())
    provider = TerminalRecordingProvider()
    application = WorkerApplication(repository, provider, clock=lambda: NOW)

    disposition = await application.process(JOB, delivery_count=3)

    assert disposition is ProcessingDisposition.ACK
    assert repository.record.state is ExceptionState.FAILED
    assert repository.record.failure_reason == "model_provider_terminal_failure"
    assert repository.record.failure_reason != "model_provider_exhausted"
