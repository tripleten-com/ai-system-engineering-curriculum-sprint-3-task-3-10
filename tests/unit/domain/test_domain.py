"""Coldline.

===================

File:              tests/unit/domain/test_domain.py
Component:         Unit tests — Test Domain
Purpose:           Unit tests for the Coldline exception domain.
Interacts With:    One isolated source responsibility
Sprint/Task:       Sprint 1 — Project 1
Concepts:          Fast feedback, failure paths, state invariants
Tools:             Python 3.12, pytest, Pydantic
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from domain import exception_id_for, requires_exception
from domain.contracts import ExceptionState, SensorReading
from domain.redaction import redact_sensitive_text


def reading(temperature_c: float = 9.2) -> SensorReading:
    """Return a fixed synthetic reading for domain tests."""
    return SensorReading(
        reading_id="reading-syn-001",
        shipment_id="shipment-syn-001",
        temperature_c=temperature_c,
        allowed_min_c=2.0,
        allowed_max_c=8.0,
        recorded_at=datetime(2026, 8, 28, tzinfo=UTC),
    )


def test_out_of_range_reading_requires_exception() -> None:
    """An upper-bound excursion must enter the exception path."""
    assert requires_exception(reading()) is True


def test_in_range_reading_does_not_require_exception() -> None:
    """A reading inside its handling range must not create exception work."""
    assert requires_exception(reading(5.0)) is False


def test_invalid_handling_range_is_rejected() -> None:
    """A reversed temperature range must fail before external work occurs."""
    with pytest.raises(ValidationError):
        SensorReading(
            reading_id="reading-syn-001",
            shipment_id="shipment-syn-001",
            temperature_c=5.0,
            allowed_min_c=8.0,
            allowed_max_c=2.0,
            recorded_at=datetime(2026, 8, 28, tzinfo=UTC),
        )


def test_exception_identity_is_stable_for_duplicate_reading() -> None:
    """Replaying one reading must address the same exception record."""
    assert exception_id_for(reading()) == "exc-e6a7451a-fe1a-53ca-b280-9bf67f555977"


def test_exception_states_are_the_published_state_machine() -> None:
    """The shared contract must expose only the documented lifecycle states."""
    assert [state.value for state in ExceptionState] == [
        "RECEIVED",
        "QUEUED",
        "PROCESSING",
        "COMPLETED",
        "FAILED",
    ]


def test_sensitive_text_is_redacted_before_logging_or_model_use() -> None:
    """Common identifying fields must not cross the supplied redaction boundary."""
    raw = "name=Ada Lovelace email=ada@example.test phone=+1 555 0100"
    assert redact_sensitive_text(raw) == "name=[REDACTED] email=[REDACTED] phone=[REDACTED]"


def test_single_token_name_does_not_consume_the_next_context_field() -> None:
    """A bounded name value must be removed without hiding unrelated evidence."""
    raw = "name=Ada zone=west status=delayed"
    assert redact_sensitive_text(raw) == "name=[REDACTED] zone=west status=delayed"


def test_multiline_name_and_phone_are_redacted_without_hiding_operations() -> None:
    """Newlines and semicolons must bound PII without consuming operational text."""
    raw = "name=Ada Lovelace\nphone=+1 555 0100; shipment delayed"
    assert redact_sensitive_text(raw) == ("name=[REDACTED]\nphone=[REDACTED]; shipment delayed")
