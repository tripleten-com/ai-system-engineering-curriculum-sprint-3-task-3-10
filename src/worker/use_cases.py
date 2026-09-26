"""Coldline.

===================

File:              src/worker/use_cases.py
Component:         Worker — Use Cases
Purpose:           Coordinate one provider-neutral exception-processing attempt.
Interacts With:    LocalStack SQS, domain, ports, and adapters
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Background processing, retries, idempotency, failure classification
Tools:             Python 3.12
"""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from domain.contracts import ExceptionJob, ExceptionState, ModelRequest
from domain.errors import TerminalProviderError
from domain.repositories import ExceptionRepository
from ports import ModelProvider


class ProcessingDisposition(StrEnum):
    """Tell the transport loop whether to acknowledge or retry a delivery."""

    ACK = "ACK"
    ACK_EXISTING = "ACK_EXISTING"
    ACK_MISSING = "ACK_MISSING"
    RETRY = "RETRY"


class WorkerApplication:
    """Apply bounded model processing to one durable exception job."""

    def __init__(
        self,
        repository: ExceptionRepository,
        provider: ModelProvider,
        *,
        clock: Callable[[], datetime],
        maximum_attempts: int = 3,
    ) -> None:
        """Receive collaborators and a positive delivery-attempt limit."""
        if maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")
        self._repository = repository
        self._provider = provider
        self._clock = clock
        self._maximum_attempts = maximum_attempts

    async def process(self, job: ExceptionJob, *, delivery_count: int) -> ProcessingDisposition:
        """Process one delivery and tell the queue whether it can be acknowledged.

        Completed or failed identities are safe replays and need no new model
        call. In-flight work is retried until the third delivery. A successful
        provider result is persisted before ``ACK`` is returned. A provider
        failure the provider itself classified as terminal fails immediately,
        on the first delivery, without spending a redelivery on an outcome
        that cannot change. Any other provider failure returns ``RETRY``
        unless the delivery limit is exhausted. A missing record returns a
        distinct acknowledgement so the runtime can expose the broken
        persistence-before-publish invariant.
        """
        record = await self._repository.get(job.exception_id)
        if record is None:
            return ProcessingDisposition.ACK_MISSING
        if record.state in {ExceptionState.COMPLETED, ExceptionState.FAILED}:
            return ProcessingDisposition.ACK_EXISTING
        if record.state is ExceptionState.PROCESSING:
            if delivery_count >= self._maximum_attempts:
                await self._repository.transition(
                    job.exception_id,
                    {ExceptionState.PROCESSING},
                    ExceptionState.FAILED,
                    failure_reason="processing_attempts_exhausted",
                )
                return ProcessingDisposition.ACK
            return ProcessingDisposition.RETRY

        # PROCESSING is durable before the provider call starts. Recovery can
        # therefore distinguish work that never started from interrupted work.
        await self._repository.transition(
            job.exception_id,
            {ExceptionState.QUEUED},
            ExceptionState.PROCESSING,
        )

        try:
            summary = await self._provider.summarize(
                ModelRequest(
                    exception_id=job.exception_id,
                    shipment_id=job.reading.shipment_id,
                    temperature_c=job.reading.temperature_c,
                    allowed_min_c=job.reading.allowed_min_c,
                    allowed_max_c=job.reading.allowed_max_c,
                )
            )
        except TerminalProviderError:
            # The provider already decided a retry cannot succeed. Recording
            # the failure now, instead of after wasted redeliveries, is the
            # whole point of classifying it at the provider boundary.
            await self._repository.transition(
                job.exception_id,
                {ExceptionState.PROCESSING},
                ExceptionState.FAILED,
                failure_reason="model_provider_terminal_failure",
            )
            return ProcessingDisposition.ACK
        except Exception:
            if delivery_count >= self._maximum_attempts:
                await self._repository.transition(
                    job.exception_id,
                    {ExceptionState.PROCESSING},
                    ExceptionState.FAILED,
                    failure_reason="model_provider_exhausted",
                )
                return ProcessingDisposition.ACK
            await self._repository.transition(
                job.exception_id,
                {ExceptionState.PROCESSING},
                ExceptionState.QUEUED,
            )
            return ProcessingDisposition.RETRY

        # Terminal persistence happens before the transport loop acknowledges.
        await self._repository.transition(
            job.exception_id,
            {ExceptionState.PROCESSING},
            ExceptionState.COMPLETED,
            summary=summary.summary,
        )
        return ProcessingDisposition.ACK
