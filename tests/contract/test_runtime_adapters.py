"""Coldline.

===================

File:              tests/contract/test_runtime_adapters.py
Component:         Contract tests — Runtime adapters
Purpose:           Runs real PostgreSQL and SQS contracts inside the worker container.
Interacts With:    Docker Compose, PostgreSQL, LocalStack SQS, and worker image
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Initialization, idempotency, dead-letter redrive, terminal failure
Tools:             Python 3.12, pytest, Docker Compose
"""

import subprocess
from pathlib import Path

import pytest

TASK_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.runtime


def _compose(*arguments: str) -> None:
    """Run one Docker Compose command against this Task's stack."""
    subprocess.run(["docker", "compose", *arguments], cwd=TASK_ROOT, check=True)


def test_postgres_and_sqs_adapters_preserve_runtime_contracts() -> None:
    """Catch a provider adapter that passes unit tests but fails on real services.

    The real ``worker`` container runs continuously and consumes from the same
    production queue this verifier's own dead-letter check publishes to and
    receives from directly; left running, it competes with — and reliably
    wins — the verifier's own receive. The container is stopped for the
    duration of the check and always restarted afterward, exactly like the
    supplied ``poe worker-stop``/``poe worker-start`` failure exercise.
    """
    verifier = (TASK_ROOT / "tests" / "contract" / "runtime_adapters.py").read_text(
        encoding="utf-8"
    )
    # `exec` needs a running container to attach to, so stopping the real
    # consumer rules that out; `run` starts a fresh one-off container from
    # the same image instead, on the same network, without touching its
    # dependencies (already up).
    _compose("stop", "worker")
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "worker",
                "python",
                "-",
            ],
            cwd=TASK_ROOT,
            input=verifier,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
    finally:
        _compose("start", "worker")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Integration verification passed" in result.stdout
