# Tracker #0009 — Memory consumption: root-cause analysis (2026-08-29)

Analysis of the 84 h PROD run started 2026-08-25 20:23 (log `QapBot started.`),
the two memory profiles taken 2026-08-29 08:47 / 09:04, and the full PROD data
snapshot restored to DEV.

Every figure below marked **measured** was obtained by loading the real PROD data on DEV and
reading an actual allocation figure (`tracemalloc`, or a `psutil` RSS delta) — not by re-running
the bot's own estimator. Where the two disagree, see the correction note in §2: an RSS delta is
only trustworthy when its baseline predates *everything* the structure will end up retaining.

---

## 1. There is no unbounded leak

Daily RSS over the run (all cycles, `[CYCLE-END] [MEMORY CONSUMPTION] RSS=`):

| date | min | max | avg | samples |
|---|---|---|---|---|
| 2026-08-25 (from 20:23) | 993 MB | 1222 MB | 1131 MB | 43 |
| 2026-08-26 | 1122 MB | 6776 MB | 4004 MB | 286 |
| 2026-08-27 | 5618 MB | 6721 MB | 6142 MB | 285 |
| 2026-08-28 | 5526 MB | 7126 MB | 6082 MB | 284 |
| 2026-08-29 (to 09:05) | 5884 MB | 6632 MB | 6254 MB | 108 |

The daily average is flat after the first day (6142 → 6082 → 6254 MB). The previous
run (2026-08-20 20:01 → 2026-08-25 20:23, five days) plateaued the same way at ~5 GB.

**The problem is the height of the plateau (~6 GB, reached within ~24 h of start), not a leak.**

## 2. What is actually resident — measured, not estimated

Loaded the real PROD data on DEV in a clean interpreter and measured RSS deltas:

| structure | entries | **measured** | profile reported | error |
|---|---|---|---|---|
| `temp_war_objects` (+ `temp_war_metadata`) | 31,113 | **1,195 MB** (~40 KB/entry) | 1,417 MB | +18 % |
| `temp_war_stats` | 31,113 | **449 MB** (~15 KB/entry) | 497 MB | +11 % |
| `clan_name_cache` | 447,354 | **263 MB** (617 B/entry) | 462 MB | **+76 %** |

Live cache content is about **1.9 GB**. The first post-start RSS reading was 993 MB (base
interpreter + discord.py + coc.py + matplotlib + aiohttp + DB pool, before the temp caches
fill). So the *explainable* steady state is about **2.8–2.9 GB**, against an observed floor
of 5.5 GB and peaks of 7.1 GB.

> **Correction (made while implementing the fixes).** `clan_name_cache` was first reported here
> as 126 MB. That measurement took its RSS baseline *after* the source DB rows were already in
> memory, so every string the cache retains was charged to those rows rather than to the cache.
> A `tracemalloc` window spanning the whole build — query included — gives **263 MB**, and that
> is the number that answers "how much of RSS is this cache". It does not change any conclusion
> below: 263 MB is ~4% of RSS, still far from the driver, and the fixed estimator now reports
> 249 MB against it (5% under) instead of 462 MB.

`temp_war_objects` reaching ~31 k entries is by design and is not the anomaly: the bot
polls every tracked clan within 22 h, and every poll that finds a non-`war_ended` war writes
the payload into the cache. 31,113 temp war files exist on disk (626 MB of JSON), so the
cache count simply mirrors the tracked clan base.

## 3. Root cause: 22 h re-check waves x per-fetch retention

### 3a. The 22 h interval creates self-perpetuating waves

`INACTIVE_CLAN_UPDATE_INTERVAL_HOURS = 22` aliases against the 24 h day: clans checked
together in one cycle all become due again exactly 22 h later, in one cycle. The waves are
visible in the logs and drift 2 h earlier each day:

```
2026-08-25 07:11 (3644 clans) -> 08-26 05:19 (4195) -> 08-27 03:21 (3971)
2026-08-25 11:48 (4206 clans) -> 08-26 09:48 (4087) -> 08-27 07:48 (4079)
```

Steady state only needs ~114 clans/cycle to cover 30 k clans in 22 h.
`_MAX_INACTIVE_PER_CYCLE = 5000` ([QapBot.py:1061](../QapBot.py#L1061)) lets a wave run
about 44x that rate.

### 3b. Each wave clan costs ~120–170 KB of simultaneously-held memory

Two independent retention paths, both **measured** against synthetic-but-realistic API payloads:

**(i) `fetch_results` holds every war until the last fetch returns.**
[QapBot.py:1220-1222](../QapBot.py#L1220-L1222):

```python
fetch_tasks = [fetch_single_clan(clan_tag, is_active) for clan_tag, is_active in clans_to_update]
fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
```

`_fetch_semaphore` bounds concurrent *HTTP*, not concurrent *retained results*.
`fetch_clan_war_data()` returns `{'war_obj': coc.ClanWar, ...}`, and `fetch_results` stays in
scope through Phases 2–4, leaderboards and Discord posting.

A `coc.ClanWar` object is only 2.3 KB itself — but `WarClan._iter_members` is an un-exhausted
generator expression closing over the raw `data["members"]` list, so each retained war pins
its whole raw API response: **168 KB measured** for a 50v50.

**(ii) `coc_clan_cache` has no size cap.**
[qapbot/coc_cache.py:50](../qapbot/coc_cache.py#L50) is TTL-only (600 s hard), swept once per
cycle by `clear_expired()`. During a wave the cache grows unbounded until the cycle ends.
Peak observed in the logs: `[COC-CACHE-CLEANUP] Removed 5007 expired entries`.

Measured cost of one cached `coc.Clan`, with the raw dict dropped and `.members` never touched:

| clan size | **measured** | class docstring claims |
|---|---|---|
| 36 members | **68.8 KB** | "~8-10 KB per clan (with full 50 members)" |
| 50 members | **90.4 KB** | (same docstring) |

Same `_iter_members` mechanism. 5,007 x ~75 KB is about **375 MB** in that cache alone, at peak.

### 3c. The arithmetic matches the logs

| cycle | clans fetched | delta RSS |
|---|---|---|
| 2026-08-26 05:28 | 3,831 | **+700 MB** |
| 2026-08-25 07:11 | 3,665 | **+586 MB** |
| 2026-08-26 05:19 | 4,216 | **+541 MB** |

`coc_clan_cache` (~375 MB) + retained `ClanWar` payloads (~1,500 in-war x ~170 KB = ~255 MB)
lands at 630–700 MB. The peaks then bake into RSS: quiet cycles give back only 10–40 MB each,
and the next wave arrives 22 h later — so the floor ratchets up over the first day and holds.

## 4. The memory report's own numbers are misleading

Worth fixing before the next profile is used to decide anything:

1. **`clan_name_cache` over-reported 76%** (462 MB vs 263 MB measured).
   `_estimate_dict_size_mb()` resets its `seen` set per entry, so the 8 shared key strings and
   the interned/singleton values are re-counted for all 447 k entries.
2. **`coc_clan_cache` under-reported by ~250x** (0.1 MB for 322 entries; real is ~22 MB, and
   ~375 MB at wave peak). `get_memory_usage_mb()`'s `_deep()` recurses via
   `hasattr(value, "__dict__")`, but every coc.py model class uses `__slots__` and therefore
   has **no** `__dict__` — verified: `hasattr(coc.Clan(...), "__dict__") == False`. The walk
   stops at the top-level object and returns `sys.getsizeof()` = 360 bytes for an entire clan.
   The 2026-08-22 "deepened it to walk each member's League/Icon/Badge sub-objects" fix never
   fires. Note that fixing only this — a `__dict__` + `__slots__` walk — still under-reports by
   3x, because the dominant cost is the raw API payload held in the `_iter_members` generator's
   frame, which no attribute walk can reach.
3. **`[GC OBJECT COUNTS]` is blind to the permanent generation.** `gc.freeze()` removes objects
   from `gc.get_objects()` entirely — verified: 1000 objects, 0 visible after `gc.freeze()`.
   Nightly maintenance logs `re-froze 1908052 object(s) into the permanent generation`, so the
   counts understate the heap by roughly that much for most of each day.
4. `temp_war_objects` (+18 %) and `temp_war_stats` (+11 %) are accurate enough to trust.

## 5. Recommended fixes, in order of expected effect

1. **Stream Phase-1 results instead of gathering them all.** Replace the single
   `asyncio.gather()` over up to 5,000+ clans with `asyncio.as_completed()` or a bounded worker
   queue, so each war payload is converted to the temp dict and released immediately rather
   than held until the slowest fetch returns. Chunking the gather into batches of ~250 is the
   smaller-diff variant.
2. **Cap `coc_clan_cache`.** Add a max-entries LRU bound (a few hundred covers the working set
   — it sat at 322 and 784 entries in the two profiles) and/or sweep expired entries during a
   long cycle rather than only at `[CYCLE-CLEANUP]`.
3. **Disperse the waves.** Jitter the per-clan next-check time (e.g. 22 h plus up to 2 h of
   random slack) so a wave spreads out instead of re-forming, and/or drop
   `_MAX_INACTIVE_PER_CYCLE` from 5,000 to something nearer the ~114/cycle the 22 h SLA
   actually requires (e.g. 500) — 5,000 only ever fires during a wave.
4. **Fix the three reporting bugs in section 4** so the next profile can be read at face value.

Not recommended: touching `clan_name_cache` (263 MB measured, ~4 % of RSS; and the 2026-08-21
investigation already established that pruning it is both a no-op and unsafe against the five
`ON DELETE CASCADE` FKs), or lowering `_MAX_TEMP_WAR_OBJECTS` (that cache is 1.2 GB of
legitimately-tracked live wars and never comes near its 100 k cap).


---

## 6. Implementation status (2026-08-29)

All four recommendations are implemented; `.un_tests.ps1` → **2729 pass**.

| # | Change | Where |
|---|---|---|
| 1 | Phase 3 clears each `fetch_results` slot as it consumes it, instead of holding the cycle's whole set of wars until after Phase 3B / notifications / leaderboards | `QapBot.py` |
| 2 | `coc_clan_cache` gained a 1500-entry FIFO size cap (`MAX_COC_CLAN_CACHE_ENTRIES`, `_evict_over_cap()`), plus `max_entries`/`evicted_by_cap` in `get_stats()` | `qapbot/coc_cache.py` |
| 3 | `compute_recheck_offset_seconds()` — stable `crc32(tag) % 7200` phase offset on the 22h re-check; `_MAX_INACTIVE_PER_CYCLE` 5000 → 1500 | `QapBot.py` |
| 4 | All three report bugs fixed (shared `seen`, `gc.get_referents()` walk, frozen-generation note) | `qapbot/QBdiscocmdshelper_admin_command.py`, `qapbot/coc_cache.py` |

### Deliberately NOT done: full Phase-1 streaming

The recommendation in §5.1 mentioned `asyncio.as_completed()` as the ideal form. It was not
taken, because the peak this would target is structural rather than incidental: Phase 2 needs
the complete `failed_clans` set before it runs, and Phase 3 needs the full result set after the
temp/archive pre-scan, so every result must exist simultaneously at the Phase-1/2 boundary
regardless of how they are collected. Chunking or streaming the gather changes when results are
*produced*, not how many are *retained* — the peak stays equal to the cycle's clan count.

Bounding that clan count (§5.3) is therefore what actually bounds the peak, and the slot-clearing
in §5.1 shortens the tail after it. Making the peak itself smaller would mean converting each
`coc.ClanWar` to its lightweight temp payload inside Phase 1 and reworking
`process_clan_war_data()` to accept that instead of the coc object — a real refactor of the
cycle's hottest path, with the war-file lifecycle invariants riding on it. Worth doing on its
own, not folded into a memory fix.

### What to check on the next PROD run

- `[THROTTLE]` should now appear regularly (it fired only twice in 84h at the old 5000 cap) with
  `throttle_backlog` draining to 0 within a few cycles rather than persisting.
- `War updates: N inactive clans` should lose its 3000-4200 spikes as the jitter disperses the
  waves; expect the spread to take ~2h (one jitter window) to establish after the restart.
- `[COC-CACHE-CAP]` should be rare-to-absent in steady state — it firing every cycle would mean
  1500 is too low for the real working set.
- The RSS floor is the number to watch. If it still settles near 5.5 GB with the waves gone,
  the remaining gap is allocator-level rather than wave-driven and needs a different fix.
