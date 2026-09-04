# Update-cycle performance: what actually binds, and what doesn't

Written after a 2026-09-03 CWL-season investigation in which two plausible-looking
"speed up the API layer" changes were deployed to the server-machine and measurably
did nothing. Read this before touching concurrency, throttle, or batch-size knobs.

## The one thing to internalise

**The update cycle is bounded by CPU on the server-machine, not by API throughput.**

The server-machine is a low-power 2-core NAS-class x86 CPU (~2 GHz, no SMT, 10 GB RAM). Python's GIL means the asyncio event loop gets
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

## Follow-up measurements (2026-09-04)

**The to_thread pool change (item 3 above) worked.** At matched write volume — 523 writes
before, 525 after — the Phase-3 tail went **31.5 s -> 23.3 s** and per-write cost
**60.1 ms -> 44.3 ms**, about 26%. The falsifier did not fire, so those writes really are
disk-blocked and a bigger pool decouples them from the core count.

**`cores_busy` first readings.** One cycle measured `wall=92.2s cpu=92.3s cores_busy=1.00`
— one core pinned for 92 seconds straight. That confirms the GIL ceiling is real and
reachable, and that when it happens the aggregate CPU meter reads only ~50% on a 2-core
box, which is easy to misread as "not CPU-bound". But the other cycles measured 0.52-0.56,
and those ran only 193-420 clans — too few to even saturate the fetch semaphore. **The
heavy (2,000+ clan) regime has not been measured yet**; CWL wound down before the
instrumentation landed. Do not treat the CPU-bound story as settled for that regime.

**The categorize loop is NOT where its own wall time goes.** A faithful replay of the loop
against the real ~459K-row clan table costs **~0.15 us/clan** on a dev box; the
server-machine reports **~15.9 us/clan** (7.3 s). A ~5x slower CPU does not explain a 100x
gap, and `await asyncio.sleep(0)` yields measured at 0.3-1.8 ms for all 229 of them. So
optimising the loop body would likely have freed nothing. `[CATEGORIZE-TIMING]` now logs
`cpu=` and `cores_busy=` so the next heavy cycle answers this directly:

- `cores_busy` ~1.0 -> the loop really is computing; the body is worth optimising
- `cores_busy` well below 1.0 -> the elapsed time is the event loop servicing other tasks
  during the yields, and the work must be found somewhere else entirely

**Correction (measured same day):** `[CATEGORIZE-TIMING]` came back at
`cores_busy=0.99-1.00` — one core pinned for the loop's full 7.2-7.7 s. The loop **is**
genuinely CPU-bound and the "it's really the yields" theory above was wrong. The dev-box
profile that suggested otherwise timed only *fragments* of the loop (fast-reject, passive
block, crc32) and missed the per-clan work done for the 152K tracked clans; a faithful
full-loop replay costs 0.78 us/clan, 5x the fragment figure. The remaining dev/PROD gap is
consistent with the server-machine being **memory-latency bound** — walking 458K scattered
dicts through a 2 MB cache on single-channel DDR3L, where the CPU-clock ratio understates
the real penalty.

That distinction matters for *what* to fix: if the cost is dominated by touching 458K
scattered objects rather than by the arithmetic performed on each, the win comes from
**iterating fewer entries**, not from micro-optimising the body. Hence the tracked-clan
index (`track_war_updates=1` is 152,402 of 458,715 — a 67% cut) is worth doing after all,
and the passive-scan throttle (~28% of the loop, measured) is worth having correct.

**Lesson:** profile the *whole* unit of work, not the branches you happen to suspect.
Fragment timings under-measured this loop by 5x and nearly sent a real optimisation to the
bin.

**Second correction (the index was still the wrong instrument).** The tracked-clan index was
built, deployed, measured — and reverted. Like-for-like, both `passive_scan=skipped`:

| build | walks | wall |
|---|---|---|
| full walk | 462K clans | 6.526 s (n=5) |
| tracked index | 150K clans | 6.600 s (n=5) |
| index + memoised per-clan constants | 150K clans | **4.150 s** |

Walking 312K fewer entries saved *nothing*: the fast-reject path is essentially free (one
`dict.get`, a branch, `continue`), and materialising the index-driven item list costs about
what it saves. The entire win came from memoising two per-clan **constants** the tracked
path recomputed every cycle — a regex re-parse of coc.py's Timestamp repr (~63%) and
`crc32(tag) % jitter` (~31%) — plus hoisting an f-string that built a debug message with an
`isoformat()` call on every war-critical clan even at INFO level.

Note the trap in that first table: the initial reading compared a `scan=full` cycle that
also ran the passive scan against a `scan=indexed` cycle that skipped it, and credited the
index with a 15% win that actually belonged to the passive-scan throttle. **When comparing
cycles, hold every other flag on the line constant** — `[CATEGORIZE-TIMING]` prints
`passive_scan=` precisely so this is checkable.

**Generalised lesson:** "touch fewer objects" only pays when the objects you stop touching
were expensive. Measure the per-item cost of the population you intend to skip *before*
building machinery to skip it. Here the skipped population cost ~3.7 us/clan and the
retained one ~43.9 us/clan.

**Watch the cache key.** The memoisation above was first keyed on the raw
`coc.Timestamp` string, which embeds a `seconds_until=` countdown recomputed at every
`str()` — so it missed for every clan that had just been polled, i.e. exactly the ones it
existed to serve. Anything derived from a war object is volatile by default: see
`COPILOT_PITFALLS_COOKBOOK.md` Pitfall 55 before keying, hashing, or diffing on it.

## Rule of thumb

Before changing a performance knob, predict the effect **and** state which measurement
would falsify it. Then measure from log timestamps, not from the setting being present
in the log. `[PHASE-1] ... (max N concurrent)` only proves the value was read; the
initial-burst count and `ms/clan` prove whether it did anything.

## A cache whose TTL is shorter than its caller's own gate can never hit

`CoCClanCache` (`qapbot/coc_cache.py`) caches `coc.Clan` objects for 280 s soft / 600 s hard,
capped at 1500 entries. Its dominant caller is `fetch_clan_war_data()`, which sits behind a
**`last_checked_via_api` gate of 12 h** (30 min for role clans) held in the DB. That gate is
strictly longer than the cache's hard TTL, so a polled clan is *always* expired by the time
anything could ask for it again. The two layers do not compose — the outer one subsumes the
inner one entirely.

Measured over 12.4 h on the server-machine:

| | |
|---|---|
| API fetches | 56,070 |
| distinct clans | 55,609 |
| fetches per clan | **1.01** |
| clans fetched more than once | 24 |
| refetch gaps inside the 600 s hard TTL | **0** |

Median gap between two fetches of the same clan: **30 minutes**. Nothing came back inside
the TTL, so nothing could have been served from cache. Raising `MAX_COC_CLAN_CACHE_ENTRIES`
would change none of this: the cap is not what evicts these entries, the TTL is, and the
reuse interval is 3x the TTL at its *shortest*. At the cap the cache holds roughly
**100-130 MB** (measured 69-90 KB per cached `coc.Clan` — see the module docstring for why a
"lazy" clan object is not free).

The residual value is the interactive callers — Discord commands, `ui_clan_management`,
`web_bridge`, `guild_role_manager` — which *can* re-request a clan within 600 s. That
population is small, so if it is worth caching at all it is worth caching in something far
smaller than 1500 entries.

**Correction — the 1500 cap invalidated its own evidence.** The measurement above shows
*what the cache did*, not what the workload wants. Smart backdating deliberately schedules a
CWL clan on consecutive cycles around its war boundary (just before the war ends, then just
after), and that is the pattern this cache was introduced for. At a 1500 cap the Phase-1
flood evicted entries within seconds, so that pattern could never be served — a hit rate
taken there measures the eviction policy, not the access pattern.

The cap is now 20000, which cannot bind: occupancy is TTL-bounded at
`clans_per_cycle x (600s / cycle_period)` — ~6.1K entries at the measured 3,053 clans/cycle
and a 300s period, ~10.2K if cycles shorten to 180s. `get_clan()` counts
`hits`/`stale_hits`/`misses` **split by population** (protected vs the rest), and
`[COC-CACHE-CLEANUP]` reports both rates every cycle. A single blended rate would be useless
here: the two populations differ by three orders of magnitude in size, so the larger would
swamp the smaller.

**Read those two numbers before resizing anything, and do not act on a cache experiment
whose own configuration is what the experiment is measuring.**

**Generalised:** before adding or tuning a cache, find the *caller's own* dedup window. A
cache only earns its memory when something asks the same question twice inside its TTL.
