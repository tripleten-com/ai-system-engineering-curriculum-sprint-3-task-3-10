"""Coldline.

===================

File:              tests/unit/test_scenario.py
Component:         Unit tests — Published scenario output
Purpose:           Verify singular current trace references in scenario output
Interacts With:    tests.e2e.scenario
Sprint/Task:       Sprint 1 — Project 1
Concepts:          Deterministic evidence shape
Tools:             Python 3.12, pytest
"""

import json

import httpx
import pytest

from tests.e2e import scenario


@pytest.mark.parametrize("joined", [False, True], ids=["separate-traces", "joined-trace"])
def test_published_scenario_prints_one_api_and_one_worker_trace_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], joined: bool
) -> None:
    """The command reports current references both before and after telemetry repair."""
    real_client = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"status_url": "/api/v1/exceptions/example"})
        if request.url.path == "/api/v1/exceptions/example":
            return httpx.Response(
                200, json={"exception_id": "exception-example", "state": "COMPLETED"}
            )
        assert request.url.path == "/api/traces"
        assert json.loads(request.url.params["tags"]) == {
            "coldline.exception_id": "exception-example"
        }
        service = request.url.params["service"]
        trace_id = "joined-trace" if joined else f"{service}-trace"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"traceID": "older-trace", "spans": [{"startTime": 10}]},
                    {"traceID": trace_id, "spans": [{"startTime": 20}]},
                ]
            },
        )

    monkeypatch.setattr(
        scenario.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )

    assert scenario.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "COMPLETED"
    assert output["api_trace_id"] == ("joined-trace" if joined else "coldline-api-trace")
    assert output["worker_trace_id"] == ("joined-trace" if joined else "coldline-worker-trace")
    assert "api_trace_ids" not in output
    assert "worker_trace_ids" not in output


@pytest.mark.parametrize("reverse", [False, True])
def test_trace_lookup_selects_the_most_recent_matching_trace(reverse: bool) -> None:
    """Jaeger result ordering does not choose an older run's trace reference."""
    traces = [
        {"traceID": "older-trace", "spans": [{"startTime": 10}]},
        {"traceID": "newer-trace", "spans": [{"startTime": 5}, {"startTime": 20}]},
    ]
    if reverse:
        traces.reverse()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": traces}))
    ) as client:
        assert scenario._wait_for_traces(client, "coldline-api", "example") == "newer-trace"
