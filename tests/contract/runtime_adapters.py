"""Coldline.

===================

File:              tests/contract/runtime_adapters.py
Component:         Contract tests — Runtime Adapters
Purpose:           Exercise PostgreSQL and SQS adapter behavior against the running stack.
Interacts With:    Published interfaces and repository boundaries
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Compatibility, ownership, export safety, dead-letter redrive
Tools:             Python 3.12, pytest, PostgreSQL, boto3, LocalStack
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg

from adapters.persistence import PostgresExceptionRepository
from adapters.queue import SqsJobQueue, create_sqs_client, ensure_queue
from domain.contracts import (
    ExceptionJob,
    ExceptionRecord,
    ExceptionState,
    JobDelivery,
    ModelRequest,
    ModelSummary,
    SensorReading,
)
from worker.use_cases import ProcessingDisposition, WorkerApplication

DATABASE_URL = "postgresql://coldline:coldline_local@postgres:5432/coldline"
LOCALSTACK_ENDPOINT = "http://localstack:4566"


class AlwaysFailProvider:
    """Fail deterministically at the active model-provider boundary."""

    async def summarize(self, request: ModelRequest) -> ModelSummary:
        """Raise the fixed integration failure."""
        raise TimeoutError("integration provider failure")


async def verify() -> None:
    """Verify initialization, durable identity, and dead-letter recovery."""
    identity = uuid4().hex
    exception_id = f"verify-{identity}"
    queue_name = f"coldline-verify-{identity}"
    dead_letter_name = f"{queue_name}-dlq"
    now = datetime.now(UTC)
    reading = SensorReading(
        reading_id=f"reading-{identity}",
        shipment_id="shipment-integration",
        temperature_c=9.2,
        allowed_min_c=2.0,
        allowed_max_c=8.0,
        recorded_at=now,
    )
    record = ExceptionRecord(
        exception_id=exception_id,
        reading=reading,
        state=ExceptionState.RECEIVED,
        accepted_at=now,
        updated_at=now,
    )
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    sqs = create_sqs_client(
        endpoint_url=LOCALSTACK_ENDPOINT,
        region_name="us-east-1",
        access_key_id="localstack-development-key",
        secret_access_key="localstack-development-secret",
    )
    try:
        assert await pool.fetchval("SELECT to_regclass('public.exceptions')") == "exceptions"

        repository = PostgresExceptionRepository(pool)
        created = await repository.create(record)
        duplicate = await repository.create(record)
        assert created == duplicate
        queued = await repository.transition(
            exception_id, {ExceptionState.RECEIVED}, ExceptionState.QUEUED
        )
        assert queued.state is ExceptionState.QUEUED

        queue_url, dlq_url = await asyncio.to_thread(
            ensure_queue,
            sqs,
            queue_name=queue_name,
            dead_letter_name=dead_letter_name,
            visibility_timeout_seconds=1,
            max_receive_count=2,
        )
        queue = SqsJobQueue(sqs, queue_url=queue_url, wait_time_seconds=1)
        job = ExceptionJob(exception_id=exception_id, reading=reading, accepted_at=now)
        await queue.publish(job)
        first = await queue.read(block_ms=2000)
        assert first is not None and first.job == job
        assert first.delivery_count == 1
        assert await queue.pending_count() == 1

        # A message the first receiver never acknowledges becomes visible to any
        # receiver again once the queue's own visibility timeout elapses; SQS
        # counts that as the *same* message's second delivery, not a duplicate.
        # LocalStack's own revisibility latency runs measurably past the
        # configured 1 s timeout, so this polls rather than sleeping once and
        # checking a single time.
        second = await _poll_for_redelivery(queue, job)
        assert second is not None and second.job == job
        assert second.delivery_count == 2
        await queue.acknowledge(second.message_id)
        assert await queue.pending_count() == 0

        # One delivery beyond the bound moves the message to the dead-letter
        # queue automatically; nothing here acknowledges it, so this proves the
        # *queue's own* redrive policy, not application-level handling of it.
        await queue.publish(job)
        for _ in range(3):
            delivery = await queue.read(block_ms=2000)
            if delivery is None:
                break
            await asyncio.sleep(1.2)
        dlq_depth = await asyncio.to_thread(
            lambda: int(
                sqs.get_queue_attributes(
                    QueueUrl=dlq_url, AttributeNames=["ApproximateNumberOfMessages"]
                )["Attributes"]["ApproximateNumberOfMessages"]
            )
        )
        assert dlq_depth >= 1

        terminal = await WorkerApplication(
            repository,
            AlwaysFailProvider(),
            clock=lambda: now,
            maximum_attempts=3,
        ).process(job, delivery_count=3)
        failed = await repository.get(exception_id)
        assert terminal is ProcessingDisposition.ACK
        assert failed is not None and failed.state is ExceptionState.FAILED
        assert failed.failure_reason == "model_provider_exhausted"

        # The *configured production queue* — not this test's own throwaway
        # queue — must give a delivery at least one retry before redrive: a
        # worker that crashes once, never acknowledging what it received,
        # must still get the job back rather than lose it to the dead-letter
        # queue on the very next receive. This is what makes
        # queue_max_receive_count=1 a wrong configuration even though it is
        # within the published Field bounds.
        production_url = await asyncio.to_thread(
            lambda: sqs.get_queue_url(QueueName="coldline-exception-jobs")["QueueUrl"]
        )
        production_queue = SqsJobQueue(sqs, queue_url=production_url, wait_time_seconds=1)
        recovery_job = ExceptionJob(
            exception_id=f"recovery-{identity}", reading=reading, accepted_at=now
        )
        await production_queue.publish(recovery_job)
        try:
            first_receive = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=production_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=2,
            )
            messages = first_receive.get("Messages")
            assert messages, "the first receive did not observe the just-published message"
            # A per-call VisibilityTimeout on receive_message is not reliably honored by
            # this LocalStack version; change_message_visibility on the message already
            # in hand is the documented, dependable way to force it visible again
            # without waiting out the production queue's real (much longer) timeout.
            await asyncio.to_thread(
                sqs.change_message_visibility,
                QueueUrl=production_url,
                ReceiptHandle=messages[0]["ReceiptHandle"],
                VisibilityTimeout=0,
            )
            recovered = await _poll_for_redelivery(production_queue, recovery_job)
            assert (
                recovered is not None
                and recovered.job == recovery_job
                and recovered.delivery_count >= 2
            ), (
                "one unacknowledged delivery must not exhaust the configured redrive "
                "budget; queue_max_receive_count must allow at least one retry"
            )
            await production_queue.acknowledge(recovered.message_id)
        finally:
            await asyncio.to_thread(_purge_queue_quietly, sqs, "coldline-exception-jobs")
            await asyncio.to_thread(_purge_queue_quietly, sqs, "coldline-exception-jobs-dlq")
    finally:
        await asyncio.to_thread(_delete_queue_quietly, sqs, queue_name)
        await asyncio.to_thread(_delete_queue_quietly, sqs, dead_letter_name)
        await pool.execute("DELETE FROM exceptions WHERE exception_id = $1", exception_id)
        await pool.close()
    print(
        "Integration verification passed: PostgreSQL, SQS dead-letter recovery, and terminal "
        "failure contracts are valid."
    )


async def _poll_for_redelivery(
    queue: SqsJobQueue, expected: ExceptionJob, *, attempts: int = 20, delay_seconds: float = 0.5
) -> JobDelivery | None:
    """Wait for a message to become visible again, tolerating variable transport latency.

    LocalStack's own revisibility latency for a short visibility timeout has been observed
    to run measurably past the configured bound. A single sleep-then-check assumes an exact
    latency; repeatedly checking with a short pause between attempts absorbs the variance
    instead, up to a generous total budget (``attempts * delay_seconds`` seconds).
    """
    for _ in range(attempts):
        delivery = await queue.claim_stale(minimum_idle_ms=0)
        if delivery is not None and delivery.job == expected:
            return delivery
        await asyncio.sleep(delay_seconds)
    return None


def _delete_queue_quietly(sqs: object, name: str) -> None:
    """Remove one exercise queue, tolerating a name that was never created."""
    try:
        url = sqs.get_queue_url(QueueName=name)["QueueUrl"]  # type: ignore[attr-defined]
        sqs.delete_queue(QueueUrl=url)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - best-effort cleanup, never the test's own failure
        pass


def _purge_queue_quietly(sqs: object, name: str) -> None:
    """Empty one *shared, provisioned* queue without deleting it."""
    try:
        url = sqs.get_queue_url(QueueName=name)["QueueUrl"]  # type: ignore[attr-defined]
        sqs.purge_queue(QueueUrl=url)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - best-effort cleanup, never the test's own failure
        pass


if __name__ == "__main__":
    asyncio.run(verify())
