"""Coldline.

===================

File:              infra/cache/cache_probe.py
Component:         Cache tools — Status read probe
Purpose:           Read one exception's status around the worker's result and record every read.
Interacts With:    The API and its cache headers, PostgreSQL via Docker Compose, /metrics
Sprint/Task:       Sprint 3 — Project 3 / Task 3.10
Concepts:          Read-through cache, cache hit and miss, invalidation, staleness, evidence
Tools:             Python 3.12, httpx, Docker Compose, psql

``poe cache-probe`` is the whole measurement, and a student runs it without options. In
order, it

1. submits one fresh out-of-range reading and takes the exception id the API returns;
2. reads the status twice, back to back, well inside the smallest TTL the configuration
   allows and before the worker can have finished (its provider takes a quarter of a
   second per reading): nothing was cached before the first read, and the second comes
   within the TTL;
3. waits for the worker's result by polling the store itself, through ``docker compose
   exec`` into the ``postgres`` container, and marks the moment the store holds a terminal
   state;
4. reads the status again at once and then once a second until a read returns the state
   the worker wrote, plus two more reads served from the copy that read stored;
5. prints one line per read (hit or miss, the state returned, and the age of the copy
   served, all from the status route's two headers), the two counters' movement on
   ``/metrics``, and writes the same sequence to ``docs/student/cache/probe.json`` with a
   generator marker and a content digest that the contract checks recompute.

The two reads before the mark have to happen before the worker finishes, or the second
one could be answered by a copy the first stored *after* an invalidation, and the sequence
would say nothing about the read-through. The probe therefore gives them a small time
budget and, when the budget is missed on a busy machine, discards that reading and
submits another.

Never edit the output. Rerun instead; the new file replaces the old one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

TASK_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_PATH = TASK_ROOT / "docs/student/cache/probe.json"
GENERATOR = "poe cache-probe"
SCHEMA = "coldline-cache-probe/1"
COMPOSE_PROFILES = ("--profile", "observability", "--profile", "localstack")
# The two headers the status route adds to every successful read; the same names as
# src/api/status_cache.py declares (tests/contract/test_cache_contract.py holds them equal).
OUTCOME_HEADER = "X-Coldline-Cache"
AGE_HEADER = "X-Coldline-Cache-Age-Seconds"
HIT_COUNTER = "coldline_status_cache_hits_total"
MISS_COUNTER = "coldline_status_cache_misses_total"
TERMINAL_STATES = frozenset({"COMPLETED", "FAILED"})
EXCEPTION_ID = re.compile(r"^exc-[0-9a-f-]{36}$")
# The budget for the two reads before the mark, measured from the moment the API accepted
# the reading. The worker's provider takes 250 ms per reading (COLDLINE_MODEL_LATENCY_MS in
# compose.yaml), so two reads that finish inside this budget are made before its result
# exists; the smallest TTL the configuration allows is 5 s, so the second is inside it.
PRE_UPDATE_BUDGET_SECONDS = 0.2
PRE_UPDATE_READS = 2
SUBMISSION_ATTEMPTS = 5
STORE_POLL_INTERVAL_SECONDS = 0.25
WORKER_DEADLINE_SECONDS = 90.0
POST_UPDATE_INTERVAL_SECONDS = 1.0
POST_UPDATE_CONFIRMATION_READS = 2
# Long enough for a copy stored just before the mark to reach the largest TTL the
# configuration allows (120 s) and expire, so a run without invalidation still ends.
POST_UPDATE_DEADLINE_SECONDS = 150.0
READ_TIMEOUT_SECONDS = 5.0


class ProbeError(RuntimeError):
    """Report one actionable failure of the probe without a stack trace."""


@dataclass(frozen=True)
class Read:
    """One status read, as the route answered it."""

    sequence: int
    phase: str
    offset_seconds: float
    outcome: str
    state: str
    age_seconds: float


# --- host configuration -------------------------------------------------------------------


def _host_port(name: str, default: int) -> int:
    """Return one host port from the shell, the local `.env`, or the documented default."""
    value = os.environ.get(name)
    dotenv = TASK_ROOT / ".env"
    if value is None and dotenv.is_file():
        for raw_line in dotenv.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith(f"{name}="):
                value = line.split("=", maxsplit=1)[1].strip().strip('"').strip("'")
    if value is None:
        return default
    try:
        port = int(value)
    except ValueError as exc:
        raise ProbeError(f"{name} must be an integer host port") from exc
    if not 1 <= port <= 65_535:
        raise ProbeError(f"{name} must be between 1 and 65535")
    return port


def require_ready(client: httpx.Client) -> None:
    """Refuse to measure a stack that is not ready."""
    try:
        response = client.get("/health/ready")
    except httpx.HTTPError as exc:
        raise ProbeError(f"the API at {client.base_url} did not answer: {exc}") from exc
    if response.status_code != 200:
        raise ProbeError(
            f"the API at {client.base_url} answered {response.status_code} on /health/ready; "
            "run `poe start` and `poe ready` before `poe cache-probe`"
        )


# --- the API side: one submission, and reads through the status route -----------------------


def submit_reading(client: httpx.Client) -> tuple[str, str]:
    """Submit one fresh out-of-range reading and return its exception id and status path."""
    token = uuid.uuid4().hex[:12]
    reading = {
        "reading_id": f"reading-cache-probe-{token}",
        "shipment_id": f"shipment-cache-probe-{token}",
        "temperature_c": 9.6,
        "allowed_min_c": 2.0,
        "allowed_max_c": 8.0,
        "recorded_at": datetime.now(UTC).isoformat(),
        "context": "Sprint 3 Task 3.10 cache probe",
    }
    try:
        response = client.post("/api/v1/readings", json=reading)
    except httpx.HTTPError as exc:
        raise ProbeError(f"the reading could not be submitted: {exc}") from exc
    if response.status_code != 202:
        raise ProbeError(
            f"the API answered {response.status_code} instead of 202 Accepted: {response.text}"
        )
    body = response.json()
    exception_id = str(body.get("exception_id", ""))
    status_url = str(body.get("status_url", ""))
    if not EXCEPTION_ID.match(exception_id) or not status_url:
        raise ProbeError(f"the API returned an unexpected acceptance body: {body}")
    return exception_id, status_url


def read_status(
    client: httpx.Client, status_url: str, *, sequence: int, phase: str, started: float
) -> Read:
    """Read the status once and take the outcome and age from the route's two headers."""
    try:
        response = client.get(status_url)
    except httpx.HTTPError as exc:
        raise ProbeError(f"status read {sequence} failed: {exc}") from exc
    offset = time.time() - started
    if response.status_code != 200:
        raise ProbeError(f"status read {sequence} answered {response.status_code}: {response.text}")
    outcome = response.headers.get(OUTCOME_HEADER)
    age = response.headers.get(AGE_HEADER)
    if outcome not in {"hit", "miss"} or age is None:
        raise ProbeError(
            "the status route did not report the cache outcome and age headers; the running "
            "API is not this Task's build. Run `poe start` so Compose rebuilds it, then rerun"
        )
    body = response.json()
    state = body.get("state") if isinstance(body, dict) else None
    if not isinstance(state, str):
        raise ProbeError(f"status read {sequence} returned no state: {body}")
    return Read(
        sequence=sequence,
        phase=phase,
        offset_seconds=round(offset, 3),
        outcome=outcome,
        state=state,
        age_seconds=round(float(age), 3),
    )


def read_counters(client: httpx.Client) -> dict[str, float]:
    """Return the two cache counters as the API's /metrics route reports them right now."""
    try:
        response = client.get("/metrics")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ProbeError(f"/metrics did not answer: {exc}") from exc
    values = {"hits": 0.0, "misses": 0.0}
    for line in response.text.splitlines():
        for label, name in (("hits", HIT_COUNTER), ("misses", MISS_COUNTER)):
            if line.startswith(f"{name} "):
                values[label] = float(line.split()[1])
    return values


# --- the store side: when did the worker write its result? ---------------------------------


def store_state(exception_id: str) -> tuple[str, float] | None:
    """Return the store's state and its updated_at (epoch seconds) for one exception.

    The store is not published to the host, so this asks it through the Compose project,
    exactly as the supplied `poe reset-baseline` target reaches it. The id was produced
    by the API and matched against a fixed shape before it is placed in the query.
    """
    if not EXCEPTION_ID.match(exception_id):
        raise ProbeError(f"refusing to query the store for an unexpected id: {exception_id}")
    query = (
        "SELECT state || '|' || extract(epoch FROM updated_at) FROM exceptions "
        f"WHERE exception_id = '{exception_id}'"
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            *COMPOSE_PROFILES,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "coldline",
            "-d",
            "coldline",
            "-tA",
            "-c",
            query,
        ],
        cwd=TASK_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no detail"
        raise ProbeError(f"the store could not be read through docker compose exec: {detail}")
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if "|" not in line:
        return None
    state, epoch = line.rsplit("|", maxsplit=1)
    return state.strip(), float(epoch)


def wait_for_result(
    exception_id: str, *, started: float
) -> tuple[str, float, float, list[dict[str, Any]]]:
    """Poll the store until it holds a terminal state; return it with two offsets.

    The first offset is the store's own ``updated_at`` for the terminal write; the second
    is when this probe saw it, which is the mark every later read is measured from. The
    intermediate states the polls happen to catch come back too, for the record.
    """
    deadline = time.time() + WORKER_DEADLINE_SECONDS
    seen: list[dict[str, Any]] = []
    while True:
        observed = store_state(exception_id)
        now = time.time()
        if observed is not None:
            state, updated_at = observed
            if not seen or seen[-1]["state"] != state:
                seen.append({"state": state, "offset_seconds": round(now - started, 3)})
            if state in TERMINAL_STATES:
                return state, round(updated_at - started, 3), round(now - started, 3), seen
        if now >= deadline:
            raise ProbeError(
                f"the worker wrote no result for {exception_id} within "
                f"{WORKER_DEADLINE_SECONDS:.0f} s; check `docker compose ps` and the worker logs"
            )
        time.sleep(STORE_POLL_INTERVAL_SECONDS)


# --- the record ---------------------------------------------------------------------------


def digest(summary: dict[str, Any]) -> str:
    """Return the content digest of a summary, over everything but the digest itself.

    ``tests/contract/cache_contract.py`` recomputes this from the file exactly the same
    way; the two must stay identical.
    """
    body = {key: value for key, value in summary.items() if key != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pair(read: Read) -> dict[str, str]:
    """Return the two fields the answer sheet copies from one read."""
    return {"outcome": read.outcome, "state": read.state}


def _print_read(read: Read) -> None:
    """Print one read the way the lesson describes it: outcome, state, age."""
    print(
        f"read {read.sequence:>2}  {read.outcome:<4}  {read.state:<10}  "
        f"age {read.age_seconds:7.3f} s  (+{read.offset_seconds:.2f} s)",
        flush=True,
    )


def _submit_and_read_twice(client: httpx.Client) -> tuple[str, str, float, list[Read], int]:
    """Submit a reading and read it twice inside the budget, retrying on a busy machine."""
    for attempt in range(1, SUBMISSION_ATTEMPTS + 1):
        started = time.time()
        exception_id, status_url = submit_reading(client)
        accepted_at = time.time()
        reads = [
            read_status(
                client, status_url, sequence=sequence, phase="before_update", started=started
            )
            for sequence in range(1, PRE_UPDATE_READS + 1)
        ]
        elapsed = time.time() - accepted_at
        if elapsed <= PRE_UPDATE_BUDGET_SECONDS and all(
            read.state not in TERMINAL_STATES for read in reads
        ):
            return exception_id, status_url, started, reads, attempt
        print(
            f"the first two reads took {elapsed:.2f} s, longer than the "
            f"{PRE_UPDATE_BUDGET_SECONDS:.1f} s that keeps them ahead of the worker; "
            "submitting another reading",
            file=sys.stderr,
            flush=True,
        )
    raise ProbeError(
        f"could not read a status twice before the worker finished, in {SUBMISSION_ATTEMPTS} "
        "readings; the machine is too busy for a clean sequence, rerun when it is quieter"
    )


def run(output: Path) -> dict[str, Any]:
    """Run the whole probe against the running stack and write the summary to ``output``."""
    api_port = _host_port("COLDLINE_API_HOST_PORT", 8000)
    with httpx.Client(
        base_url=f"http://localhost:{api_port}", timeout=READ_TIMEOUT_SECONDS
    ) as client:
        require_ready(client)
        counters_before = read_counters(client)
        exception_id, status_url, started, reads, attempts = _submit_and_read_twice(client)
        print(f"submitted exception {exception_id} (status {status_url})", flush=True)
        for read in reads:
            _print_read(read)
        result_state, written_offset, detected_offset, polls = wait_for_result(
            exception_id, started=started
        )
        print(
            f"--- worker result written: {result_state} (store updated_at +{written_offset:.2f} s, "
            f"seen at +{detected_offset:.2f} s) ---",
            flush=True,
        )
        deadline = time.time() + POST_UPDATE_DEADLINE_SECONDS
        confirmations_left: int | None = None
        sequence = len(reads)
        while True:
            sequence += 1
            read = read_status(
                client, status_url, sequence=sequence, phase="after_update", started=started
            )
            reads.append(read)
            _print_read(read)
            if confirmations_left is None:
                if read.state == result_state:
                    confirmations_left = POST_UPDATE_CONFIRMATION_READS
            else:
                confirmations_left -= 1
            if confirmations_left == 0:
                break
            if time.time() >= deadline:
                raise ProbeError(
                    f"no read returned the worker's result ({result_state}) within "
                    f"{POST_UPDATE_DEADLINE_SECONDS:.0f} s of it being written; the copy being "
                    "served is older than any TTL the configuration allows"
                )
            time.sleep(POST_UPDATE_INTERVAL_SECONDS)
        counters_after = read_counters(client)

    after_update = [read for read in reads if read.phase == "after_update"]
    stale = sum(1 for read in after_update if read.state != result_state)
    hits = sum(1 for read in reads if read.outcome == "hit")
    misses = sum(1 for read in reads if read.outcome == "miss")
    rose_by = {
        label: round(counters_after[label] - counters_before[label]) for label in ("hits", "misses")
    }
    summary: dict[str, Any] = {
        "generator": GENERATOR,
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "exception_id": exception_id,
        "status_url": status_url,
        "submission_attempts": attempts,
        "probe": {
            "pre_update_reads": PRE_UPDATE_READS,
            "pre_update_budget_seconds": PRE_UPDATE_BUDGET_SECONDS,
            "post_update_interval_seconds": POST_UPDATE_INTERVAL_SECONDS,
            "post_update_confirmation_reads": POST_UPDATE_CONFIRMATION_READS,
        },
        "reads": [asdict(read) for read in reads],
        "store_polls": polls,
        "result": {
            "state": result_state,
            "written_offset_seconds": written_offset,
            "detected_offset_seconds": detected_offset,
        },
        "first_read": _pair(reads[0]),
        "second_read": _pair(reads[1]),
        "post_update_read": _pair(after_update[0]),
        "stale_reads_after_update": stale,
        "hits": hits,
        "misses": misses,
        "counters": {"before": counters_before, "after": counters_after, "rose_by": rose_by},
    }
    summary["digest"] = digest(summary)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"hits {hits}, misses {misses}; {HIT_COUNTER} rose by {rose_by['hits']}, "
        f"{MISS_COUNTER} rose by {rose_by['misses']}; "
        f"{stale} read(s) after the result returned an earlier state",
        flush=True,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    """Run the probe and report where it wrote the sequence."""
    parser = argparse.ArgumentParser(description="Probe the exception status cache.")
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="where to write the sequence; the default is the file the checks read",
    )
    args = parser.parse_args(argv)
    try:
        run(args.output)
    except ProbeError as exc:
        print(f"cache probe failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cache probe interrupted; nothing was written, rerun when ready", file=sys.stderr)
        return 130
    try:
        shown = args.output.resolve().relative_to(TASK_ROOT).as_posix()
    except ValueError:
        shown = str(args.output)
    print(f"wrote {shown}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
