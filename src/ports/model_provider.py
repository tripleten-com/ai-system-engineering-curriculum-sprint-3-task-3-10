"""Coldline.

===================

File:              src/ports/model_provider.py
Component:         Port — Model Provider
Purpose:           Define the provider-neutral model-execution port.
Interacts With:    Use cases and provider adapters
Sprint/Task:       Sprint 1 — Project 1
Concepts:          Dependency inversion, provider-neutral interface
Tools:             Python 3.12
"""

from typing import Protocol, runtime_checkable

from domain.contracts import ModelRequest, ModelSummary


@runtime_checkable
class ModelProvider(Protocol):
    """Generate one bounded exception summary."""

    async def summarize(self, request: ModelRequest) -> ModelSummary:
        """Return a provider-neutral exception summary."""
        ...
