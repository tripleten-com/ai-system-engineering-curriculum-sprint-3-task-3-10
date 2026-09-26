"""Coldline.

===================

File:              tests/e2e/scenario.py
Component:         End-to-end tests — Scenario
Purpose:           Run the supplied baseline exception scenario.
Interacts With:    External API, worker, storage, and telemetry
Sprint/Task:       Sprint 1 — Project 1
Concepts:          Black-box workflow, durable identity, evidence
Tools:             Python 3.12, pytest
"""

import json
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import httpx

from tests.runtime_config import host_port

TASK_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    """Run one exception and print its bounded runtime references."""
    fixture = json.loads(
        (TASK_ROOT / "tests/e2e/baseline-exception.json").read_text(encoding="utf-8")
    )
    api_port = host_port("COLDLINE_API_HOST_PORT", 8000)
    with httpx.Client(base_url=f"http://localhost:{api_port}", timeout=5.0) as client:
        accepted = client.post("/api/v1/readings", json=fixture["reading"])
        accepted.raise_for_status()
        accepted_body = accepted.json()
        record = _wait_for_completion(client, accepted_body["status_url"])
        exception_id = cast(str, record["exception_id"])
        api_trace_id = _wait_for_traces(client, "coldline-api", exception_id)
        worker_trace_id = _wait_for_traces(client, "coldline-worker", exception_id)
    print(
        json.dumps(
            {
                "scenario_id": fixture["scenario_id"],
                "exception_id": record["exception_id"],
                "state": record["state"],
                "api_trace_id": api_trace_id,
                "worker_trace_id": worker_trace_id,
                "status_url": accepted_body["status_url"],
                "jaeger_url": (f"http://localhost:{host_port('COLDLINE_JAEGER_HOST_PORT', 16686)}"),
                "grafana_url": (
                    f"http://localhost:{host_port('COLDLINE_GRAFANA_HOST_PORT', 3000)}"
                    "/d/coldline-task-1-1"
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _wait_for_completion(client: httpx.Client, status_url: str) -> dict[str, object]:
    """Poll the durable record until it reaches a terminal state."""
    for _ in range(30):
        response = client.get(status_url)
        response.raise_for_status()
        record = response.json()
        if record["state"] == "COMPLETED":
            return cast(dict[str, object], record)
        if record["state"] == "FAILED":
            raise RuntimeError("published scenario reached FAILED; run `poe reset` and retry")
        time.sleep(0.5)
    raise RuntimeError("published scenario did not complete within 15 seconds")


def _wait_for_traces(client: httpx.Client, service: str, exception_id: str) -> str:
    """Return the most recent trace identity correlated to one exception when exported."""
    tags = quote(json.dumps({"coldline.exception_id": exception_id}, separators=(",", ":")))
    jaeger_port = host_port("COLDLINE_JAEGER_HOST_PORT", 16686)
    endpoint = f"http://localhost:{jaeger_port}/api/traces?service={service}&tags={tags}"
    for _ in range(20):
        response = client.get(endpoint)
        response.raise_for_status()
        traces = cast(list[dict[str, Any]], response.json().get("data", []))
        candidates: list[tuple[int, str]] = []
        for trace in traces:
            trace_id = trace.get("traceID")
            spans = trace.get("spans")
            if not isinstance(trace_id, str) or not isinstance(spans, list):
                continue
            start_times = [
                start_time
                for span in spans
                if isinstance(span, dict)
                if isinstance(start_time := span.get("startTime"), int)
            ]
            candidates.append((max(start_times, default=0), trace_id))
        if candidates:
            return max(candidates)[1]
        time.sleep(0.5)
    raise RuntimeError(f"no {service} trace was exported for exception {exception_id}")


if __name__ == "__main__":
    raise SystemExit(main())
