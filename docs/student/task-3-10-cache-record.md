# Task 3.10 cache record

This record is the answer to Elena's question about how fresh a shipment status needs to
be. It is not graded by the automated checks; they read `submission.yaml`,
`config/cache.yaml`, your two wiring points, and `docs/student/cache/probe.json`. Replace
every italic placeholder line below with your own evidence; `poe verify` fails while any
placeholder remains. Paste the probe output exactly as `poe cache-probe` printed it, and
keep the outputs yours: every outcome, state, age, and count here comes from your own runs
on your own machine.

## Step 1 - Implement the read-through and choose the TTL

Your `status_ttl_seconds` and the reason for it, in Elena's terms: staff refreshing the same
exception during one wait on a dock, dozens of clinics doing so at once, and how old an
answer you are willing to say out loud. Then the two counter lines from
`curl http://localhost:8000/metrics` after `poe start`, and the line of your first
`poe cache-probe` run that reports a hit.

_Write your evidence here._

## Step 2 - Prove hits and misses with the probe and the metrics

The whole output of one `poe cache-probe` run with the read-through in place and the
invalidation still empty: the exception id, every `read` line, and the `worker result
written` mark. Under it, the count of reads after the mark that still returned the
earlier state, and the two counter values from Prometheus or `/metrics` beside the hits
and misses the probe printed.

_Write your evidence here._

## Step 3 - Implement invalidation and prove a fresh read after the worker's update

The whole output of the `poe cache-probe` run you committed, after `poe start` rebuilt the
worker with your invalidation: the first `read` line after the mark, its outcome and the
state it returned, and the reads that follow. Beside it, the Step 2 count, and the same
count from this run.

_Write your evidence here._

## Step 4 - State the freshness window for recipient acceptance

The four fields of `answers.freshness_window` and the reasoning behind each, with the probe
lines that support it: which reads can still lag the store and why, how long at most, and
whether a clinic may act on the page. Close with the one limit of what your single-host run
proves, such as a read replica's lag or a managed Redis.

_Write your evidence here._
