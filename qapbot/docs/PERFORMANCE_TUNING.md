# Update-cycle performance: what actually binds, and what doesn't

Written after a 2026-09-03 CWL-season investigation in which two plausible-looking
"speed up the API layer" changes were deployed to the server-machine and measurably
did nothing. Read this before touching concurrency, throttle, or batch-size knobs.

## The one thing to internalise

**The update cycle is bounded by CPU on the server-machine, not by API throughput.**

The server-machine is a low-power 2-core NAS-class x86 CPU (~2 GHz, no SMT, RAM
ceiling in the single-digit GB). Python's GIL means the asyncio event loop gets
*one* of those two cores for response parsing, object construction and serialization.
Everything else — `asyncio.to_thread` work, DSM's own services — contends for what
is left.

So the ceiling on Phase 1 is roughly:

```
one core of a ~2 GHz low-power x86  /  per-clan Python work
```

which measured out at **~30 clans/s (median 32 ms/clan)**. No API-side setting can
move that number, because the API was never the queue.

## Evidence (PROD, 2026-09-03)

Two changes were deployed and measured from log timestamps:

| Change | Expected | Actual |
|---|---|---|
| `_FETCH_CONCURRENCY` 20 → 50 | ~2.5x Phase-1 throughput | 25.6 → **23.4 clans/s** (no change) |
| Wall-clock throttler, `throttle_limit` 80 | ~80 req/s sustained | peak ~53 req/s, median ~40 (no change) |

`ms/clan` across ten consecutive cycles spanning both changes: median **31.7**,
mean 35.2, range 24-66. Effectively invariant.

**The decisive measurement is Little's Law.** In-flight = throughput x latency:

| concurrency | throughput | latency/clan | implied in-flight | semaphore |
|---|---|---|---|---|
| 20 | 25.6 clans/s | 0.78 s | 20.0 | 20 |
| 50 | 23.4 clans/s | 2.14 s | 50.1 | 50 |

The semaphore was **100% saturated at both settings** and throughput still did not
rise. Therefore the extra 1.36 s/clan bought by raising concurrency was spent
queueing *inside the process* — on the GIL and the `to_thread` pool — not waiting on
the network. That is the signature to look for: if latency rises in exact proportion
to concurrency while throughput stays flat, the bottleneck is local.

A second, independent calibration: `[CATEGORIZE-TIMING]` does zero API work and takes
a rock-steady **7.2-7.8 s for ~458k clans every single cycle** (~16 us/clan of pure
Python). That figure alone sets a hard per-cycle floor and tells you how fast this
box executes Python.

## Knobs that do NOT help (and why)

- **`_FETCH_CONCURRENCY`** (`QapBot.py`): raising it adds latency and in-flight memory,
  nothing else. Keep at 20. The comment at the call site records the measurement.
- **`throttle_limit` / the throttler**: the configured 100 req/s was never reached;
  actual peak is ~53. Note there *was* a genuine bug here — upstream
  `coc.BatchThrottler` measures its window with `time.process_time()` (CPU seconds),
  not wall-clock, so `throttle_limit` never meant req/s. `WallClockBatchThrottler` in
  `QapBot.py` fixes that. But fixing it changed no cycle time, because the gate was
  never what we were queueing on. Keep the fix (it makes the setting honest and will
  matter on better hardware); do not expect throughput from it.
- **Anything that adds parallelism in front of a CPU-bound stage.** On 2 cores with a
  GIL, that is strictly negative.

## Where the real wins are

Ordered by measured cost per cycle. All of these *remove work* rather than
parallelising it — that is the only lever that moves a CPU-bound system.

1. **Per-clan temp-file writes (~2,700/cycle, ~1 per fetched clan).**
   `save_war_object()` serializes and writes a war JSON for every polled clan with no
   content dirty-check, so a clan whose war state has not changed still pays full
   JSON serialization (GIL-held) plus a disk write. A cheap hash/mtime comparison that
   skips byte-identical rewrites would cut the largest single block of per-cycle CPU.
2. **`[CATEGORIZE-TIMING]`: 7.2-7.8 s x ~288 cycles/day = ~35-37 min/day of pure CPU**
   spent re-scanning all ~458k clans. Incrementalising this (only re-evaluate clans
   whose `last_war_update` could plausibly have crossed a threshold) is a large,
   self-contained win.
3. **`to_thread` pool size.** `asyncio.to_thread` uses the default executor, sized
   `min(32, os.cpu_count() + 4)` — which on a 2-core box is **6 threads**, shared by
   every `to_thread` call site (33 in `QBhelperfunctions.py` alone: temp writes, sync
   SQLite reads). These threads are blocked on *disk*, not CPU, so the `cpu_count`
   heuristic is wrong for this workload. An explicit `set_default_executor()` of ~16-24
   would decouple I/O waits from the core count. Low risk, worth measuring.
4. **Phase-3 tail (17-36 s/cycle)** — file moves and DB flush; already batched, but it
   scales with the temp-file population that item 1 governs.

## Rule of thumb

Before changing a performance knob, predict the effect **and** state which measurement
would falsify it. Then measure from log timestamps, not from the setting being present
in the log. `[PHASE-1] ... (max N concurrent)` only proves the value was read; the
initial-burst count and `ms/clan` prove whether it did anything.
