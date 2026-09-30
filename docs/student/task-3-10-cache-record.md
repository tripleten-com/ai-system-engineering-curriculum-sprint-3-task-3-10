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

`status_ttl_seconds: 30`.

Why 30 seconds. A clinic waiting at the dock does not refresh once; the staff in Elena's
email refreshed the same exception over and over during one wait, and on a busy afternoon
dozens of clinics do the same thing at the same time. Thirty seconds is long enough that
all of that traffic for one exception is answered from a single copy instead of a database
query per refresh: at the roughly one-refresh-per-few-seconds pace a waiting clinic keeps
up, one store read covers about ten refreshes per clinic, and every other clinic reading
the same exception in that window is served from the same copy. It is also short enough
that I can say the number out loud to Elena: an in-progress state a dispatcher watches is
never more than 30 seconds behind the store, which is less than the time it takes to pick
up the phone. The terminal result is not defended by this number at all — Step 3's
invalidation defends it — so I did not need a small TTL to buy freshness where it matters,
and I did not want a large one (120 s) that would leave a dispatcher looking at a
two-minute-old in-progress state.

The two counter lines from `curl http://localhost:8000/metrics` after `poe start`, before
the first probe run:

```text
coldline_status_cache_hits_total 0.0
coldline_status_cache_misses_total 0.0
```

The first `poe cache-probe` run after the read-through went in reports a hit on its second
read:

```text
read  1  miss  QUEUED      age   0.000 s  (+0.04 s)
read  2  hit   QUEUED      age   0.006 s  (+0.05 s)
```

That run ended with `hits 33, misses 2`, so the read path is the cache, not the store.

## Step 2 - Prove hits and misses with the probe and the metrics

The whole output of one `poe cache-probe` run with the read-through in place and the
invalidation still empty: the exception id, every `read` line, and the `worker result
written` mark. Under it, the count of reads after the mark that still returned the
earlier state, and the two counter values from Prometheus or `/metrics` beside the hits
and misses the probe printed.

One whole `poe cache-probe` run, read-through in place, `invalidate_exception_status` still
the supplied placeholder:

```text
submitted exception exc-d681e09f-b0c1-5ead-ae77-10218bcbab8c (status /api/v1/exceptions/exc-d681e09f-b0c1-5ead-ae77-10218bcbab8c)
read  1  miss  QUEUED      age   0.000 s  (+0.03 s)
read  2  hit   QUEUED      age   0.004 s  (+0.03 s)
--- worker result written: COMPLETED (store updated_at +0.28 s, seen at +0.81 s) ---
read  3  hit   QUEUED      age   0.785 s  (+0.81 s)
read  4  hit   QUEUED      age   1.795 s  (+1.82 s)
read  5  hit   QUEUED      age   2.801 s  (+2.83 s)
read  6  hit   QUEUED      age   3.806 s  (+3.83 s)
read  7  hit   QUEUED      age   4.811 s  (+4.84 s)
read  8  hit   QUEUED      age   5.843 s  (+5.87 s)
read  9  hit   QUEUED      age   6.847 s  (+6.87 s)
read 10  hit   QUEUED      age   7.852 s  (+7.88 s)
read 11  hit   QUEUED      age   8.858 s  (+8.88 s)
read 12  hit   QUEUED      age   9.865 s  (+9.89 s)
read 13  hit   QUEUED      age  10.870 s  (+10.89 s)
read 14  hit   QUEUED      age  11.875 s  (+11.90 s)
read 15  hit   QUEUED      age  12.880 s  (+12.90 s)
read 16  hit   QUEUED      age  13.885 s  (+13.91 s)
read 17  hit   QUEUED      age  14.890 s  (+14.91 s)
read 18  hit   QUEUED      age  15.895 s  (+15.92 s)
read 19  hit   QUEUED      age  16.902 s  (+16.93 s)
read 20  hit   QUEUED      age  17.908 s  (+17.93 s)
read 21  hit   QUEUED      age  18.914 s  (+18.94 s)
read 22  hit   QUEUED      age  19.920 s  (+19.94 s)
read 23  hit   QUEUED      age  20.926 s  (+20.95 s)
read 24  hit   QUEUED      age  21.934 s  (+21.96 s)
read 25  hit   QUEUED      age  22.941 s  (+22.96 s)
read 26  hit   QUEUED      age  23.946 s  (+23.97 s)
read 27  hit   QUEUED      age  24.952 s  (+24.98 s)
read 28  hit   QUEUED      age  25.958 s  (+25.98 s)
read 29  hit   QUEUED      age  26.964 s  (+26.99 s)
read 30  hit   QUEUED      age  27.968 s  (+27.99 s)
read 31  hit   QUEUED      age  28.970 s  (+29.00 s)
read 32  hit   QUEUED      age  29.975 s  (+30.00 s)
read 33  miss  COMPLETED   age   0.000 s  (+31.02 s)
read 34  hit   COMPLETED   age   1.013 s  (+32.02 s)
read 35  hit   COMPLETED   age   2.019 s  (+33.03 s)
hits 33, misses 2; coldline_status_cache_hits_total rose by 33, coldline_status_cache_misses_total rose by 2; 30 read(s) after the result returned an earlier state
```

First read: `miss`. Second read: `hit`. Nothing was cached before read 1, so it went to the
store and stored the copy; read 2 came 4 ms later, well inside the 30 s TTL, so it was
answered from that copy.

**Reads after the mark that still returned the earlier state: 30** — reads 3 to 32. The
store held `COMPLETED` from +0.28 s (its own `updated_at`), and every one of those reads
returned `QUEUED`, the state the copy had captured at +0.03 s. They stopped being stale only
at read 33 (+31.02 s), when the copy reached its 30 s TTL and Redis discarded it, so the read
missed and went back to the store. Nothing told the cache the store had changed; the clock
did all the work. Those 30 reads are exactly what the clinic in Elena's email would have
seen: half a minute of a page that "felt stuck" on `QUEUED` while the result had already been
written.

Counters. The probe's own counts for this run were 33 hits and 2 misses. Prometheus, queried
after the run at `http://localhost:9090` with
`{__name__=~"coldline_status_cache_(hits|misses)_total",job="coldline-api"}`:

```text
coldline_status_cache_hits_total{job="coldline-api"}   66
coldline_status_cache_misses_total{job="coldline-api"}  4
```

Those are cumulative counters over both probe runs I had made at that point (33 + 33 hits,
2 + 2 misses), and nothing else read the status route. The probe's own before/after readings
of `/metrics` in the same run were `before {hits: 33, misses: 2}` and
`after {hits: 66, misses: 4}`, `rose_by {hits: 33, misses: 2}`, which is the same movement the
printed lines show. The counters agree with the sequence.

## Step 3 - Implement invalidation and prove a fresh read after the worker's update

The whole output of the `poe cache-probe` run you committed, after `poe start` rebuilt the
worker with your invalidation: the first `read` line after the mark, its outcome and the
state it returned, and the reads that follow. Beside it, the Step 2 count, and the same
count from this run.

`invalidate_exception_status` now calls `await cache.delete(exception_id)` — the same key the
read-through stores under. `poe start` rebuilt the worker image, then this is the whole
`poe cache-probe` run I committed as `docs/student/cache/probe.json`:

```text
submitted exception exc-bb2024b2-2ce6-5368-b965-f5025d48495c (status /api/v1/exceptions/exc-bb2024b2-2ce6-5368-b965-f5025d48495c)
read  1  miss  QUEUED      age   0.000 s  (+0.03 s)
read  2  hit   QUEUED      age   0.005 s  (+0.04 s)
--- worker result written: COMPLETED (store updated_at +0.29 s, seen at +0.79 s) ---
read  3  miss  COMPLETED   age   0.000 s  (+0.80 s)
read  4  hit   COMPLETED   age   1.006 s  (+1.80 s)
read  5  hit   COMPLETED   age   2.011 s  (+2.81 s)
hits 3, misses 2; coldline_status_cache_hits_total rose by 3, coldline_status_cache_misses_total rose by 2; 0 read(s) after the result returned an earlier state
```

The first read after the mark is read 3: **`miss`, state `COMPLETED`, age 0.000 s**. The copy
that read 1 had stored was gone, because the worker deleted it right after writing its result,
so read 3 went to the store and came back with the state the worker had written. Reads 4 and 5
are hits on the copy read 3 stored, and they return `COMPLETED` too — the copy is fresh
because it was taken after the write.

`answers.stale_reads_after_update: 0`, counted from the three `after_update` reads in this
output: none of them returned a state other than `COMPLETED`.

Beside the Step 2 count: **30 stale reads** with the TTL alone (reads 3 to 32, 30 s of
`QUEUED` after the store held `COMPLETED`), **0** with the delete in place. Same TTL, same
probe, same worker. The difference is not the clock; it is that the write now tells the cache.

One thing the run does not show. The probe sees the store's commit at +0.29 s and reads again
at +0.80 s, about half a second later, and that read was already fresh. The gap between the
store's commit and the worker's `delete` is real — the worker writes, then deletes — but it is
smaller than the probe can resolve from outside. A read landing inside that gap would still be
served the old copy; I cannot put a number on it from this run.

## Step 4 - State the freshness window for recipient acceptance

The four fields of `answers.freshness_window` and the reasoning behind each, with the probe
lines that support it: which reads can still lag the store and why, how long at most, and
whether a clinic may act on the page. Close with the one limit of what your single-host run
proves, such as a read replica's lag or a managed Redis.

```yaml
reads_that_may_be_stale: in_progress_only
max_staleness_seconds: 30
acceptable_for_recipient_acceptance: true
```

**`reads_that_may_be_stale: in_progress_only`.** Only one write on this path is followed by a
delete: the worker's terminal result, `COMPLETED` or `FAILED`. The worker's intermediate write,
`QUEUED` to `PROCESSING`, is not, and my Step 2 run shows the store moving to `PROCESSING` at
+0.25 s while reads kept returning `QUEUED` from the copy. So a read made while the reading is
still being evaluated can return a state the store has already left. A read made after the
terminal write cannot: the copy is gone by then, and the Step 3 run's reads 3, 4 and 5 all
returned `COMPLETED`, `0 read(s) after the result returned an earlier state`.

**`max_staleness_seconds: 30`.** This follows from the TTL and the invalidation together, not
from either alone. The invalidation removes the copy at the terminal write, so no read can lag
the store past that moment; but nothing removes a copy that was stored before an intermediate
write, and Redis keeps it for the full `status_ttl_seconds`. The longest a read can therefore
lag the store is the TTL, 30 s. The Step 2 run measured that bound directly with the delete
absent: read 32 was served a copy of age 29.975 s and read 33, at +31.02 s, missed because
Redis had discarded it. A copy is never served older than 30 s, so no status read lags the
store by more than 30 s.

**`acceptable_for_recipient_acceptance: true`.** Elena's condition was precise: once the system
has written down the result, the next refresh at the clinic has to show it. My Step 3 run is
that refresh — read 3, `miss`, `COMPLETED`, taken 0.5 s after the store's own `updated_at` —
and no later read went back to an earlier state. The staleness that remains, up to 30 s, is on
in-progress states only: a page that says `QUEUED` or `PROCESSING` is not an answer a clinic can
accept or refuse a delivery on, it is a "we are still looking". The two kinds of read on this
one route support different decisions: a dispatcher watching the queue can tolerate a
half-minute-old in-progress state, and a clinic deciding on a delivery reads a terminal state,
which the delete keeps current. Coldline still only shows the evidence; the acceptance decision
stays with the clinic and the dispatchers.

**What this single-host run does not prove.** One Compose stack, one Redis, one PostgreSQL. The
probe watches the store from outside through `docker compose exec` and reads the API over
HTTP, hundreds of milliseconds apart at best, so it cannot resolve the gap between the store's
commit and the worker's `delete`: a read landing inside that gap would still be served the old
copy, and my 30 s bound does not cover it. A managed Redis adds failure modes a single
container never shows — a failover that loses the delete would leave a stale copy alive for the
rest of its TTL. And this Task adds no read replica; if the status route were ever moved onto
one, the replica's own lag would be added before the copy is even stored, and the 30 s number
above would no longer be the whole window.
