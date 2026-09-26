"""Coldline.

===================

File:              tests/contract/cache_contract.py
Component:         Contract tests — Cache helpers
Purpose:           Read the probe output, the answer sheet, the configuration, and the running API.
Interacts With:    docs/student/cache/probe.json, submission.yaml, config/cache.yaml, /metrics
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Generated evidence, content digest, read-through cache, counters
Tools:             Python 3.12, httpx

The checks never import the probe. They read the file it wrote exactly as a student
commits it, recompute the content digest the same way the probe did, and, for the one
live check, run the probe as a subprocess exactly as ``poe cache-probe`` does, so what
they assert is what the file and the running stack show.
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import httpx
import yaml

from domain.contracts import ExceptionRecord, ExceptionState
from tests.runtime_config import host_port

TASK_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = TASK_ROOT / "docs/student/cache/probe.json"
PROBE = TASK_ROOT / "infra/cache/cache_probe.py"
CONFIG_PATH = TASK_ROOT / "config/cache.yaml"
GENERATOR = "poe cache-probe"
OUTCOMES = ("hit", "miss")
STATES = ("RECEIVED", "QUEUED", "PROCESSING", "COMPLETED", "FAILED")
TERMINAL_STATES = ("COMPLETED", "FAILED")
STALE_READ_CHOICES = ("none", "in_progress_only", "all")
# The three reads the answer sheet copies from the probe output, by the summary field that
# holds each one's outcome and state.
COPIED_READS = ("first_read", "second_read", "post_update_read")
HIT_COUNTER = "coldline_status_cache_hits_total"
MISS_COUNTER = "coldline_status_cache_misses_total"


class CacheCheckError(ValueError):
    """Report one actionable cache-evidence failure."""


def load_probe(path: Path = PROBE_PATH) -> dict[str, Any] | None:
    """Return the probe output as written, or None when it does not exist or is not JSON."""
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else None


def digest(summary: dict[str, Any]) -> str:
    """Return the content digest of a probe output, over everything but the digest itself.

    Identical to ``infra/cache/cache_probe.py``'s ``digest``; the two must stay the same,
    because an output whose digest no longer matches its content is one that was edited.
    """
    body = {key: value for key, value in summary.items() if key != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_pair(value: object) -> bool:
    """Return whether one value is an outcome/state pair the probe writes."""
    return (
        isinstance(value, dict)
        and value.get("outcome") in OUTCOMES
        and value.get("state") in STATES
    )


def probe_problems(
    summary: dict[str, Any] | None, name: str = "docs/student/cache/probe.json"
) -> list[str]:
    """Return every reason one file is not what `poe cache-probe` writes."""
    if summary is None:
        return [f"{name} is missing or is not one JSON object; run `poe cache-probe`"]
    problems: list[str] = []
    if summary.get("generator") != GENERATOR:
        problems.append(f"{name} was not written by {GENERATOR}")
    if summary.get("digest") != digest(summary):
        problems.append(f"{name} has been edited since {GENERATOR} wrote it (digest mismatch)")
    reads = summary.get("reads")
    if not isinstance(reads, list) or len(reads) < 3:
        problems.append(f"{name}: reads must list the two reads before the mark and those after")
    else:
        for entry in reads:
            if not _is_pair(entry) or entry.get("phase") not in ("before_update", "after_update"):
                problems.append(f"{name}: a read is missing its outcome, state, or phase")
                break
        if [entry.get("phase") for entry in reads[:2]] != ["before_update"] * 2:
            problems.append(f"{name}: the first two reads must come before the worker's result")
        if not any(entry.get("phase") == "after_update" for entry in reads):
            problems.append(f"{name}: no read after the worker's result was recorded")
    for field_name in COPIED_READS:
        if not _is_pair(summary.get(field_name)):
            problems.append(f"{name}: {field_name} must hold an outcome and a state")
    result = summary.get("result")
    if not isinstance(result, dict) or result.get("state") not in TERMINAL_STATES:
        problems.append(f"{name}: result must name the terminal state the worker wrote")
    stale = summary.get("stale_reads_after_update")
    if isinstance(stale, bool) or not isinstance(stale, int) or stale < 0:
        problems.append(f"{name}: stale_reads_after_update must be a whole number")
    for counter in ("hits", "misses"):
        value = summary.get(counter)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"{name}: {counter} must be a whole number")
    return problems


def copied_pair(summary: dict[str, Any], field_name: str) -> tuple[str, str] | None:
    """Return one copied read's outcome and state from a probe output, or None."""
    value = summary.get(field_name)
    if not _is_pair(value):
        return None
    pair = cast(dict[str, Any], value)
    return str(pair["outcome"]), str(pair["state"])


def reads_after_update(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every read the probe made after it saw the worker's result."""
    reads = summary.get("reads")
    if not isinstance(reads, list):
        return []
    return [
        cast(dict[str, Any], entry)
        for entry in reads
        if isinstance(entry, dict) and entry.get("phase") == "after_update"
    ]


def load_answers() -> dict[str, Any]:
    """Load the recorded answers; a blank sheet still lets every check run and report."""
    document = yaml.safe_load((TASK_ROOT / "submission.yaml").read_text(encoding="utf-8"))
    recorded = document.get("answers") if isinstance(document, dict) else None
    return recorded if isinstance(recorded, dict) else {}


def mapping(container: dict[str, Any], key: str) -> dict[str, Any]:
    """Return one nested answers mapping, or an empty mapping when it is absent or not one."""
    value = container.get(key)
    return value if isinstance(value, dict) else {}


def load_cache_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Return config/cache.yaml as one mapping, or an empty mapping when it is not one."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document if isinstance(document, dict) else {}


def api_base_url() -> str:
    """Return the API's base URL, honouring the documented host-port override."""
    return f"http://localhost:{host_port('COLDLINE_API_HOST_PORT', 8000)}"


def read_counters(base_url: str) -> dict[str, float]:
    """Return the two cache counters as the API's /metrics route reports them right now."""
    response = httpx.get(f"{base_url}/metrics", timeout=5.0)
    response.raise_for_status()
    values = {"hits": 0.0, "misses": 0.0}
    for line in response.text.splitlines():
        for label, name in (("hits", HIT_COUNTER), ("misses", MISS_COUNTER)):
            if line.startswith(f"{name} "):
                values[label] = float(line.split()[1])
    return values


def probe_header_names() -> tuple[str | None, str | None]:
    """Return the outcome and age header names the probe's source declares.

    Read from the text rather than imported: ``infra/`` is not a package, and the
    probe is run as a script, never imported, everywhere else.
    """
    source = PROBE.read_text(encoding="utf-8")
    found: dict[str, str | None] = {"OUTCOME_HEADER": None, "AGE_HEADER": None}
    for name in found:
        match = re.search(rf'^{name} = "([^"]+)"$', source, re.MULTILINE)
        found[name] = match.group(1) if match else None
    return found["OUTCOME_HEADER"], found["AGE_HEADER"]


def run_probe(output: Path) -> dict[str, Any]:
    """Run the probe exactly as `poe cache-probe` does, writing to ``output``, and load it."""
    result = subprocess.run(
        [sys.executable, str(PROBE), "--output", str(output)],
        cwd=TASK_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CacheCheckError(
            f"cache_probe.py failed (exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    loaded = load_probe(output)
    if loaded is None:
        raise CacheCheckError("the probe exited 0 but wrote no JSON object")
    return loaded


class CountingRepository:
    """Hold exception records for the read-through checks and count every store read."""

    def __init__(self, records: dict[str, ExceptionRecord] | None = None) -> None:
        """Start with the given records and no reads."""
        self.records: dict[str, ExceptionRecord] = dict(records or {})
        self.reads: list[str] = []

    async def get(self, exception_id: str) -> ExceptionRecord | None:
        """Return one record and remember that the store was asked."""
        self.reads.append(exception_id)
        return self.records.get(exception_id)

    async def create(self, record: ExceptionRecord) -> ExceptionRecord:
        """Create or return one record; the read-through checks never call this."""
        return self.records.setdefault(record.exception_id, record)

    async def transition(
        self,
        exception_id: str,
        expected: set[ExceptionState],
        target: ExceptionState,
        *,
        summary: str | None = None,
        failure_reason: str | None = None,
    ) -> ExceptionRecord:
        """Apply one state transition; the read-through checks never call this."""
        current = self.records[exception_id]
        if current.state not in expected:
            raise RuntimeError(f"invalid state transition for {exception_id}")
        updated = current.model_copy(
            update={
                "state": target,
                "summary": summary if summary is not None else current.summary,
                "failure_reason": failure_reason,
            }
        )
        self.records[exception_id] = updated
        return updated
