"""Coldline.

===================

File:              src/api/bootstrap.py
Component:         API — Bootstrap
Purpose:           Compose and run the Coldline API service.
Interacts With:    FastAPI, domain, ports, adapters, Redis (the Task 3.10 status cache)
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          HTTP boundary, composition, asynchronous work, read-through cache
Tools:             Python 3.12, FastAPI, PostgreSQL, pgvector, boto3, Redis, OpenTelemetry
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import asyncpg
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from adapters.cache import RedisStatusCache, create_redis_client
from adapters.logging import configure_json_logging
from adapters.object_store import S3ObjectStore, create_s3_client
from adapters.persistence import PostgresExceptionRepository, PostgresIdempotencyStore
from adapters.queue import SqsJobQueue, create_sqs_client
from adapters.retriever import PostgresHybridRetriever
from adapters.telemetry import configure_tracing
from api.config import load_api_settings
from api.document_service import DocumentService
from api.experiment import ExperimentRetrieval
from api.extensions import wiring
from api.retrieval_workflow import CITATION_LIMIT, RetrievalWorkflow
from api.routes import create_app
from api.runtime import RuntimeBindings
from api.status_cache import status_ttl_seconds
from api.use_cases import ReadingApplication

settings = load_api_settings()
configure_json_logging(settings.service_name)
tracer_provider = configure_tracing(settings.service_name, settings.otel_endpoint)
bindings = RuntimeBindings()
application = ReadingApplication(bindings, bindings, clock=lambda: datetime.now(UTC))

# Task 3.10: the status cache, in the Redis role the stack already runs. The TTL is read
# and validated here, at start-up, so a config/cache.yaml outside the published range
# stops the container from serving instead of surfacing on the first status read.
status_cache = RedisStatusCache(create_redis_client(settings.redis_url))
logging.getLogger(__name__).info(
    "status cache configured status_ttl_seconds=%s", status_ttl_seconds()
)

# The composed access policy comes from the student wiring factory. Composing it
# here, rather than defaulting it inside the adapter, is what makes the change
# of enforcement a visible one-line composition change.
access_constraints = wiring.build_access_constraints()

# The two service factories are the student-editable seam. Each returns None
# until an implementation is wired, and the workflow keeps its coupled code for
# whichever half is still None. Composing them here, rather than inside the
# workflow, is what makes a substitution observable from outside.
retrieval = RetrievalWorkflow(
    bindings,
    top_k=settings.retrieval_top_k,
    dense_weight=settings.retrieval_dense_weight,
    token_budget=settings.retrieval_token_budget,
    retrieval_orchestrator=wiring.build_retrieval_orchestrator(
        bindings,
        top_k=settings.retrieval_top_k,
        dense_weight=settings.retrieval_dense_weight,
        citation_limit=CITATION_LIMIT,
    ),
)
documents = DocumentService(bindings.documents)
# The evaluation surface measures the same retrieval port the product
# endpoint uses, with the parameters stated per request instead of composed.
experiment = ExperimentRetrieval(bindings)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Own API dependency startup and shutdown.

    PostgreSQL and the object-storage and queue clients are created once per
    process. The ``finally`` block closes the pool, the status cache's Redis
    client, and flushes tracing even when startup work or a request fails.
    """
    bindings.mark_serving(settings.ready_delay_seconds)
    bindings.pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=4)
    bindings.repository = PostgresExceptionRepository(bindings.pool)
    sqs_client = create_sqs_client(
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )
    queue_url = await asyncio.to_thread(
        lambda: sqs_client.get_queue_url(QueueName=settings.queue_name)["QueueUrl"]
    )
    bindings.queue = SqsJobQueue(sqs_client, queue_url=queue_url)
    bindings.object_store = S3ObjectStore(
        create_s3_client(
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        ),
        bucket=settings.s3_bucket,
    )
    bindings.retriever = PostgresHybridRetriever(
        bindings.pool,
        access_constraints=access_constraints,
    )
    # The student factory decides whether the document API is available. It is
    # given the one process pool rather than being allowed to make its own.
    bindings.document_repository = wiring.build_document_repository(bindings.pool)
    bindings.idempotency_store = PostgresIdempotencyStore(bindings.pool)
    try:
        yield
    finally:
        await status_cache.aclose()
        await bindings.pool.close()
        tracer_provider.shutdown()


# The version 2 router is composed from the student wiring factory. FastAPI
# needs the application object before the lifespan runs, so the idempotency
# store is a forwarding binding rather than a client created here.
app = create_app(
    application,
    bindings,
    retrieval,
    bindings.list_corpus_keys,
    documents,
    experiment=experiment,
    version_two=wiring.build_v2_router(
        documents=documents,
        readings=application,
        store=bindings,
    ),
    readiness=bindings.ready,
    build_version=settings.build_version,
    lifespan=lifespan,
    status_cache=status_cache,
)
FastAPIInstrumentor.instrument_app(app)
