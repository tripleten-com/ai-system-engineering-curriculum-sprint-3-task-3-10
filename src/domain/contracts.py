"""Coldline.

===================

File:              src/domain/contracts.py
Component:         Domain — Contracts
Purpose:           Define provider-neutral contracts for exception processing and retrieval.
Interacts With:    API and worker use cases
Sprint/Task:       Sprint 2 — Project 2
Concepts:          Business rules, immutable contracts, state, retrieval evidence
Tools:             Python 3.12, Pydantic
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExceptionState(StrEnum):
    """Describe the observable lifecycle of one exception."""

    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SensorReading(BaseModel):
    """Represent one synthetic shipment sensor observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reading_id: str = Field(min_length=1, max_length=80)
    shipment_id: str = Field(min_length=1, max_length=80)
    temperature_c: float
    allowed_min_c: float
    allowed_max_c: float
    recorded_at: datetime
    context: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_range(self) -> "SensorReading":
        """Reject a handling range whose minimum is not below its maximum."""
        if self.allowed_min_c >= self.allowed_max_c:
            raise ValueError("allowed_min_c must be below allowed_max_c")
        return self


class ExceptionJob(BaseModel):
    """Carry one validated exception across the queue boundary."""

    model_config = ConfigDict(frozen=True)

    exception_id: str
    reading: SensorReading
    accepted_at: datetime


class JobDelivery(BaseModel):
    """Describe one provider-neutral queue delivery attempt."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    job: ExceptionJob
    delivery_count: int = Field(ge=1)
    trace_carrier: dict[str, str] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    """Describe the provider-neutral input for an exception summary."""

    model_config = ConfigDict(frozen=True)

    exception_id: str
    shipment_id: str
    temperature_c: float
    allowed_min_c: float
    allowed_max_c: float


class ModelSummary(BaseModel):
    """Describe the deterministic provider response stored by Coldline."""

    model_config = ConfigDict(frozen=True)

    summary: str
    provider: str


class ExceptionRecord(BaseModel):
    """Represent the durable state of one exception workflow."""

    model_config = ConfigDict(frozen=True)

    exception_id: str
    reading: SensorReading
    state: ExceptionState
    accepted_at: datetime
    updated_at: datetime
    summary: str | None = None
    failure_reason: str | None = None


class AccessTier(StrEnum):
    """Describe the two classification tiers carried by the supplied corpus."""

    STANDARD = "standard"
    RESTRICTED = "restricted"


class AccessLabel(BaseModel):
    """Carry the tenancy and classification labels attached to stored content.

    Every document and every chunk derived from it keeps the same label. The
    data layer, the retrieval adapter, and the diagnostic stage evidence all
    read this one representation so an access decision cannot be made from a
    second, divergent copy of the same fact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    access_tier: AccessTier


class Provenance(BaseModel):
    """Record where one supplied document came from and which revision it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_uri: str = Field(min_length=1, max_length=300)
    custodian: str = Field(min_length=1, max_length=120)
    revision: str = Field(min_length=1, max_length=40)
    recorded_at: datetime


class DocumentRecord(BaseModel):
    """Represent one whole supplied procedure, handling rule, or playbook."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    access: AccessLabel
    provenance: Provenance


class ChunkRecord(BaseModel):
    """Represent one deterministic slice of a document with its search fields.

    ``chunk_id`` is derived from the document identity and the chunk position,
    so re-ingesting the same corpus produces the same identifiers. The label
    and provenance are copied from the parent document rather than recomputed,
    which is what lets retrieval evidence name a chunk's origin and custody.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(min_length=1, max_length=140)
    document_id: str = Field(min_length=1, max_length=120)
    chunk_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    embedding: tuple[float, ...]
    access: AccessLabel
    provenance: Provenance

    @model_validator(mode="after")
    def validate_identity(self) -> "ChunkRecord":
        """Reject a chunk whose identifier does not match its document position."""
        expected = f"{self.document_id}#{self.chunk_index:04d}"
        if self.chunk_id != expected:
            raise ValueError(f"chunk_id must be {expected}")
        if not self.embedding:
            raise ValueError("embedding must not be empty")
        return self


class AuthorizationContext(BaseModel):
    """Carry the caller's tenancy and clearance through the retrieval request.

    The published ``Retriever`` contract accepts this context from Task 2.1
    onward. Whether the supplied adapter *enforces* it is a separate, recorded
    fact: see ``RetrievalResult.authorization_enforced``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    clearance: AccessTier


class RetrievalRequest(BaseModel):
    """Describe one hybrid retrieval request and its controlled parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=1000)
    authorization: AuthorizationContext
    top_k: int = Field(ge=1, le=50)
    dense_weight: float = Field(ge=0.0, le=1.0)
    # Ask the adapter for the extra authorization-pool evidence. Ordinary
    # request handling and the benchmark harness leave this False so no
    # diagnostic query is added to a measured path; the Task 2.8 stage
    # inspector sets it to True.
    explain: bool = False


class RetrievalStage(StrEnum):
    """Name the four observable stages of the supplied retrieval pipeline."""

    DENSE = "dense"
    SPARSE = "sparse"
    AUTHORIZATION = "authorization"
    FUSION = "fusion"


class Candidate(BaseModel):
    """Represent one ranked chunk produced by a retrieval stage.

    The chunk text travels with the candidate so that assembling prompt
    context needs no second read, and so that a citation can name the chunk's
    tenancy and custody without a join.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(min_length=1, max_length=140)
    document_id: str = Field(min_length=1, max_length=120)
    rank: int = Field(ge=1)
    score: float
    text: str = Field(min_length=1)
    access: AccessLabel
    provenance_revision: str = Field(min_length=1, max_length=40)


class StageEvidence(BaseModel):
    """Record what one stage admitted and what it removed.

    The identifiers are the evidence a Task 2.8 attribution rests on: a chunk
    listed in ``admitted`` reached that stage, and a chunk listed in
    ``dropped`` was removed by it. Both lists are ordered exactly as the stage
    produced them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: RetrievalStage
    admitted: tuple[str, ...]
    dropped: tuple[str, ...] = ()
    note: str = Field(default="", max_length=300)


class RetrievalResult(BaseModel):
    """Carry the final ranked chunks together with per-stage evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(min_length=1, max_length=120)
    results: tuple[Candidate, ...]
    stages: tuple[StageEvidence, ...]
    authorization_enforced: bool

    def stage(self, stage: RetrievalStage) -> StageEvidence:
        """Return the evidence recorded for one stage or fail loudly."""
        for evidence in self.stages:
            if evidence.stage is stage:
                return evidence
        raise KeyError(f"no evidence recorded for stage {stage.value}")


class AssembledContext(BaseModel):
    """Carry the prompt-ready context and citations built from retrieved chunks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(min_length=1, max_length=120)
    prompt_context: str
    citations: tuple[str, ...]
    token_budget: int = Field(ge=1)
    used_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_budget(self) -> "AssembledContext":
        """Reject an assembled context that exceeds its declared token budget."""
        if self.used_tokens > self.token_budget:
            raise ValueError("used_tokens must not exceed token_budget")
        return self
