"""Coldline.

===================

File:              src/worker/bootstrap.py
Component:         Worker — Bootstrap
Purpose:           Compose and run the SQS-backed Coldline worker.
Interacts With:    LocalStack SQS, Redis, domain, ports, and adapters
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Background processing, idempotency, bounded resilience, cache invalidation
Tools:             Python 3.12, PostgreSQL, boto3, Redis, OpenTelemetry, Prometheus
"""

import asyncio
import logging
import signal
from datetime import UTC, datetime

import asyncpg
from opentelemetry import trace
from prometheus_client import start_http_server

from adapters.cache import RedisStatusCache, create_redis_client
from adapters.logging import configure_json_logging
from adapters.model import DeterministicModelProvider, ResilientModelProvider
from adapters.persistence import PostgresExceptionRepository
from adapters.queue import SqsJobQueue, create_sqs_client
from adapters.telemetry import configure_tracing
from worker.config import WorkerSettings
from worker.queue_monitor import poll_dead_letter_depth
from worker.runtime import run_loop
from worker.use_cases import WorkerApplication


async def run() -> None:
    """Compose the worker, run it, and release every owned resource.

    This is wiring only. Domain decisions stay in ``WorkerApplication`` and
    provider and transport behavior stay behind their ports. Signal handlers
    cancel both background tasks together, then the ``finally`` block closes
    PostgreSQL, the status cache's Redis client, and the trace provider.
    """
    settings = WorkerSettings()  # type: ignore[call-arg]  # protected environment is the source
    configure_json_logging(settings.service_name)
    logging.getLogger(__name__).info("worker starting build_version=%s", settings.build_version)
    tracer_provider = configure_tracing(settings.service_name, settings.otel_endpoint)
    tracer = trace.get_tracer(__name__)
    pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=4)
    sqs_client = create_sqs_client(
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )
    queue_url = await asyncio.to_thread(
        lambda: sqs_client.get_queue_url(QueueName=settings.queue_name)["QueueUrl"]
    )
    # Task 3.4's own resolution, alongside the main queue's: the dead-letter
    # monitor polls this queue directly, never through JobQueue, since
    # SqsJobQueue is bound to the main queue only.
    dead_letter_queue_url = await asyncio.to_thread(
        lambda: sqs_client.get_queue_url(QueueName=settings.dead_letter_name)["QueueUrl"]
    )
    queue = SqsJobQueue(sqs_client, queue_url=queue_url)
    # Task 3.10: the same Redis role the API caches status copies in. The worker only
    # ever deletes from it, through the loop's invalidation call after a terminal write.
    status_cache = RedisStatusCache(create_redis_client(settings.redis_url))
    start_http_server(settings.metrics_port)
    provider = ResilientModelProvider(
        DeterministicModelProvider(latency_ms=settings.model_latency_ms),
        timeout_seconds=settings.model_timeout_ms / 1000,
        max_attempts=settings.model_provider_max_attempts,
        backoff_seconds=settings.model_retry_backoff_ms / 1000,
    )
    application = WorkerApplication(
        PostgresExceptionRepository(pool),
        provider,
        clock=lambda: datetime.now(UTC),
        maximum_attempts=settings.maximum_attempts,
    )
    worker_task = asyncio.create_task(
        run_loop(
            queue,
            application,
            tracer,
            stale_message_ms=settings.stale_message_ms,
            status_cache=status_cache,
        )
    )
    monitor_task = asyncio.create_task(poll_dead_letter_depth(sqs_client, dead_letter_queue_url))

    def cancel_both() -> None:
        worker_task.cancel()
        monitor_task.cancel()

    loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(shutdown_signal, cancel_both)
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        await status_cache.aclose()
        await pool.close()
        tracer_provider.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
