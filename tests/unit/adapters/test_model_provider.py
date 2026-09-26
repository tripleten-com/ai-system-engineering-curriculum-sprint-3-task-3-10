"""Coldline.

===================

File:              tests/unit/adapters/test_model_provider.py
Component:         Unit tests — Test Model Provider
Purpose:           Unit tests for the deterministic model provider.
Interacts With:    One isolated source responsibility
Sprint/Task:       Sprint 1 — Project 1
Concepts:          Fast feedback, failure paths, state invariants
Tools:             Python 3.12, pytest
"""

import pytest

from adapters.model import DeterministicModelProvider
from domain.contracts import ModelRequest


@pytest.mark.asyncio
async def test_provider_returns_fixed_bounded_exception_summary() -> None:
    """A fixed request must produce a stable provider-neutral response."""
    provider = DeterministicModelProvider(latency_ms=0)
    response = await provider.summarize(
        ModelRequest(
            exception_id="exc-001",
            shipment_id="shipment-syn-001",
            temperature_c=9.2,
            allowed_min_c=2.0,
            allowed_max_c=8.0,
        )
    )

    assert response.provider == "deterministic-local"
    assert response.summary == (
        "Synthetic shipment shipment-syn-001 exceeded the upper handling bound "
        "by 1.2 C; operational review is required."
    )


@pytest.mark.asyncio
async def test_provider_handles_lower_bound_excursion() -> None:
    """A lower excursion must report the correct direction and magnitude."""
    provider = DeterministicModelProvider(latency_ms=0)
    response = await provider.summarize(
        ModelRequest(
            exception_id="exc-002",
            shipment_id="shipment-syn-002",
            temperature_c=1.5,
            allowed_min_c=2.0,
            allowed_max_c=8.0,
        )
    )

    assert "fell below the lower handling bound by 0.5 C" in response.summary
