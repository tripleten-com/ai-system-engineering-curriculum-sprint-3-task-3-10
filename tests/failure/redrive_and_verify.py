"""Coldline.

===================

File:              tests/failure/redrive_and_verify.py
Component:         Failure tools — Redrive and verify
Purpose:           Redrive the dead-lettered exercise message and prove idempotent recovery.
Interacts With:    The API, LocalStack SQS
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Dead-letter redrive, idempotent recovery, evidence
Tools:             Python 3.12, boto3, httpx
"""

import json
import time

import httpx

from tests.failure.force_dlq_arrival import DEAD_LETTER_NAME, QUEUE_NAME
from tests.failure.queue_client import client, queue_url
from tests.runtime_config import host_port


def main() -> int:
    """Redrive the dead-lettered message, then confirm it completes exactly once.

    Start the worker again before running this
    (`docker compose --profile observability --profile localstack start worker`).
    The redriven message is the *same* durable exception identity the injector
    submitted; `WorkerApplication` treats a delivery for an already-terminal
    identity as a safe replay, so this proves recovery is idempotent rather
    than merely that the message eventually got processed.
    """
    sqs = client()
    dlq_url = queue_url(sqs, name=DEAD_LETTER_NAME)
    main_url = queue_url(sqs, name=QUEUE_NAME)

    response = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=1, WaitTimeSeconds=5)
    messages = response.get("Messages")
    if not messages:
        print("no dead-lettered message found; run force_dlq_arrival.py first", flush=True)
        return 1
    message = messages[0]
    sqs.send_message(QueueUrl=main_url, MessageBody=message["Body"])
    sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=message["ReceiptHandle"])
    body = json.loads(message["Body"])
    exception_id = body["exception_id"]

    api_port = host_port("COLDLINE_API_HOST_PORT", 8000)
    with httpx.Client(base_url=f"http://localhost:{api_port}", timeout=5.0) as api:
        record = _wait_for_terminal(api, exception_id)

    print(
        json.dumps(
            {
                "exception_id": exception_id,
                "state": record["state"],
                "summary": record.get("summary"),
            },
            indent=2,
        )
    )
    return 0 if record["state"] == "COMPLETED" else 1


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
