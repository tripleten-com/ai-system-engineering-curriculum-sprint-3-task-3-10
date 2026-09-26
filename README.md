# Coldline Task 3.10 — Optional Task 10: Read cache and freshness window

This checkpoint is the complete, settled Coldline platform from Task 3.7, with one read path
changed on it: the exception status view, `GET /api/v1/exceptions/{exception_id}`, that
dispatchers and receiving clinics refresh while a reading is being evaluated. Today every
read of that route goes to PostgreSQL, the store. This Task puts a short-lived cache in
front of it, in the Redis role the stack already runs: a supplied adapter, a supplied
configuration file with one key, two supplied functions with TODO bodies that the route
and the worker loop already call, and a supplied probe that reads the status around the
moment the worker writes its result. You fill the two bodies, choose the TTL, commit the
probe's output, and record the staleness a recipient may accept. This Task is optional.

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/tripleten-com/ai-system-engineering-curriculum-sprint-3-task-3-10/tree/main)

## Start the system

Prerequisites are Python 3.12, git, and Docker with Compose v2. The supplied bootstrap supports
macOS arm64/x86-64, Windows x86-64, and Linux x86-64/aarch64, and installs pinned uv 0.11.8
under `.tools/bin`. If your computer cannot run the stack locally, use the Codespaces button
above.

On macOS and most Linux distributions the interpreter is `python3`; substitute it wherever these
commands say `python`.

```shell
python infra/scripts/bootstrap.py
./.tools/bin/uv sync --frozen
./.tools/bin/uv run --frozen poe preflight
./.tools/bin/uv run --frozen poe start
./.tools/bin/uv run --frozen poe ready
```

PowerShell and POSIX wrappers are available under `infra/scripts/`. After uv is on `PATH`, the
shorter `uv run --frozen poe <task>` form works.

| Service | Local URL | Purpose |
|---|---|---|
| API | `http://localhost:8000` | Submit exception workflows and retrieval queries; `/metrics` lists the two cache counters |
| Grafana | `http://localhost:3000` | Use the focused diagnostics dashboard |
| Prometheus | `http://localhost:9090` | Query bounded metrics, including the two cache counters, and inspect the deployed alert rule |
| Alertmanager | `http://localhost:9093` | Inspect firing and resolved alerts |
| Jaeger | `http://localhost:16686` | Inspect local traces; the cache adapter's `get`, `set`, and `delete` each leave a span |
| LocalStack S3/SQS | `http://localhost:4566` | Inspect the emulated object-storage and queue endpoint |

Each of these ports can be overridden by setting the matching `COLDLINE_API_HOST_PORT`,
`COLDLINE_GRAFANA_HOST_PORT`, `COLDLINE_PROMETHEUS_HOST_PORT`, `COLDLINE_ALERTMANAGER_HOST_PORT`,
`COLDLINE_JAEGER_HOST_PORT`, or `COLDLINE_LOCALSTACK_HOST_PORT` environment variable in your shell
environment or a local `.env` file (copy `.env.example`) if a default collides with something
already running on your machine. Keep the override in place for every `poe` command;
`poe cache-probe` reads `COLDLINE_API_HOST_PORT` the same way for its reads.

PostgreSQL, Redis, worker metrics, and OTLP remain inside the Compose network. Codespaces uses the
same `compose.yaml` and keeps every forwarded port private. Redis carries this Task's status
cache: both composition roots now open a client to it through `COLDLINE_REDIS_URL`, the API
to read and store copies, the worker to delete them.

## Command path

For this Task, run the supplied commands in this order:

```text
poe start
poe ready
poe cache-probe
poe verify
```

Between `poe cache-probe` and `poe verify` sit the four Steps of the lesson: the read-through
and the TTL, then `poe start` and a probe run; the invalidation, then `poe start` and the
probe run you commit; and the freshness window in `submission.yaml` and the record. Run
`poe start` again after every change to the two wiring points or the TTL: Compose builds
the API and worker images from your working tree, and `poe restart` does not rebuild.

| Command | Use |
|---|---|
| `poe cache-probe` | Submit one reading, read its status twice before the worker can have finished, wait for the worker's result by polling the store and mark that point, then read again until a read returns the worker's state plus two more. Print one line per read (hit or miss, the state, the age of the copy served) and the counters' movement; write the same sequence to `docs/student/cache/probe.json` with a generator marker and a content digest, replacing the previous run. Never edit that file |
| `poe answers` | The static half of this Task's own check: the answer sheet's format, the cache record's template markers, and the diff from your merge base against the six permitted files |
| `poe cache-checks` | The assessed checks: the read-through and the invalidation against an in-process cache and store, the configuration's single key and range, the committed probe output's digest and its agreement with your answers, the freshness window's allowed values, and, against the running stack, the two counters on `/metrics` and one live probe run compared with your committed output |
| `poe cache-contract` | `poe answers` and `poe cache-checks` together; the check `poe verify` runs for this Task. Its live probe waits for the worker, so expect a few minutes |
| `poe verify` | The public student verification path: it starts the stack from your code, ingests the corpus, runs `poe cache-contract`, then the smoke tests, the end-to-end workflow, and the supplied student tests |
| `poe queue-contract`, `poe slo-contract`, `poe gate-contract`, `poe runbook-contract` | The inherited Task 3.3 through 3.6 checks over the settled checkpoint; still runnable, not part of this Task's verify path |
| `poe contract` | Check interfaces, boundaries, submissions, and repository structure |
| `poe smoke` | Check the initialized running platform |
| `poe e2e` | Run the external API-to-worker workflow |
| `poe student-tests` | Run the supplied tests under `tests/student/`; this Task permits no additions there |
| `poe dev-failure-lab`, `poe trigger-alert-load`, `poe verify-alert-recovery`, `poe inject-failure`, `poe redrive` | Inherited exercises from Tasks 3.3, 3.4, and 3.6, still runnable; not part of this Task |
| `poe restart` | Restart the existing API and worker containers **without rebuilding** |
| `poe stop` | Remove containers and the network, keeping named volumes |
| `poe reset` | Remove containers, the network, and local named volumes |

For Task 3.10, `poe verify` starts the stack from your working tree, ingests the supplied
corpus, runs `poe answers`, runs the cache checks (including one live probe run against the
stack), then the smoke tests and the end-to-end exception workflow through the cached status
route, and the supplied student tests. The inherited Task 3.3 through 3.6 checks are not in
this path; they were qualified against the settled checkpoint and remain runnable on their own.

## The cache, the two wiring points, and the probe

The cache adapter, `src/adapters/cache/redis_status_cache.py`, is supplied and protected. It
has three operations, each keyed by exception id: `get` returns the cached copy or nothing,
`set` stores a copy with a TTL (`ttl_seconds`, the number of seconds Redis keeps it before
discarding it), and `delete` removes the copy. Every `get` is counted once as a hit or a
miss on `coldline_status_cache_hits_total` and `coldline_status_cache_misses_total`, which
the API's `/metrics` route exposes from the first scrape. The worker process exports the
same two names and never reads through the cache, so in Prometheus query the API's job:
`coldline_status_cache_hits_total{job="coldline-api"}`.

`config/cache.yaml` holds one key, `status_ttl_seconds`; its comments publish the range, 5
to 120 seconds inclusive. The API reads and validates it at start-up, so a value outside the
range stops `poe start` rather than surfacing on the first status read.

`src/api/status_cache.py` holds `read_exception_status(exception_id, cache, repository)`.
The status route calls it on every read and passes the exception id, the cache adapter, and
the exception repository; it answers with the record and two headers, `X-Coldline-Cache`
(`hit` or `miss`) and `X-Coldline-Cache-Age-Seconds`, which the probe reads. The supplied
placeholder asks the cache, never stores anything, and reads the store, so every read is a
miss until Step 1 fills the body in. `src/worker/status_invalidation.py` holds
`invalidate_exception_status(exception_id, cache)`. The worker loop calls it right after
the use case writes a processing result, a completion or a failure alike, and before it
acknowledges the delivery; the intermediate `QUEUED` to `PROCESSING` write is not
followed by a call. The supplied placeholder does nothing until Step 3.

`poe cache-probe` needs Docker reachable from your shell: it polls the store for the
worker's result through `docker compose exec` into the `postgres` container, so the mark
it prints is the store's own write, not a guess from the cached view. With the
invalidation still empty, a run lasts until the copy stored before the mark expires, up to
your TTL. See [the contract](docs/student/task-3-10-contract.md) for every field the
probe writes.

## Folder map

```text
repository root/
├── config/              Retrieval configuration from Sprint 2, and this Task's cache.yaml
├── docs/                Student guidance, public contracts, and fidelity notes
│   ├── contracts/       Machine-readable public contracts
│   ├── fidelity/        Local-runtime boundary notes, including the settled JobQueue record
│   ├── architecture/    Supplied vector engine technical profiles, in prose
│   ├── retrieval/       Supplied retrieval pipeline reference
│   └── student/         This Task's contract and cache record, the supplied Task 6 runbook,
│                        and cache/, where poe cache-probe writes its output
├── infra/               Local setup and runtime configuration
│   ├── cache/           The supplied probe
│   ├── containers/      The API and worker Dockerfiles, with the build identity arguments
│   ├── observability/   Prometheus, Alertmanager, and Grafana configuration
│   ├── release/         The supplied Task 3.1 release manifest, unchanged
│   ├── corpus/          Supplied synthetic corpus, query set, and designated investigation
│   ├── judge/           Supplied cached judge evidence and its provenance record
│   ├── profiles/        Supplied engine and emulator profiles, and their provenance record
│   └── postgres/        Database initialization and the migration baseline stamp
├── loadtest/            Supplied traffic profile and provider-latency harness
├── migrations/          Alembic environment, revision template, and revisions
├── src/
│   ├── api/             HTTP application code, the retrieval and document paths, composition,
│   │                    and this Task's read-through wiring point
│   ├── worker/          Background application code, the dead-letter depth poller, and this
│   │                    Task's invalidation wiring point
│   ├── domain/          Shared domain code, contracts, the failure taxonomy, service, repository,
│   │                    and status cache contracts
│   ├── ports/           Application interfaces
│   └── adapters/        Technology-specific implementations, including the Redis status cache
└── tests/
    ├── unit/            Isolated behavior checks
    ├── benchmark/       Supplied evaluation harness, metrics, and adoption policy
    ├── contract/        Interface, retrieval, and repository checks, and this Task's cache checks
    ├── diagnostics/     Supplied stage inspector
    ├── doubles/         Supplied deterministic test doubles
    ├── failure/         Supplied failure-lab and exercise scripts from Tasks 3.3, 3.4, and 3.6 — not this Task's work
    ├── student/         Supplied student tests; no additions in this Task
    ├── smoke/           Running-platform checks
    └── e2e/             Supplied workflow tools and checks
```

## Overview

Use the Optional Task 10 lesson (Task 3.10 in this repository) to decide what to do. This
README covers local setup and repository orientation.

1. `README.md` — local setup, commands, and permitted changes.
2. [`docs/student/task-3-10-contract.md`](docs/student/task-3-10-contract.md) — what this Task
   assesses and who assesses it, the four Steps, the commands, every field the probe writes,
   the mapping from the lesson's Check-list to each check, and the six permitted paths.
3. [`src/api/status_cache.py`](src/api/status_cache.py) and
   [`src/worker/status_invalidation.py`](src/worker/status_invalidation.py) — the two wiring
   points; each docstring names the arguments it is passed.
4. [`config/cache.yaml`](config/cache.yaml) — the one key; its comments publish the range.
5. [`docs/student/task-3-10-cache-record.md`](docs/student/task-3-10-cache-record.md) — the
   record template, one section per Step; replace every marker with your own evidence.
6. [`src/adapters/cache/redis_status_cache.py`](src/adapters/cache/redis_status_cache.py) — the
   three operations and what `set` expects for the TTL.

The application source lives in five flat packages:

| Package | Responsibility |
|---|---|
| `api` | HTTP delivery, API use cases, the retrieval workflow, versioned routes, configuration, composition, and the status read-through |
| `worker` | Background processing, retries, the dead-letter depth poller, configuration, composition, and the status invalidation |
| `domain` | Provider-neutral contracts, state rules, identity, redaction, embedding, chunking, fusion, access constraints, failure classification, service, repository, and status cache contracts |
| `ports` | Exactly five visible application interfaces |
| `adapters` | PostgreSQL, pgvector retrieval, LocalStack SQS/DLQ, S3-compatible object storage, the Redis status cache, deterministic model, the resilient model-provider wrapper, logs, traces |

`src/api/bootstrap.py` and `src/worker/bootstrap.py` compose each process from its settings and
adapters, including the status cache. Process settings live in `src/api/config.py` and
`src/worker/config.py`.

## The five ports

Find the available interfaces in `src/ports/`. A port describes an application capability; an
adapter provides it using a concrete technology.

| Port | General responsibility |
|---|---|
| `ModelProvider` | Call an AI model service |
| `Retriever` | Look up relevant context or documents |
| `ObjectStore` | Store large binary objects or files |
| `JobQueue` | Publish and consume background work |
| `SecretProvider` | Read API keys and credentials |

The status cache is not a sixth port. Like `ExceptionRepository`, it is an internal
collaborator declared in `src/domain/status_cache.py`: it caches the store's answer rather
than providing a capability of its own. LocalStack SQS, with a bound dead-letter queue, still
carries `JobQueue`, unchanged from Task 3.3; see [JobQueue fidelity](docs/fidelity/JobQueue.md)
for what it does and does not prove.

## Test levels

| Level | Requires Compose | Main question |
|---|---:|---|
| Unit | No | Does one responsibility behave correctly, including failures? |
| Contract | Some | Do interfaces, schemas, paths, and dependency rules stay compatible? |
| Smoke | Yes | Did the complete local platform initialize and become observable? |
| E2E | Yes | Can an external client complete the supplied workflow? |

Contract checks marked `runtime` need the running stack, and checks marked `assessed` read your
work. `poe contract` skips both; `poe cache-checks` runs this Task's own module, whose static
checks exercise the two wiring points in-process and read the configuration, the probe output,
and the answers, and whose two runtime checks read `/metrics` and run the probe. A fresh
Task 3.10 checkout fails the read-through, invalidation, recorded-TTL, probe-output,
freshness-window, and live-probe checks, because the two bodies, the TTL, the probe output,
and the answers are this Task's work; the configuration, missing-record, and `/metrics`
checks pass on the starter.

## Submission checks

Run `poe verify` locally before opening your student pull request. Public GitHub CI repeats
the student checks, running `poe answers` first so a boundary violation fails fast, then
`poe start`, `poe ingest`, and `poe verify`. The enumerated and numeric fields of
`answers.freshness_window` are compared with a protected answer key after you submit on the
platform; the public checks confirm their format and their allowed values only. Follow the
Task lesson's submission policy: this Task is optional and gates nothing.

## Task boundary

Task 3.10 asks you to fill the two wiring points, choose the TTL, commit the probe output,
fill the answer sheet, and complete the cache record. The only student-editable paths are:

- `src/api/status_cache.py`
- `src/worker/status_invalidation.py`
- `config/cache.yaml`
- `docs/student/cache/probe.json`, written by `poe cache-probe` only
- `docs/student/task-3-10-cache-record.md`
- `submission.yaml`

Change only the two wiring points and the TTL. Keep the cache adapter, the status route, the
worker loop, the store, the probe, the transport adapters, `compose.yaml`, every test file, and
both workflows exactly as supplied; do not add a second cache, a second key, or a background
refresh. Never edit `docs/student/cache/probe.json` by hand: the check recomputes its digest,
and a sequence you disagree with is a reason to rerun. The public check compares the diff
from your merge base against the six permitted files and reports any other change as a
boundary violation.

### Student walkthrough

See **Optional Task 10: Read cache and freshness window** in your course platform for the
full walkthrough. In outline: read `docs/student/task-3-10-contract.md`, start the stack, fill
the read-through and set the TTL, run `poe start` and `poe cache-probe`, record the first two
reads and count the stale reads after the mark, fill the invalidation, run `poe start` and
`poe cache-probe` again, record the first read after the mark, fill the freshness window and
the record, check `git diff --stat` shows only the six permitted files, run `poe verify`, open
your pull request, and submit on the platform.

## Operational limits

This local system does not authenticate users, terminate TLS, or manage production secrets.
The Compose PostgreSQL password and the LocalStack access keys are local-only non-secret
credentials. Never place real credentials, personal data, or production records in this
repository.

Alertmanager here is configured with a "default" receiver that has no notification integration:
alerts are queryable through its own API but never sent anywhere real. Never add a webhook, email,
Slack, or paid integration; Sprints 1-4 are emulator-only and never call a hosted endpoint. Use
the Redis service in the supplied Compose stack; do not create a managed cache, a database
replica, or any paid cloud resource. Read replicas appear in this Task only as a sentence in
your note.

One Compose stack on one host, with one Redis container, is the whole measurement. The probe
sees the store's write and the cache's delete from outside, hundreds of milliseconds apart at
the closest, so the gap between the store's commit and the delete is not something this run
measures. A managed Redis has failure modes a single container never shows, and a read replica
would add its own lag before a copy is even stored.

Named volumes preserve local PostgreSQL, Redis, Prometheus, Alertmanager, Grafana, and Jaeger state
across `poe stop`; cached status copies expire on their own, and `poe reset-baseline` flushes
them. LocalStack object and queue contents are deliberately not persisted; the initializer
re-uploads the supplied corpus artifacts and re-provisions the queue on every start. The
`poe reset` command deletes the named volumes. This topology makes no backup, replication,
high-availability, disaster-recovery, capacity, latency-SLO, or availability claim beyond the one
alert Task 3.4 configures, the one CI gate Task 3.5 wires to it, and the one bounded recovery
Task 3.6's failure lab demonstrates.

See [JobQueue fidelity](docs/fidelity/JobQueue.md),
[ModelProvider fidelity](docs/fidelity/ModelProvider.md),
[ObjectStore fidelity](docs/fidelity/ObjectStore.md), and
[Retriever fidelity](docs/fidelity/Retriever.md) for the active adapter boundaries. The
[local runtime evidence](docs/fidelity/local-runtime.md) records the current measurement and its
qualification limits.
