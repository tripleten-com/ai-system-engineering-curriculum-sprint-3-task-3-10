"""Coldline.

===================

File:              src/api/routes.py
Component:         API — Routes
Purpose:           Expose the Coldline exception and retrieval applications through FastAPI.
Interacts With:    FastAPI, domain, ports, adapters, and the Task 3.10 status read-through
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          HTTP boundary, composition, asynchronous work, read-through cache
Tools:             Python 3.12, FastAPI, OpenTelemetry, Prometheus, Pydantic

Task 3.10 changes one route here and nothing else: ``GET /api/v1/exceptions/{exception_id}``
now answers through ``read_exception_status`` (src/api/status_cache.py), which asks the
status cache before the store, and adds two response headers naming the outcome (hit or
miss) and the age of the copy it served. The route, the headers, and the composition of
the cache are supplied; the read-through's body is the Task's Step 1.
"""

from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response, status
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Lifespan

from adapters.cache import InMemoryStatusCache
from api.document_service import DocumentService, DocumentsUnavailable
from api.experiment import TOP_K_LIMIT, ExperimentRetrieval
from api.retrieval_workflow import RetrievalWorkflow
from api.status_cache import CACHE_AGE_HEADER, CACHE_OUTCOME_HEADER, read_exception_status
from api.use_cases import (
    InRangeReading,
    QueueUnavailable,
    ReadingApplication,
    TerminalExceptionConflict,
)
from domain.contracts import (
    AccessTier,
    AuthorizationContext,
    Candidate,
    ChunkRecord,
    DocumentRecord,
    ExceptionRecord,
    SensorReading,
    StageEvidence,
)
from domain.failures import ObjectStoreUnavailable, RetrievalUnavailable
from domain.repositories import ExceptionRepository, RepositoryError
from domain.status_cache import StatusCache

_REQUESTS = Counter(
    "coldline_api_requests_total",
    "Count API requests by bounded route, method, and status.",
    ("route", "method", "status"),
)
_REQUEST_DURATION = Histogram(
    "coldline_api_request_duration_seconds",
    "Measure API request duration in seconds.",
    ("route", "method"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

_KNOWN_ROUTES = frozenset(
    {
        "/api/v1/readings",
        "/api/v1/retrieval/search",
        "/api/v1/corpus/objects",
        "/api/v1/experiments/retrieval",
        "/api/v1/documents",
        "/api/v2/documents",
        "/api/v2/readings",
        "/health/live",
        "/health/ready",
        "/version",
        "/metrics",
    }
)


class AcceptedReading(BaseModel):
    """Return the stable identity and polling location for accepted work."""

    exception_id: str
    state: str
    status_url: str


class SearchRequest(BaseModel):
    """Describe one procedural retrieval request submitted over HTTP.

    The caller states its own tenancy and clearance. This local system has no
    authentication, so the context is an *asserted* identity, not a verified
    one; ``docs/fidelity/Retriever.md`` records that limit.
    """

    model_config = ConfigDict(extra="forbid")

    query_id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=1000)
    authorization: AuthorizationContext
    explain: bool = False


class ExperimentRequest(BaseModel):
    """Describe one measurement of one stated retrieval configuration.

    The two parameters are required and bounded here rather than defaulted.
    An evaluation request that silently fell back to the deployed defaults
    would report a measurement of the wrong configuration, which is worse than
    a rejected request.
    """

    model_config = ConfigDict(extra="forbid")

    query_id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=1000)
    authorization: AuthorizationContext
    top_k: int = Field(ge=1, le=TOP_K_LIMIT)
    fusion_weight: float = Field(ge=0.0, le=1.0)


class ExperimentResponse(BaseModel):
    """Return the fused ranking, its stage evidence, and the applied parameters."""

    query_id: str
    results: tuple[Candidate, ...]
    stages: tuple[StageEvidence, ...]
    authorization_enforced: bool
    top_k: int
    fusion_weight: float


class SearchResponse(BaseModel):
    """Return the fused ranking, the stage evidence, and the prompt context."""

    query_id: str
    results: tuple[Candidate, ...]
    stages: tuple[StageEvidence, ...]
    authorization_enforced: bool
    prompt_context: str
    citations: tuple[str, ...]
    token_budget: int
    used_tokens: int


class CorpusObjects(BaseModel):
    """Return the object keys visible through the published ObjectStore port."""

    prefix: str
    keys: tuple[str, ...]


class StoredDocument(BaseModel):
    """Return one persisted document and the chunk identifiers derived from it."""

    document: DocumentRecord
    chunk_ids: tuple[str, ...]


class DocumentListing(BaseModel):
    """Return every document one caller may read."""

    documents: tuple[DocumentRecord, ...]


class ChunkListing(BaseModel):
    """Return the readable chunks of one document."""

    document_id: str
    chunks: tuple[ChunkRecord, ...]


async def _ready_by_default() -> bool:
    """Return readiness for dependency-free HTTP contract tests."""
    return True


def create_app(
    application: ReadingApplication,
    repository: ExceptionRepository,
    retrieval: RetrievalWorkflow,
    objects: Callable[[str], Awaitable[list[str]]],
    documents: DocumentService,
    *,
    experiment: ExperimentRetrieval | None = None,
    version_two: APIRouter | None = None,
    readiness: Callable[[], Awaitable[bool]] = _ready_by_default,
    build_version: str = "dev",
    lifespan: Lifespan[FastAPI] | None = None,
    status_cache: StatusCache | None = None,
) -> FastAPI:
    """Create the HTTP delivery layer around provider-neutral application behavior.

    ``status_cache`` is the Task 3.10 cache the status route reads through. The
    composition root passes the Redis adapter; the dependency-free HTTP contract
    tests pass nothing and get an in-process copy that behaves the same way.
    """
    app = FastAPI(title="Coldline API", version=build_version, lifespan=lifespan)
    cache: StatusCache = status_cache if status_cache is not None else InMemoryStatusCache()

    @app.middleware("http")
    async def record_request(
        request: Request, call_next: Callable[..., Awaitable[Response]]
    ) -> Response:
        """Record bounded request count and duration labels."""
        started = perf_counter()
        route = _route_label(request.url.path)
        response_status = "500"
        try:
            response = await call_next(request)
            response_status = str(response.status_code)
            return response
        finally:
            # Unhandled server errors are re-raised by FastAPI. The finally block
            # still records their bounded 500 signal before error handling returns.
            _REQUESTS.labels(route, request.method, response_status).inc()
            _REQUEST_DURATION.labels(route, request.method).observe(perf_counter() - started)

    @app.get("/health/live", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        """Report that the API process can serve requests."""
        return {"status": "alive"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(response: Response) -> dict[str, str]:
        """Report whether every required API dependency is available."""
        if not await readiness():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready"}
        return {"status": "ready"}

    @app.get("/version", include_in_schema=False)
    async def version() -> dict[str, str | bool]:
        """Report which build is answering, and whether it is ready to serve."""
        return {
            "service": "coldline-api",
            "build_version": build_version,
            "ready": await readiness(),
        }

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        """Expose Prometheus metrics for the API service."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post(
        "/api/v1/readings",
        response_model=AcceptedReading,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def accept_reading(reading: SensorReading) -> AcceptedReading:
        """Accept one synthetic exception reading for background processing."""
        try:
            record = await application.accept(reading)
        except InRangeReading as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except QueueUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TerminalExceptionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        trace.get_current_span().set_attribute("coldline.exception_id", record.exception_id)
        return AcceptedReading(
            exception_id=record.exception_id,
            state=record.state.value,
            status_url=f"/api/v1/exceptions/{record.exception_id}",
        )

    @app.get("/api/v1/exceptions/{exception_id}", response_model=ExceptionRecord)
    async def get_exception(exception_id: str, response: Response) -> ExceptionRecord:
        """Return the state of one exception workflow, through the status cache.

        Task 3.10: the read goes through ``read_exception_status``, which asks the
        cache before the store. The two headers say where this answer came from
        (``hit`` or ``miss``) and how old the copy was; ``poe cache-probe`` reads them.
        """
        read = await read_exception_status(exception_id, cache, repository)
        if read is None:
            raise HTTPException(status_code=404, detail="exception not found")
        response.headers[CACHE_OUTCOME_HEADER] = read.outcome.value
        response.headers[CACHE_AGE_HEADER] = f"{read.age_seconds:.3f}"
        return read.record

    if experiment is not None:

        @app.post("/api/v1/experiments/retrieval", response_model=ExperimentResponse)
        async def measure_retrieval(request: ExperimentRequest) -> ExperimentResponse:
            """Return the fused ranking one stated retrieval configuration produces."""
            try:
                result = await experiment.measure(
                    request.query_id,
                    request.text,
                    request.authorization,
                    top_k=request.top_k,
                    fusion_weight=request.fusion_weight,
                )
            except RetrievalUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            return ExperimentResponse(
                query_id=result.query_id,
                results=result.results,
                stages=result.stages,
                authorization_enforced=result.authorization_enforced,
                top_k=request.top_k,
                fusion_weight=request.fusion_weight,
            )

    @app.post("/api/v1/retrieval/search", response_model=SearchResponse)
    async def search(request: SearchRequest) -> SearchResponse:
        """Retrieve ranked procedure chunks and the assembled prompt context."""
        try:
            outcome = await retrieval.answer(
                request.query_id,
                request.text,
                request.authorization,
                explain=request.explain,
            )
        except RetrievalUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        trace.get_current_span().set_attribute("coldline.query_id", request.query_id)
        return SearchResponse(
            query_id=outcome.result.query_id,
            results=outcome.result.results,
            stages=outcome.result.stages,
            authorization_enforced=outcome.result.authorization_enforced,
            prompt_context=outcome.context.prompt_context,
            citations=outcome.context.citations,
            token_budget=outcome.context.token_budget,
            used_tokens=outcome.context.used_tokens,
        )

    def _scope(tenant_id: str, clearance: AccessTier) -> AuthorizationContext:
        """Build the caller scope this local system takes on trust."""
        return AuthorizationContext(tenant_id=tenant_id, clearance=clearance)

    @app.post(
        "/api/v1/documents",
        response_model=StoredDocument,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_document(document: DocumentRecord) -> StoredDocument:
        """Persist one document and its deterministic chunks atomically."""
        try:
            chunks = await documents.create(document)
        except DocumentsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RepositoryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StoredDocument(
            document=document, chunk_ids=tuple(chunk.chunk_id for chunk in chunks)
        )

    @app.get("/api/v1/documents", response_model=DocumentListing)
    async def list_documents(
        tenant_id: str = Query(min_length=1, max_length=80),  # noqa: B008 - FastAPI idiom
        clearance: AccessTier = Query(default=AccessTier.STANDARD),  # noqa: B008 - FastAPI idiom
    ) -> DocumentListing:
        """List the documents one caller scope may read."""
        try:
            stored = await documents.list_all(scope=_scope(tenant_id, clearance))
        except DocumentsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RepositoryError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return DocumentListing(documents=tuple(stored))

    @app.get("/api/v1/documents/{document_id}", response_model=DocumentRecord)
    async def get_document(
        document_id: str,
        tenant_id: str = Query(min_length=1, max_length=80),  # noqa: B008 - FastAPI idiom
        clearance: AccessTier = Query(default=AccessTier.STANDARD),  # noqa: B008 - FastAPI idiom
    ) -> DocumentRecord:
        """Return one document the caller scope may read."""
        try:
            stored = await documents.get(document_id, scope=_scope(tenant_id, clearance))
        except DocumentsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RepositoryError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if stored is None:
            # An out-of-scope document is indistinguishable from an absent one.
            raise HTTPException(status_code=404, detail="document not found")
        return stored

    @app.get("/api/v1/documents/{document_id}/chunks", response_model=ChunkListing)
    async def get_document_chunks(
        document_id: str,
        tenant_id: str = Query(min_length=1, max_length=80),  # noqa: B008 - FastAPI idiom
        clearance: AccessTier = Query(default=AccessTier.STANDARD),  # noqa: B008 - FastAPI idiom
    ) -> ChunkListing:
        """Return the readable chunks of one document."""
        try:
            stored = await documents.chunks(document_id, scope=_scope(tenant_id, clearance))
        except DocumentsUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RepositoryError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return ChunkListing(document_id=document_id, chunks=tuple(stored))

    @app.get("/api/v1/corpus/objects", response_model=CorpusObjects)
    async def corpus_objects(
        prefix: str = Query(default="corpus/", min_length=1, max_length=200),
    ) -> CorpusObjects:
        """List stored corpus artifacts through the published ObjectStore port."""
        try:
            keys = await objects(prefix)
        except ObjectStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return CorpusObjects(prefix=prefix, keys=tuple(keys))

    if version_two is not None:
        # Mounted last so a version 2 path can never shadow a version 1 route.
        # Nothing is mounted when the wiring factory returns None, which is why
        # a fresh starter publishes no /api/v2/ path at all.
        app.include_router(version_two)

    return app


def _route_label(path: str) -> str:
    """Normalize request paths to bounded metric dimensions."""
    if path.startswith("/api/v1/exceptions/"):
        return "/api/v1/exceptions/{exception_id}"
    if path.startswith("/api/v1/documents/"):
        return (
            "/api/v1/documents/{document_id}/chunks"
            if path.endswith("/chunks")
            else "/api/v1/documents/{document_id}"
        )
    if path in _KNOWN_ROUTES:
        return path
    return "unmatched"
