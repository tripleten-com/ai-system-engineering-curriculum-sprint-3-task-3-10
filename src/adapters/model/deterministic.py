"""Coldline.

===================

File:              src/adapters/model/deterministic.py
Component:         Adapter — Deterministic
Purpose:           Implement the deterministic local model-provider adapter.
Interacts With:    Domain contracts, ports, and local providers
Sprint/Task:       Sprint 1 — Project 1
Concepts:          Boundary translation, deterministic infrastructure
Tools:             Python 3.12, OpenTelemetry
"""

import asyncio

from opentelemetry import trace

from domain.contracts import ModelRequest, ModelSummary

_TRACER = trace.get_tracer(__name__)


class DeterministicModelProvider:
    """Return repeatable summaries without network or paid-model calls."""

    def __init__(self, *, latency_ms: int = 250) -> None:
        """Configure a fixed non-negative provider delay in milliseconds."""
        if latency_ms < 0:
            raise ValueError("latency_ms must not be negative")
        self._latency_seconds = latency_ms / 1000

    async def summarize(self, request: ModelRequest) -> ModelSummary:
        """Return a bounded summary for one synthetic temperature excursion."""
        with _TRACER.start_as_current_span(
            "model_provider.summarize",
            attributes={"coldline.exception_id": request.exception_id},
        ):
            if self._latency_seconds:
                await asyncio.sleep(self._latency_seconds)

            if request.temperature_c > request.allowed_max_c:
                magnitude = request.temperature_c - request.allowed_max_c
                condition = f"exceeded the upper handling bound by {magnitude:.1f} C"
            else:
                magnitude = request.allowed_min_c - request.temperature_c
                condition = f"fell below the lower handling bound by {magnitude:.1f} C"

            return ModelSummary(
                provider="deterministic-local",
                summary=(
                    f"Synthetic shipment {request.shipment_id} {condition}; "
                    "operational review is required."
                ),
            )
