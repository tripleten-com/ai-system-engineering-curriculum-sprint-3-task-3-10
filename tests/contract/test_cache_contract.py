"""Coldline.

===================

File:              tests/contract/test_cache_contract.py
Component:         Contract tests — Read cache and freshness window
Purpose:           Check the two wiring points, the configuration, the probe output, the answers.
Interacts With:    src/api/status_cache.py, src/worker/status_invalidation.py, config/cache.yaml
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Read-through cache, time to live, invalidation, generated evidence, staleness
Tools:             Python 3.12, pytest, httpx

The static checks here exercise the two wiring points against an in-process cache and
store, read the configuration, and read the probe output and the answer sheet as
committed. The two checks marked `runtime` need the running stack: one reads the API's
/metrics route, the other runs the probe for real and compares its reads with the
committed output and the answers. None of them reads the cache record: the record is
the defense material. `poe contract` skips this whole module because it is marked
`assessed`; `poe cache-checks`, `poe cache-contract`, and `poe verify` run it.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from adapters.cache import InMemoryStatusCache
from api.status_cache import (
    CACHE_AGE_HEADER,
    CACHE_OUTCOME_HEADER,
    STATUS_TTL_MAXIMUM_SECONDS,
    STATUS_TTL_MINIMUM_SECONDS,
    ReadOutcome,
    load_status_ttl_seconds,
    read_exception_status,
)
from domain.contracts import ExceptionRecord, ExceptionState, SensorReading
from tests.contract import cache_contract as cache
from worker.status_invalidation import invalidate_exception_status

pytestmark = [pytest.mark.assessed]

NOW = datetime(2026, 9, 25, tzinfo=UTC)
EXCEPTION_ID = "exc-cache-contract-001"
OTHER_ID = "exc-cache-contract-002"
READING = SensorReading(
    reading_id="reading-cache-contract",
    shipment_id="shipment-cache-contract",
    temperature_c=9.6,
    allowed_min_c=2.0,
    allowed_max_c=8.0,
    recorded_at=NOW,
)


def _record(state: ExceptionState, exception_id: str = EXCEPTION_ID) -> ExceptionRecord:
    """Build one exception record in the given state."""
    return ExceptionRecord(
        exception_id=exception_id, reading=READING, state=state, accepted_at=NOW, updated_at=NOW
    )


class Clock:
    """Hold a settable time for the in-process cache."""

    def __init__(self, now: float = 1_000.0) -> None:
        """Start at ``now`` (epoch seconds)."""
        self.now = now

    def __call__(self) -> float:
        """Return the current test time."""
        return self.now


@pytest.fixture(scope="module")
def answers() -> dict[str, Any]:
    """Load the recorded answers once."""
    return cache.load_answers()


@pytest.fixture(scope="module")
def probe() -> dict[str, Any] | None:
    """Load the committed probe output once, as written."""
    return cache.load_probe()


# --- the two wiring points, against an in-process cache and store ---------------------------


async def test_read_through_serves_a_hit_from_the_cache_without_reading_the_store() -> None:
    """A copy in the cache is returned as it is, as old as it is, and the store is left alone."""
    clock = Clock()
    status_cache = InMemoryStatusCache(clock=clock)
    await status_cache.set(EXCEPTION_ID, _record(ExceptionState.QUEUED), ttl_seconds=30)
    # The store has moved on; a hit must still return the copy, not the store.
    repository = cache.CountingRepository({EXCEPTION_ID: _record(ExceptionState.PROCESSING)})
    clock.now += 1.5

    read = await read_exception_status(EXCEPTION_ID, status_cache, repository)

    assert read is not None, "an existing record must be answered"
    assert read.outcome is ReadOutcome.HIT, "a cached copy must be answered as a hit"
    assert read.record.state is ExceptionState.QUEUED, "a hit returns the copy as it is"
    assert read.age_seconds == pytest.approx(1.5), "a hit reports the age of the copy it served"
    assert repository.reads == [], "a hit must not read the store"


async def test_read_through_reads_the_store_on_a_miss_and_caches_the_record() -> None:
    """With no copy, the store is read once, a copy is stored, and the next read is a hit."""
    status_cache = InMemoryStatusCache(clock=Clock())
    record = _record(ExceptionState.QUEUED)
    repository = cache.CountingRepository({EXCEPTION_ID: record})

    first = await read_exception_status(EXCEPTION_ID, status_cache, repository)
    second = await read_exception_status(EXCEPTION_ID, status_cache, repository)

    assert first is not None and first.outcome is ReadOutcome.MISS, "the first read is a miss"
    assert first.record == record and first.age_seconds == 0.0, "a miss returns the store's record"
    assert EXCEPTION_ID in status_cache.entries, "a miss of an existing record stores a copy"
    assert status_cache.entries[EXCEPTION_ID].record == record
    assert second is not None and second.outcome is ReadOutcome.HIT, "the copy serves the next read"
    assert repository.reads == [EXCEPTION_ID], "the store is read once, on the miss"


async def test_read_through_stores_the_copy_with_the_ttl_from_the_configuration() -> None:
    """The copy's TTL is status_ttl_seconds from config/cache.yaml, not a number in the code."""
    status_cache = InMemoryStatusCache(clock=Clock())
    repository = cache.CountingRepository({EXCEPTION_ID: _record(ExceptionState.QUEUED)})

    await read_exception_status(EXCEPTION_ID, status_cache, repository)

    stored = status_cache.entries.get(EXCEPTION_ID)
    assert stored is not None, "a miss of an existing record stores a copy"
    assert stored.ttl_seconds == load_status_ttl_seconds(), (
        f"the copy was stored with a TTL of {stored.ttl_seconds} s; config/cache.yaml says "
        f"{load_status_ttl_seconds()} s"
    )


async def test_read_through_does_not_cache_a_missing_record() -> None:
    """A status the store has never seen stays a store read and a 404; nothing is stored."""
    status_cache = InMemoryStatusCache(clock=Clock())
    repository = cache.CountingRepository()

    read = await read_exception_status(EXCEPTION_ID, status_cache, repository)

    assert read is None, "a missing record is answered with None, which the route turns into 404"
    assert status_cache.entries == {}, "a missing record must not be cached"
    assert repository.reads == [EXCEPTION_ID], "a missing record is still looked up in the store"


async def test_invalidation_deletes_the_cached_copy_for_the_updated_exception() -> None:
    """The worker's call removes the one copy the read-through stored, and no other."""
    status_cache = InMemoryStatusCache(clock=Clock())
    await status_cache.set(EXCEPTION_ID, _record(ExceptionState.QUEUED), ttl_seconds=30)
    await status_cache.set(OTHER_ID, _record(ExceptionState.QUEUED, OTHER_ID), ttl_seconds=30)

    await invalidate_exception_status(EXCEPTION_ID, status_cache)

    assert EXCEPTION_ID not in status_cache.entries, (
        "invalidate_exception_status must delete the cached copy for the exception the "
        "worker updated"
    )
    assert OTHER_ID in status_cache.entries, "invalidation is keyed by exception id"
    assert status_cache.deleted == [EXCEPTION_ID], "the adapter's delete is what removes the copy"


# --- the configuration and the answer sheet ---------------------------------------------------


def test_config_keeps_one_key_inside_the_published_range() -> None:
    """config/cache.yaml holds status_ttl_seconds alone, a whole number inside the range."""
    document = cache.load_cache_config()
    assert set(document) == {"status_ttl_seconds"}, (
        f"config/cache.yaml must hold exactly one key, status_ttl_seconds; got {sorted(document)}"
    )
    value = document["status_ttl_seconds"]
    assert isinstance(value, int) and not isinstance(value, bool), (
        "status_ttl_seconds must be a whole number of seconds"
    )
    assert STATUS_TTL_MINIMUM_SECONDS <= value <= STATUS_TTL_MAXIMUM_SECONDS, (
        f"status_ttl_seconds must be from {STATUS_TTL_MINIMUM_SECONDS} to "
        f"{STATUS_TTL_MAXIMUM_SECONDS} seconds; got {value}"
    )


def test_recorded_ttl_equals_the_configured_ttl(answers: dict[str, Any]) -> None:
    """answers.status_ttl_seconds is the value in config/cache.yaml."""
    recorded = answers.get("status_ttl_seconds")
    configured = cache.load_cache_config().get("status_ttl_seconds")
    assert recorded == configured, (
        f"answers.status_ttl_seconds records {recorded!r}; config/cache.yaml says {configured!r}"
    )


def test_probe_output_is_the_unedited_output_of_a_probe_run(
    probe: dict[str, Any] | None,
) -> None:
    """docs/student/cache/probe.json exists and is exactly as `poe cache-probe` wrote it."""
    assert cache.probe_problems(probe) == [], "\n".join(cache.probe_problems(probe))


def test_recorded_outcomes_match_the_committed_probe_output(
    answers: dict[str, Any], probe: dict[str, Any] | None
) -> None:
    """The five copied answers are the values in the committed probe output."""
    assert probe is not None, "docs/student/cache/probe.json is missing; run `poe cache-probe`"
    mismatches: list[str] = []
    for field_name in cache.COPIED_READS:
        pair = cache.copied_pair(probe, field_name)
        if pair is None:
            mismatches.append(f"the probe output holds no {field_name}")
            continue
        outcome, state = pair
        if answers.get(f"{field_name}_outcome") != outcome:
            mismatches.append(
                f"answers.{field_name}_outcome records {answers.get(f'{field_name}_outcome')!r}; "
                f"the probe output says {outcome!r}"
            )
        if field_name == "post_update_read" and answers.get("post_update_read_state") != state:
            mismatches.append(
                f"answers.post_update_read_state records "
                f"{answers.get('post_update_read_state')!r}; the probe output says {state!r}"
            )
    stale = probe.get("stale_reads_after_update")
    if answers.get("stale_reads_after_update") != stale:
        mismatches.append(
            f"answers.stale_reads_after_update records "
            f"{answers.get('stale_reads_after_update')!r}; the probe output says {stale!r}"
        )
    assert mismatches == [], "\n".join(mismatches)


def test_probe_output_shows_a_fresh_read_after_the_workers_result(
    probe: dict[str, Any] | None,
) -> None:
    """After the mark, the first read is from the store with the worker's state, and none lags."""
    assert probe is not None, "docs/student/cache/probe.json is missing; run `poe cache-probe`"
    result = cache.mapping(probe, "result").get("state")
    pair = cache.copied_pair(probe, "post_update_read")
    assert pair is not None, "the probe output holds no post_update_read"
    outcome, state = pair
    assert outcome == "miss", (
        "the first read after the worker's result must come from the store; a hit means the "
        "copy stored before the result was still there, so the invalidation did not remove it"
    )
    assert state == result and state in cache.TERMINAL_STATES, (
        f"the first read after the worker's result returned {state!r}; the worker wrote {result!r}"
    )
    later = [entry.get("state") for entry in cache.reads_after_update(probe)]
    assert all(entry == result for entry in later), (
        f"a read after the worker's result returned an earlier state: {later}"
    )
    assert probe.get("stale_reads_after_update") == 0, (
        "no read after the worker's result may return an earlier state"
    )


def test_freshness_window_holds_allowed_values_and_a_note(answers: dict[str, Any]) -> None:
    """Every field of answers.freshness_window holds one of its allowed values, with a note."""
    window = cache.mapping(answers, "freshness_window")
    problems: list[str] = []
    if window.get("reads_that_may_be_stale") not in cache.STALE_READ_CHOICES:
        problems.append(
            "answers.freshness_window.reads_that_may_be_stale must be one of "
            f"{', '.join(cache.STALE_READ_CHOICES)}"
        )
    staleness = window.get("max_staleness_seconds")
    if isinstance(staleness, bool) or not isinstance(staleness, int) or staleness < 0:
        problems.append(
            "answers.freshness_window.max_staleness_seconds must be a whole number of at least 0"
        )
    if not isinstance(window.get("acceptable_for_recipient_acceptance"), bool):
        problems.append(
            "answers.freshness_window.acceptable_for_recipient_acceptance must be true or false"
        )
    note = window.get("note")
    if not isinstance(note, str) or not note.strip():
        problems.append("answers.freshness_window.note must say why, in Elena's terms")
    assert problems == [], "\n".join(problems)


# --- the running stack ----------------------------------------------------------------------


@pytest.mark.runtime
def test_metrics_route_exposes_both_cache_counters() -> None:
    """The API's /metrics route lists the hit and miss counters, from the first scrape."""
    response = httpx.get(f"{cache.api_base_url()}/metrics", timeout=5.0)
    assert response.status_code == 200, f"/metrics answered {response.status_code}"
    names = {line.split()[0] for line in response.text.splitlines() if line and line[0] != "#"}
    for counter in (cache.HIT_COUNTER, cache.MISS_COUNTER):
        assert counter in names, f"{counter} is not exposed on the API's /metrics route"


@pytest.mark.runtime
def test_live_probe_agrees_with_the_committed_output_and_raises_the_counters(
    tmp_path: Path, answers: dict[str, Any], probe: dict[str, Any] | None
) -> None:
    """One probe run against the running stack repeats the committed sequence and the answers.

    The run also proves the two counters are in the read path: they rise on /metrics by
    exactly the hits and misses the probe made. The probe's header names are held equal to
    the route's here, so a renamed header cannot pass as a missing cache.
    """
    assert cache.probe_header_names() == (CACHE_OUTCOME_HEADER, CACHE_AGE_HEADER), (
        "the probe and the status route disagree on the cache header names"
    )
    base_url = cache.api_base_url()
    before = cache.read_counters(base_url)
    try:
        live = cache.run_probe(tmp_path / "probe.json")
    except cache.CacheCheckError as exc:
        pytest.fail(str(exc))
    after = cache.read_counters(base_url)
    assert cache.probe_problems(live, "the live probe output") == [], "\n".join(
        cache.probe_problems(live, "the live probe output")
    )
    rose_by = {label: after[label] - before[label] for label in ("hits", "misses")}
    assert rose_by == {"hits": live["hits"], "misses": live["misses"]}, (
        f"the counters rose by {rose_by}; the probe made {live['hits']} hit(s) and "
        f"{live['misses']} miss(es)"
    )
    assert probe is not None, "docs/student/cache/probe.json is missing; run `poe cache-probe`"
    # A read made while the worker is still working can return QUEUED or PROCESSING depending
    # on scheduling, so only the outcome is compared for those reads. The first read after
    # the worker's result is compared with its state too: that state is the written result.
    mismatches: list[str] = []
    for field_name in cache.COPIED_READS:
        keep = 2 if field_name == "post_update_read" else 1
        live_pair = cache.copied_pair(live, field_name) or ()
        committed_pair = cache.copied_pair(probe, field_name) or ()
        if live_pair[:keep] != committed_pair[:keep]:
            mismatches.append(
                f"{field_name}: the live probe saw {cache.copied_pair(live, field_name)}; the "
                f"committed output says {cache.copied_pair(probe, field_name)}"
            )
    if live.get("stale_reads_after_update") != probe.get("stale_reads_after_update"):
        mismatches.append(
            f"stale_reads_after_update: the live probe counted "
            f"{live.get('stale_reads_after_update')}; the committed output says "
            f"{probe.get('stale_reads_after_update')}"
        )
    assert mismatches == [], (
        "the running stack no longer behaves as the committed probe output shows; rerun "
        "`poe cache-probe` after `poe start` and commit the new output:\n" + "\n".join(mismatches)
    )
    for field_name in cache.COPIED_READS:
        pair = cache.copied_pair(live, field_name)
        assert pair is not None and answers.get(f"{field_name}_outcome") == pair[0], (
            f"answers.{field_name}_outcome does not match the live probe"
        )
    live_post = cache.copied_pair(live, "post_update_read")
    assert live_post is not None and answers.get("post_update_read_state") == live_post[1], (
        "answers.post_update_read_state does not match the live probe"
    )
