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

---

## Profiling Phase-1 (2026-09-04, build 10): profile the COLD cycle and the WARM cycle — they are different bottlenecks

`PROFILE_PHASE1=<n>` wraps Phase-1's `asyncio.gather` in `cProfile` for the next *n* cycles,
then self-disarms. Set it to **at least 2**. The first cycle after a restart and the steady
state are not the same workload with a colder cache — they are two unrelated problems, and
profiling only one leads to the wrong fix.

| | Cycle 1 (cold) | Cycle 2 (warm) |
|---|---|---|
| Wall (profiled) | 287.7s | 113.7s |
| `epoll.poll` calls | **840,029** | 7,565 |
| Largest item in the profile | ~418,000 SQLite round-trips | `simple_member()` O(n²), 33% of Phase-1 |

> **Read that last row as "largest in the profile", NOT "largest in reality".** Fixing the
> 418,000 round-trips bought **nothing** measurable — see "The trap" below. Keep that in mind
> for the `simple_member()` row too: it has not been verified against an unprofiled cycle
> either.

### The trap: cProfile's overhead is proportional to CALL COUNT, so it skews the *distribution*, not just the totals

Everyone knows to distrust profiled absolute seconds. The subtler error — made here, on
2026-09-04 — is then trusting the **distribution** and concluding "this is the dominant
cost". It carries exactly the same bias: cProfile charges ~1 µs per call, so code that is
*cheap per call but enormous in call count* is inflated far more than everything around it,
and floats to the top of the profile on that basis alone.

The SQLite storm was almost pure call count: 418,000 round-trips, several Python frames
each. Under cProfile it looked like the dominant cost of the cold cycle. Unprofiled, at
**matched load** (2,061 vs 2,050 clans), removing it moved Phase-1 from **68.5s to 66.0s**
— about 3.6%, inside cycle-to-cycle noise.

**How to avoid it:** a profile ranks *suspects*, it never confirms one. Before claiming any
profiled hotspot is a real bottleneck, confirm it against **two unprofiled cycles at matched
load** — the same clan count, and comparable `Temp writes` / `Finalization` counts from
`[CYCLE-SUMMARY]`. And beware the reverse trap when reading a whole-cycle delta: the cold
cycle in that same comparison went 190.9s → 150.0s, which looks like a huge win and is not
one — the older cycle's post-Phase-3 tail simply did 19 extra CoC API calls and twice the
`[ROLE-SYNC]` work. Attribute a delta to a phase before attributing it to a change.

### Fixed (correctness/robustness, NOT a speed-up): the cold-cycle SQLite storm

`persist_user("UNASSIGNED")` rewrote the entire 5,794-row unlinked-player pool on every
newly-discovered player — ~11,600 event-loop round-trips per save, ~36 saves per cold
Phase-1. Fixed by bulk-querying (`_ensure_clans_exist()`) and `executemany()`:
**17,383 → 13 round-trips**, 1337x, identical writes.

Worth keeping — it removes real work and stops the cost scaling with a pool that only grows
— but **do not cite it as a cycle-time optimisation.** It measured as no improvement.


### Reading a cProfile of the event-loop thread

Four things will mislead you if you don't correct for them:

1. **cProfile inflates wall time**, worst for high-call-count code (cycle 1 measured
   100.8 ms/clan vs ~29 unprofiled). Trust the *distribution*, never the absolute seconds,
   and never compare a profiled cycle against an unprofiled one.
2. **Each coroutine resumption counts as a call.** `ncalls` on any `async def` is inflated by
   its await count. Anchor real counts on C-level non-coroutines — `put_nowait`,
   `call_soon_threadsafe` — then cross-check the arithmetic closes.
3. **It captures the whole loop thread**, not just the code you wrapped. Anything else running
   on the loop in that window appears too.
4. **`asyncio.to_thread` work is out of scope entirely** — cProfile instruments the calling
   thread only. That is the right scope for GIL-bound work, not a gap.

### `epoll.poll` count is the highest-signal number in the file

Not its time — its **call count**. It equals the number of loop iterations. Two orders of
magnitude above normal means round-trip amplification: something is `await`ing per item over
a data-sized collection. That single number found the storm below; see
`COPILOT_PITFALLS_COOKBOOK.md` Pitfall 56 for the full pattern and the fix.

### Fixed: the cold-cycle SQLite storm

`persist_user("UNASSIGNED")` rewrote the entire 5,794-row unlinked-player pool on every
newly-discovered player — ~11,600 event-loop round-trips per save, ~36 saves per cold
Phase-1. Fixed by bulk-querying (`_ensure_clans_exist()`) and `executemany()`:
**17,383 → 13 round-trips**, 1337x, identical writes.

This also reframes what the CoC clan cache is *for*: its main value is not saving API calls,
it is **suppressing this storm** by keeping `_fetch_and_cache()` → `_update_clan_metadata()`
from running. See the cache-sizing discussion above and tracker #0094.

### Known, not yet fixed: `simple_member()` is O(n²) with an expensive constant

`cache_manager.py`'s `simple_member()` is **27.97s cumulative of a warm Phase-1's 83.5s
(33%)**. For each war member, both `find_best_opponent_attack()` and
`calculate_defensive_stars()` rescan *every* opponent's *every* attack — two full O(n²)
passes. Worse, `getattr(opp_member, "attacks", [])` hits a coc.py **property that rebuilds
its list on every access**: **2,795,399 calls** in one cycle, and most of the 9.77M `getattr`
calls (5.78s self time).

The fix is one pass building a `defender_tag → (total_stars, best_attack)` index before the
member loop, collapsing both scans to O(n) and evaluating `.attacks` once per opponent
instead of 2n times. Deliberately left as a separate change so its effect stays separable
from the storm fix.

## Discord unresponsiveness during cycles is gen-2 GC. All of it.

**Settled 2026-09-04 (build 11, unprofiled).** Across two cycles the `[LOOP-LAG]` watchdog
recorded 11 stalls over 500ms. Every one of them is a gen-2 garbage collection, matching to
within the watchdog's own 100ms probe resolution:

| `[GC-AUTO]` pause | `[LOOP-LAG]` stall |
|---|---|
| 0.536s | 0.54s |
| 0.660s | 0.69s |
| 0.731s | 0.74s |
| 0.881s | 0.96s |
| 0.983s | 1.05s |
| 1.198s | 1.25s |
| 1.700s | 1.74s |
| 1.297s | 1.27s |
| 1.550s | 1.57s |
| 1.814s | 1.91s |
| 2.441s | 2.42s |

**11 of 11.** That is identity, not correlation. `p95 = 0.046s` — between collections the
loop is fine. So the longstanding "bot goes unresponsive during update cycles" complaint is
**not** GIL contention, **not** blocking I/O, and **not** any of our own cycle code. It is
the collector, and no amount of `to_thread`-ing or yielding will touch it (GC holds the GIL;
see Pitfall 16).

This is longstanding, not a regression — `[GC-AUTO]` maxima per build: build 2 2.70s,
build 4 5.27s, build 9 5.82s, build 10 5.53s, build 11 2.44s. Several exceed Discord's **3s
interaction ACK deadline** outright.

### Why the existing mitigations don't cover it

The design already anticipates this: `gc.set_threshold(700, 10, 20)` and a startup
`gc.freeze()` (620,316 objects on PROD), plus a nightly `unfreeze → full collect → freeze`.
The gap is *timescale*. The nightly re-freeze exists because "every cycle promotes newly
created long-lived CACHE growth into gen-2, which the one-time startup freeze does NOT
cover" (Issue 3, 2026-08-08) — but the pauses above appeared **7 minutes after a restart**,
within the first two cycles. The growth that makes sweeps expensive happens *inside a
cycle*; a once-a-day release valve is ~24h too slow for it.

### FIXED (2026-09-04, build 12): collection moved onto our schedule

**Why the garbage exists at all — and why it cannot be designed away.** coc.py's war graph is
cyclic by construction: `WarAttack.war` and `WarClan._war` point back at the `ClanWar`. So
~2,050 wars x ~150 objects per cycle is *unavoidably* GC-only garbage — refcounting can never
free it. (This matches PROD's observed `collected=320,701` / `273,627` almost exactly.) The
lever is which sweep runs when, not whether one runs.

The policy, in three parts:

| When | What | Cost |
|---|---|---|
| Startup | `gc.freeze()` the loaded caches, then `gc.disable()` | one-off |
| **Every cycle** | `gc.collect(1)` in `_post_cycle_cleanup()` | **0.018s**, reclaims ~311K objects |
| **Nightly** | `unfreeze -> full gc.collect() -> freeze`, in the maintenance window | the only full sweep the process runs |

The per-cycle collect is young-generation only, and that is sufficient *because* automatic
collection is off: with nothing being promoted on CPython's schedule, the entire war
population is still young when the cycle ends, so a young-only collect reclaims all of it
without ever walking the long-lived heap. Measured at PROD shape over 14 cycles: **RSS flat
(116 -> 120 MB, no drift)**, 311,448 objects reclaimed every cycle, and the nightly full sweep
then found just **1,064 objects in 0.001s** — a safety net rather than the main event.

**The sleep window is not a safe place to hide a pause.** An early version of this fix put the
full collect there, reasoning that no cycle work is waiting. That is wrong: the bot is *idle*
during the sleep window, which is exactly when a user is most likely to be interacting with
it. Only the nightly maintenance window is genuinely free, because `db_maintenance_mode`
already blocks Discord commands. Relocating a stall is not the same as removing one.

**Freezing is the wrong tool for churn.** `gc.freeze()` is excellent for the large static
caches — it makes a full collect measurably free (0.056s -> 0.000s over 1M objects), which is
why the startup freeze exists. It is wrong for the per-cycle war population, which is rebuilt
every cycle: freezing that would make its garbage permanent, which is the pile-up the policy
exists to avoid.

Escape hatch: `GC_AUTOMATIC=1` restores CPython scheduling.

### The real fix: stop creating garbage that needs collecting (build 14)

Scheduling the collection better still left a **2.0s** stop-the-world pause per cycle
(508,769 objects at ~240K objects/s on this box — dev extrapolation had predicted 0.1s; the
70x gap is the same memory-latency wall documented for the categorize loop, not a CPU-clock
gap). Rescheduling a 2s stall is not removing it.

**Why the garbage exists.** coc.py's war graph is cyclic by construction:

| Back-reference | Points to |
|---|---|
| `WarClan._war` | the `ClanWar` |
| `ClanWarMember.war` / `.clan` | the `ClanWar` / its `WarClan` |
| `WarAttack.war` / `.member` | the `ClanWar` / its `ClanWarMember` |
| `ClanWarMember._best_opponent_attacker` | another `ClanWarMember` |

Nothing in a war graph is reachable-but-unreferenced, so **refcounting can never free one** —
every war survives until a sweep walks it. Measured on real `coc.ClanWar` objects: 132
sweep-only objects for a 15v15; PROD averages ~195/clan across ~2,600 clans, which is exactly
the 508,769 observed.

**The fix.** `release_war_object()` (`QBhelperfunctions.py`) severs those back-references once
the graph's last consumer is done, making it acyclic so it dies on the last reference drop and
never reaches the collector. Measured: sweep-only garbage **2,640 -> 0**, and build+drop got
*faster* (2.2ms -> 1.2ms per 20 wars), because immediate refcount release beats deferred
collection.

Called from Phase 3 in a `finally` around `process_clan_war_data()` — the last thing that
*navigates* the graph. The war object's full lifetime is: built in `fetch_clan_war_data()`,
returned inside `{'war_obj': ...}`, read for `.end_time` by the backdate check, then consumed
by Phase 3. Exactly two consumers, both verified.

**The safety contract** (pinned by `tests/unit/test_release_war_object.py`): scalar fields
(`state`, `end_time`, `start_time`, `team_size`) and `.clan` / `.opponent` survive — the
backdate check depends on it. What dies is *navigation*: `member.war`, `member.clan`,
`attack.war` and `attack.member` become `None`. **Add a consumer that walks upward from a
member or attack and you must move the call, not weaken it.** Only materialised state is
touched (`_cs_members` / `_attacks`, never the public lazy properties), so a war whose members
were never iterated is not built just to be torn down.

Those tests deliberately use real `coc.ClanWar` objects. The question under test is which
references coc.py *actually* holds; a mock would confirm whatever we assumed, and would keep
passing after a coc.py upgrade added a back-reference while the pause silently returned.

**The general lesson:** when a GC pause is the problem, the question "when should collection
run?" is usually the second-best one. Ask "why is this garbage collectable-only at all?"
first — an object graph that refcounting can free costs nothing to release, at any time.

### Also fixed: `gc.set_threshold(700, 10, 20)` was inoperative-to-harmful on Python 3.14

That call (2026-08-17) was written against Python <=3.11, whose default really was
`(700, 10, 10)`. **Python 3.12 raised the gen-0 default to 2000**, so on 3.14 it was silently
*lowering* gen-0 and tripling how often it ran — 1,214 gen-0 collections vs 425 for the same
workload — while buying nothing. And 3.13 replaced the three-generation collector with an
incremental one, so the gen-2 multiplier no longer gates full sweeps either (measured:
threshold2 of 20 vs 1000 produced the same gen-2 count). Both halves were dead.

**Rule:** never carry a `gc.set_threshold()` call across a Python upgrade without re-measuring.
The defaults *and* the collector have both changed since 3.11.
