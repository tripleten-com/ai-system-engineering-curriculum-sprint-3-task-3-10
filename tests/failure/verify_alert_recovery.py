"""Coldline.

===================

File:              tests/failure/verify_alert_recovery.py
Component:         Failure tools — Verify alert recovery
Purpose:           Redrive the dead-lettered exercise message and prove the alert resolves.
Interacts With:    The API, LocalStack SQS, Alertmanager
Sprint/Task:       Sprint 3 — Project 3
Concepts:          SLO/alert, dead-letter redrive, idempotent recovery, evidence
Tools:             Python 3.12, boto3, httpx
"""

import json
import time

import httpx

from tests.failure.force_dlq_arrival import DEAD_LETTER_NAME, QUEUE_NAME
from tests.failure.queue_client import client, queue_url
from tests.failure.trigger_alert_load import wait_for_alert_state
from tests.runtime_config import host_port

# Long enough to clear a correctly configured alert's `resolve_timeout` plus
# scrape and evaluation slack.
RESOLVED_WAIT_SECONDS = 45.0


def main() -> int:
    """Redrive the dead-lettered message, then confirm recovery and alert resolution.

    Run this after `poe trigger-alert-load`, which restarts the worker; that
    is a precondition here, not something this script repeats. The redriven
    message is the *same* durable exception identity the load script
    submitted; `WorkerApplication` treats a delivery for an already-terminal
    identity as a safe replay, so this proves recovery is idempotent, not
    merely eventual.
    """
    sqs = client()
    dlq_url = queue_url(sqs, name=DEAD_LETTER_NAME)
    main_url = queue_url(sqs, name=QUEUE_NAME)

    response = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=1, WaitTimeSeconds=5)
    messages = response.get("Messages")
    if not messages:
        print("no dead-lettered message found; run trigger_alert_load.py first", flush=True)
        return 1
    message = messages[0]
    sqs.send_message(QueueUrl=main_url, MessageBody=message["Body"])
    sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=message["ReceiptHandle"])
    body = json.loads(message["Body"])
    exception_id = body["exception_id"]

    api_port = host_port("COLDLINE_API_HOST_PORT", 8000)
    with httpx.Client(base_url=f"http://localhost:{api_port}", timeout=5.0) as api:
        record = _wait_for_terminal(api, exception_id)

    alert_state, _ = wait_for_alert_state(
        target_state="resolved", timeout_seconds=RESOLVED_WAIT_SECONDS
    )
    resolved = alert_state in {"resolved", "absent"}

    print(
        json.dumps(
            {
                "exception_id": exception_id,
                "state": record["state"],
                "alert_state": alert_state,
            },
            indent=2,
        )
    )
    return 0 if record["state"] == "COMPLETED" and resolved else 1


def _wait_for_terminal(api: httpx.Client, exception_id: str) -> dict[str, object]:
    """Poll the durable record until it reaches a terminal state."""
    for _ in range(30):
        response = api.get(f"/api/v1/exceptions/{exception_id}")
        response.raise_for_status()
        record: dict[str, object] = response.json()
        if record["state"] in {"COMPLETED", "FAILED"}:
            return record
        time.sleep(1)
    raise TimeoutError(f"exception {exception_id} did not reach a terminal state")


if __name__ == "__main__":
    raise SystemExit(main())
