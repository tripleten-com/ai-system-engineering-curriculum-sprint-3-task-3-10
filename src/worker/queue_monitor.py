"""Coldline.

===================

File:              src/worker/queue_monitor.py
Component:         Worker — Dead-letter queue monitor
Purpose:           Poll the dead-letter queue's own depth for the SLO alert.
Interacts With:    LocalStack SQS
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Background processing, SLO/alert, dead-letter redrive
Tools:             Python 3.12, boto3, Prometheus
"""

import asyncio
import logging
from typing import Any

from worker.metrics import DEAD_LETTER_DEPTH

_LOGGER = logging.getLogger(__name__)


async def poll_dead_letter_depth(
    client: Any,
    dead_letter_queue_url: str,
    *,
    interval_seconds: float = 2.0,
) -> None:
    """Report the dead-letter queue's approximate depth forever.

    Runs beside the worker's own processing loop, not inside it, and is
    started and cancelled as its own task by the composition root. The two
    dead-letter failure exercises stop and restart the *processing* loop to
    force and recover a dead-lettered message; this poller has to keep
    reporting through that window, because `ColdlineDeadLetterQueueBacklog`
    (`infra/observability/alerts.yml`) needs a sustained non-zero reading to
    evaluate against, not an absent series.

    `SqsJobQueue.queue_depth`/`pending_count` are bound to the *main* queue
    only (`src/adapters/queue/sqs.py`); this reads the separate dead-letter
    queue's own attribute directly, the same technique
    `tests/failure/force_dlq_arrival.py` already uses to read it back.
    """
    while True:
        try:
            depth = await asyncio.to_thread(_approximate_depth, client, dead_letter_queue_url)
            DEAD_LETTER_DEPTH.set(depth)
        except Exception:  # noqa: BLE001 - a transient read must never kill the poller
            _LOGGER.exception("dead-letter queue depth poll failed")
        await asyncio.sleep(interval_seconds)


def _approximate_depth(client: Any, queue_url: str) -> int:
    """Read one dead-letter queue's approximate depth from the live service."""
    response = client.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessages"]
    )
    return int(response["Attributes"]["ApproximateNumberOfMessages"])
