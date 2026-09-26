# Task 3.10 — Read cache and freshness window contract

Your repository is the finished Project 3 system. This Task changes one read path on it:
the exception status view, `GET /api/v1/exceptions/{exception_id}`, that dispatchers and
receiving clinics refresh while a reading is being evaluated. You put a short-lived Redis
cache in front of it, prove with a supplied probe and the metrics that repeated reads are
served from the copy, remove the copy the moment the worker writes its result and prove
the next read is fresh, and then state the staleness a recipient may accept as a
structured answer. You fill two supplied functions and set one number; you never touch the
cache adapter, the route, the worker loop, the probe, or a test.

## What is assessed, and by whom

| Assessed | By |
|---|---|
| The pull request changes only `src/api/status_cache.py`, `src/worker/status_invalidation.py`, `config/cache.yaml`, `docs/student/cache/probe.json`, `docs/student/task-3-10-cache-record.md`, and `submission.yaml` | Automated, in this repository (`poe answers`, and `poe verify` repeats it) |
| The answer sheet has the published shape, every enumerated field holds an allowed value, and the cache record has no template marker left | Automated (same command) |
| `read_exception_status` asks the cache first, reads the store only on a miss, stores an existing record with the TTL from `config/cache.yaml`, and never caches a missing record | Automated, in this repository, against an in-process cache and store (`poe cache-checks`) |
| `invalidate_exception_status` deletes the cached copy for the exception the worker updated | Automated (same command) |
| `config/cache.yaml` keeps its single key inside the published range, and `answers.status_ttl_seconds` equals it | Automated (same command) |
| `docs/student/cache/probe.json` is the unedited output of a `poe cache-probe` run, and the five copied answers match it | Automated (same command) |
| The first read after the worker's result in that output came from the store and returned the worker's state, and no later read returned an earlier state | Automated (same command) |
| Both cache counters are exposed on the API's `/metrics` route, and one live probe run repeats the committed sequence and raises them by its hits and misses | Automated, in this repository, against the running stack (same command) |
| `answers.freshness_window`: the three enumerated and numeric fields | Protected automated check, after you submit on the platform |
| Your cache record, your TTL's reasoning, and your note | Your instructor, if the Add-On evidence is referenced at the Project Defense |

## The supplied pieces

| Supplied | Where | What it does |
|---|---|---|
| The cache adapter | `src/adapters/cache/redis_status_cache.py` | `get`, `set(..., ttl_seconds=...)`, and `delete`, each keyed by exception id, against the Redis role in `compose.yaml` (`COLDLINE_REDIS_URL`). Every `get` counts once as a hit or a miss on `coldline_status_cache_hits_total` and `coldline_status_cache_misses_total`, exported on the API's `/metrics` route. |
| The TTL configuration | `config/cache.yaml` | One key, `status_ttl_seconds`. Its comments publish the range, 5 to 120 seconds inclusive. The API validates it at start-up. |
| The read-through wiring point | `src/api/status_cache.py`, `read_exception_status(exception_id, cache, repository)` | Called by the status route on every read. The placeholder reads the store every time and counts a miss. Step 1 is its body. |
| The invalidation wiring point | `src/worker/status_invalidation.py`, `invalidate_exception_status(exception_id, cache)` | Called by the worker loop right after it writes a processing result, a completion or a failure alike, before it acknowledges the delivery. The placeholder does nothing. Step 3 is its body. |
| The status route | `src/api/routes.py` | Answers `GET /api/v1/exceptions/{exception_id}` through `read_exception_status` and adds two headers: `X-Coldline-Cache` (`hit` or `miss`) and `X-Coldline-Cache-Age-Seconds`. |
| The worker loop | `src/worker/runtime.py` | Calls `invalidate_exception_status` after every `ACK` disposition, the one the use case returns after writing `COMPLETED` or `FAILED`. The intermediate `QUEUED` to `PROCESSING` write is not followed by a call. |
| The probe | `infra/cache/cache_probe.py`, `poe cache-probe` | Submits one reading, reads its status twice back to back, polls the store until the worker's result is written and marks that point, then reads again until a read returns the worker's state plus two more. Prints one line per read and writes `docs/student/cache/probe.json` with a generator marker and a content digest. |
| The record template | `docs/student/task-3-10-cache-record.md` | One section per Step. |

## The four Steps

### Step 1 — Implement the read-through and choose the TTL

Fill in `read_exception_status`: ask the cache; return a hit as it is; on a miss read the
store; a missing record stays a store read and a 404; store a copy of an existing record
with `status_ttl_seconds()` and return it. Set `status_ttl_seconds` in `config/cache.yaml`
to a whole number inside the range and copy it into `answers.status_ttl_seconds`. Run
`poe start` again so Compose rebuilds both images with your code and configuration
(`poe restart` does not rebuild), confirm both counters on `/metrics`, and run
`poe cache-probe` once to see at least one hit.

### Step 2 — Prove hits and misses with the probe and the metrics

Run `poe cache-probe`, read every line, and record the first two reads as
`answers.first_read_outcome` and `answers.second_read_outcome`. Find the
`worker result written` mark, count the reads after it that still returned the earlier
state (with the invalidation still empty, a copy stored before the mark keeps being served
until its age reaches your TTL), and write the count into the record. Read the two
counters in Prometheus, or from `/metrics`, and write them beside the probe's own counts.
Query them by job: `coldline_status_cache_hits_total{job="coldline-api"}`. The worker
process exports the same two names and never reads through the cache, so its series stays
at zero. Commit `docs/student/cache/probe.json` as written.

### Step 3 — Implement invalidation and prove a fresh read after the worker's update

Fill in `invalidate_exception_status`: call the adapter's `delete` for the exception id.
Run `poe start` so the worker image is rebuilt, then `poe cache-probe` again. From the
first read after the mark, record `answers.post_update_read_outcome` and
`answers.post_update_read_state`; count the reads after the mark that returned any
earlier state into `answers.stale_reads_after_update`. Commit the new
`docs/student/cache/probe.json`; this is the run the checks read.

### Step 4 — State the freshness window for recipient acceptance

Fill `answers.freshness_window` from your TTL and your probe runs together: which reads
may be stale, by how much at most, whether a recipient may act on the view, and why, in
Elena's terms, with one limit of what your single-host run proves. The enumerated and
numeric values are compared with a protected answer key after you submit.

## Commands

```shell
poe cache-probe     # one probe run against the running stack; writes docs/student/cache/probe.json
poe answers         # the static half: answer format, record markers, permitted-path boundary
poe cache-checks    # the assessed checks: both wiring points, the configuration, the probe output, the answers, /metrics, one live probe run
poe cache-contract  # both halves together; the check poe verify runs for this Task
poe verify          # the full public path
```

Start the stack per `README.md` first, and again after every change to the two wiring
points or the TTL, because the containers are built from your working tree. The probe
polls the store through `docker compose exec` into the `postgres` container, so Docker
must be reachable from the shell you run it in. With the invalidation still empty and a
long TTL, a run lasts until the stored copy expires, up to two minutes; expect
`poe verify` to take a few minutes. Keep any host-port override in place for every
command; the probe reads `COLDLINE_API_HOST_PORT` the same way the stack does.

## What the probe writes

Every field below is written by `poe cache-probe`, and the checks read it as written:

| Field | Meaning |
|---|---|
| `exception_id`, `status_url` | The reading the probe submitted and where it read its status |
| `reads` | One entry per read: `sequence`, `phase` (`before_update` or `after_update`), `offset_seconds` from the submission, `outcome` (`hit` or `miss`), `state`, and `age_seconds` of the copy served, from the route's two headers |
| `store_polls` | The states the probe saw in the store while waiting for the worker, with their offsets |
| `result` | The terminal state the worker wrote, the store's own `updated_at` as an offset, and when the probe saw it (the mark) |
| `first_read`, `second_read`, `post_update_read` | The outcome and state of the reads the answer sheet copies |
| `stale_reads_after_update` | How many reads after the mark returned a state other than the one the worker wrote |
| `hits`, `misses`, `counters` | The probe's own counts, and the two `/metrics` counters before and after the run, with the difference |
| `generator`, `digest` | The generator marker and a SHA-256 content digest over the rest of the file |

The two reads before the mark are made back to back inside a small time budget, before
the worker's provider (a quarter of a second per reading) can have finished; on a busy
machine the probe discards a reading whose two reads missed that budget and submits
another, and says so.

## What the checks verify

Each row of the lesson's Check-list maps to one or more checks:

| Check-list row | Check | What it looks at |
|---|---|---|
| `src/api/status_cache.py` asks the cache first, reads the store only on a miss, and stores an existing record with `status_ttl_seconds` from `config/cache.yaml` | `test_read_through_serves_a_hit_from_the_cache_without_reading_the_store`, `test_read_through_reads_the_store_on_a_miss_and_caches_the_record`, `test_read_through_stores_the_copy_with_the_ttl_from_the_configuration`, `test_read_through_does_not_cache_a_missing_record` | `read_exception_status` against an in-process cache and a store that counts its reads: a preloaded copy is returned as a hit with its age and no store read; an empty cache reads the store once, stores the record, and answers the next read as a hit; the stored copy's TTL equals `config/cache.yaml`; a missing record returns `None` and stores nothing |
| `config/cache.yaml` keeps its single key inside the published range, and `answers.status_ttl_seconds` equals it | `test_config_keeps_one_key_inside_the_published_range`, `test_recorded_ttl_equals_the_configured_ttl` | Exactly one key, a whole number from 5 to 120; the answer equals it |
| `src/worker/status_invalidation.py` deletes the cached copy for the exception the worker updated | `test_invalidation_deletes_the_cached_copy_for_the_updated_exception` | Two copies in an in-process cache; after the call, the updated exception's copy is gone, the other stays, and the adapter's `delete` was what removed it |
| `docs/student/cache/probe.json` is the unedited output of your last `poe cache-probe` run | `test_probe_output_is_the_unedited_output_of_a_probe_run`, `test_live_probe_agrees_with_the_committed_output_and_raises_the_counters` | The file parses, carries the generator marker, its digest recomputes over its content, and it holds the reads, the mark, and the copied fields; one live run repeats its five copied values |
| The three outcomes are each `hit` or `miss`, and the post-update state is one of the five states, all taken from that output | `test_recorded_outcomes_match_the_committed_probe_output` (and the schema) | `answers.first_read_outcome`, `second_read_outcome`, `post_update_read_outcome`, and `post_update_read_state` equal the probe output's `first_read`, `second_read`, and `post_update_read` |
| `answers.stale_reads_after_update` is a whole number of at least 0 counted from that output | `test_recorded_outcomes_match_the_committed_probe_output`, `test_probe_output_shows_a_fresh_read_after_the_workers_result` | The answer equals the output's count; and the output's first read after the mark is a miss returning the worker's terminal state, with no later read returning an earlier state |
| The two counters are exposed on the API's `/metrics` route and rose during the probe | `test_metrics_route_exposes_both_cache_counters`, `test_live_probe_agrees_with_the_committed_output_and_raises_the_counters` | Both names appear on `/metrics`; across one live probe run they rise by exactly the hits and misses that run made |
| `answers.freshness_window` holds every field from its allowed values and a non-empty note | `test_freshness_window_holds_allowed_values_and_a_note` (and the schema) | The enumeration, a whole number of at least 0, a boolean, and a note of at most 400 characters; the values themselves are compared with the protected answer key after you submit |
| The cache record replaces every template marker | `tests/contract/submission_validation.py` (`poe answers`) | `docs/student/task-3-10-cache-record.md` no longer contains `_Write your evidence here._` |
| The pull request modifies only the six permitted files | `tests/contract/submission_validation.py` (`poe answers`) and `test_submission_change_stays_within_the_permitted_diff` | The diff from the merge base with `main` against the six-file allowlist, with no directory prefix exempted |

The two checks marked `runtime` need the running stack; the rest are static and need no
Docker. `poe contract` skips this whole module because it is marked `assessed`;
`poe cache-checks`, `poe cache-contract`, and `poe verify` run it. A fresh checkout fails
the read-through checks except the missing-record one, the invalidation check, the
recorded-TTL check, every check that reads the probe output, the freshness-window check,
and the live probe check, because the two bodies, the TTL, the probe output, and the
answers are this Task's work; the configuration, missing-record, and `/metrics` checks pass
on the starter and are meant to.

## Student-editable paths

- `src/api/status_cache.py`
- `src/worker/status_invalidation.py`
- `config/cache.yaml`
- `docs/student/cache/probe.json`, written by `poe cache-probe` only
- `docs/student/task-3-10-cache-record.md`
- `submission.yaml`

That is the whole list. The cache adapter, the status route, the worker loop, the store,
the probe, the transport adapters, every test, and both workflows stay as supplied. Do not
add a second cache, a second key, or a background refresh. A probe output you edit by hand
fails the digest check; a sequence you disagree with is a reason to rerun, never to
retype. Before you push, run `git status` and `git diff --stat` against your merge base: if
anything besides the six files changed, the public check reports the boundary violation
rather than your work.

## What this local run does not prove

One Compose stack on one host, with one Redis and one PostgreSQL, is the whole measurement.
The probe sees the store's write and the cache's delete from outside, hundreds of
milliseconds apart at the closest; the gap between the store's commit and the delete is
real and this run does not measure it. A read replica would add its own lag before the
copy is even stored, and a managed Redis has failure modes a single container never
shows. Say which of these bears on your answer in the record and in
`answers.freshness_window.note`.
