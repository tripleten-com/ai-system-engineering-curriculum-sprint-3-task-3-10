"""Coldline.

===================

File:              tests/e2e/test_exception_workflow.py
Component:         End-to-end tests — Exception workflow
Purpose:           Proves the external API-to-worker behavior and durable duplicate identity.
Interacts With:    API, worker, PostgreSQL, Redis Streams, and Jaeger
Sprint/Task:       Sprint 1 — Project 1
Concepts:          Asynchronous completion, idempotency, observable evidence
Tools:             Python 3.12, pytest, httpx
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest

from tests.runtime_config import host_port

TASK_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.runtime


def _load_unique_reading() -> tuple[str, dict[str, Any]]:
    """Return the supplied synthetic reading with a unique test identity."""
    fixture = json.loads(
        (TASK_ROOT / "tests/e2e/baseline-exception.json").read_text(encoding="utf-8")
    )
    reading = cast(dict[str, Any], fixture["reading"])
    suffix = uuid4().hex
    reading["reading_id"] = f"reading-e2e-{suffix}"
    reading["shipment_id"] = f"shipment-e2e-{suffix}"
    reading["recorded_at"] = datetime.now(UTC).isoformat()
    return cast(str, fixture["scenario_id"]), reading


def _wait_for_terminal(client: httpx.Client, status_url: str) -> dict[str, Any]:
    """Poll the public status resource until the workflow becomes terminal."""
    for _ in range(40):
        response = client.get(status_url)
        response.raise_for_status()
        record = cast(dict[str, Any], response.json())
        if record["state"] == "COMPLETED":
            return record
        if record["state"] == "FAILED":
            pytest.fail("the supplied workflow reached FAILED")
        time.sleep(0.5)
    pytest.fail("the supplied workflow did not complete within 20 seconds")


def _wait_for_service_evidence(service: str, exception_id: str) -> list[dict[str, Any]]:
    """Return Jaeger evidence for one service and durable exception identity."""
    jaeger_port = host_port("COLDLINE_JAEGER_HOST_PORT", 16686)
    tags = quote(json.dumps({"coldline.exception_id": exception_id}, separators=(",", ":")))
    endpoint = f"http://localhost:{jaeger_port}/api/traces?service={service}&tags={tags}"
    for _ in range(30):
        response = httpx.get(endpoint, timeout=5.0)
        response.raise_for_status()
        traces = cast(list[dict[str, Any]], response.json().get("data", []))
        if traces:
            return traces
        time.sleep(0.5)
    return []


def test_exception_workflow_completes_with_stable_duplicate_identity() -> None:
    """Catch lost work, unstable idempotency, or missing API and worker evidence."""
    scenario_id, reading = _load_unique_reading()
    api_port = host_port("COLDLINE_API_HOST_PORT", 8000)
    with httpx.Client(base_url=f"http://localhost:{api_port}", timeout=5.0) as client:
        accepted = client.post("/api/v1/readings", json=reading)
        assert accepted.status_code == 202
        first = cast(dict[str, Any], accepted.json())
        record = _wait_for_terminal(client, cast(str, first["status_url"]))

        duplicate = client.post("/api/v1/readings", json=reading)
        assert duplicate.status_code == 202
        replay = cast(dict[str, Any], duplicate.json())

        status = client.get(cast(str, first["status_url"]))
        status.raise_for_status()

    assert scenario_id == "sprint1-baseline-exception"
    assert record["state"] == "COMPLETED"
    assert replay["exception_id"] == first["exception_id"]
    assert replay["status_url"] == first["status_url"]
    assert status.json()["exception_id"] == first["exception_id"]
    assert _wait_for_service_evidence("coldline-api", cast(str, first["exception_id"]))
    assert _wait_for_service_evidence("coldline-worker", cast(str, first["exception_id"]))
