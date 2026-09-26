"""Coldline.

===================

File:              tests/failure/queue_client.py
Component:         Failure tools — Queue client
Purpose:           Connect the dead-letter exercise scripts to LocalStack SQS from the host.
Interacts With:    LocalStack SQS
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Dead-letter redrive, deterministic infrastructure
Tools:             Python 3.12, boto3, LocalStack
"""

from typing import Any

from adapters.queue import create_sqs_client
from tests.runtime_config import host_port


def client() -> Any:
    """Return one SQS client reaching LocalStack from the host, not a container."""
    port = host_port("COLDLINE_LOCALSTACK_HOST_PORT", 4566)
    return create_sqs_client(
        endpoint_url=f"http://localhost:{port}",
        region_name="us-east-1",
        access_key_id="localstack-development-key",
        secret_access_key="localstack-development-secret",
    )


def queue_url(sqs: Any, *, name: str) -> str:
    """Resolve one supplied queue name to its URL."""
    return str(sqs.get_queue_url(QueueName=name)["QueueUrl"])
