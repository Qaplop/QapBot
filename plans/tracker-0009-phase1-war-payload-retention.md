# Tracker #0009 follow-up — stop Phase 1 retaining `coc.ClanWar` objects

**Status:** deferred, not started.
**Do not start before 2026-09-11** — the 2026-09 CWL season runs 1st-10th September, and this
touches the update cycle's hottest path (`fetch_clan_war_data` → `process_clan_war_data`, the
war-file lifecycle). Refactoring that immediately before a CWL season is the wrong trade; CWL is
also exactly when the memory pressure this addresses is worst, so the *measurement* window right
after the season is the useful one anyway.

This is a critical change of the update cycle code. Do it with Opus and not with Sonnet!

**Origin:** §5.1 of `plans/implemented/tracker-0009-memory-analysis.md`, the one recommendation
deliberately left out of commit `280537a`. Read §3b of that document first — it has the measured
numbers this plan builds on.

---

## 1. Why the original §5.1 framing was wrong

§5.1 said "stream Phase-1 results instead of gathering them all — replace `asyncio.gather()`
with `asyncio.as_completed()` or a bounded worker queue".

**That would not have worked**, and the reason matters enough to record so nobody re-proposes it:

- Phase 2 (`process_orphaned_cwl_wars`) needs the **complete** `failed_clans` set before it runs.
- Phase 3 needs the full result set, and it runs after the temp/archive pre-scan.
- Therefore every result must exist simultaneously at the Phase-1/2 boundary, no matter how they
  are collected. `as_completed` changes when results are *produced*, not how many are *retained*.
  The peak stays structurally equal to the cycle's clan count.

The real problem is not *how many* results are held. It is **what each result holds**: a
`coc.ClanWar` whose un-exhausted `WarClan._iter_members` generator pins the entire raw API
response (~120-170 KB measured).

## 2. The finding that makes this cheap

`fetch_clan_war_data()` (QBhelperfunctions.py, ~L7511) **already calls
`CACHE.save_war_object(coc_war_obj, clan_tag)` inside Phase 1**. That call:

- writes the temp war JSON to disk, and
- stores the built payload dict in `CACHE.temp_war_objects[clan_tag]` (measured **~40 KB/entry**),
- and `CACHE.temp_war_metadata[clan_tag]` (state, start_time, end_time, filepath, war_tag, ...).

So by the time Phase 1 returns, a **lightweight payload of the same war already exists in CACHE**.
The `coc.ClanWar` carried in `fetch_results` is a second, 3-4x heavier copy of data the bot has
already parsed and stored.

For `preparation`/`in_war` clans — the ones that dominate a wave — carrying the payload instead
costs **zero additional memory**, because it is literally the same object `temp_war_objects`
already references.

### Everything Phase 3 actually reads off the coc object

Audited against `process_clan_war_data()` (QBhelperfunctions.py L7606-7768) and QapBot.py's
Phase-3 loop. The complete list is four things:

| Used | Where | Already in the payload dict? |
|---|---|---|
| `coc_war_obj.start_time` (raw str) | `raw_start`, L7660 | yes — `payload["start_time"]` |
| `coc_war_obj.attacks_per_member` | L7724 | yes — `payload["attacks_per_member"]` |
| `coc_war_obj.clan.members[*]` → `.tag`, `.name`, `.town_hall`, `.attacks[*].stars`, `.attacks[*].destruction`, `.best_opponent_attack.stars` | temp-stats build, L7728-7761 | yes — `payload["clan"]["members"][*]` carries `tag`, `name`, `townhall`, `attacks`, `bestOpponentAttack` |
| `war_obj.end_time` | QapBot.py in_war smart-backdating | yes — `payload["end_time"]`, and also `temp_war_metadata[tag]["end_time"]` |

`_opp_clan = getattr(coc_war_obj, 'opponent', None)` (L7659) is assigned and **never used** —
dead local, delete it.

Note there is already a parser for exactly this payload shape:
`QBcsvhandling._load_war_data_from_json(clan_tag, json_file_path=..., preloaded_raw_data=raw)`,
used by `load_all_temp_war_stats()` at startup to produce the same `temp_war_stats` structure.
Whether to reuse it or to keep `process_clan_war_data`'s own loop reading from the payload is
step 3's open question — see the risk note there.

## 3. Proposed change

### Step 1 — extract payload construction (no behaviour change)

In `cache_manager.save_war_object()`, the `payload = {...}` dict is built inline. Extract it to a
module-level `build_war_payload(coc_war_obj, clan_tag) -> Optional[Dict[str, Any]]` and have
`save_war_object()` call it. Ship this alone first and confirm byte-identical temp files against
a DEV cycle (`git stash`-diff a few written files, or hash them before/after).

### Step 2 — return the payload from Phase 1 instead of the coc object

`fetch_clan_war_data()` currently returns:

```python
return {'clan_tag': ..., 'war_obj': coc_war_obj, 'opponent_tag': ..., 'state': ...}
```

Change `war_obj` → `war_payload` (the dict from step 1). Add `end_time` explicitly so QapBot.py's
backdating never needs the coc object.

**The one real complication:** `save_war_object()` does not always run or always cache.
- It is inside `if my_clan and opp_clan:` — the `save_skip_no_clan` branch has no payload.
- It returns `False` on `skip_finalized` / `skip_stale`.
- For `war_ended` it writes the file but deliberately does **not** populate `temp_war_objects`
  (and evicts any stale entry) — see the comment at cache_manager.py ~L2986.

So do **not** read the payload back out of `CACHE.temp_war_objects` — that is a hidden coupling
that silently yields `None` for exactly the war_ended clans Phase 3 still has work to do for.
Build it explicitly via `build_war_payload()` and carry it in the returned dict. For
prep/in_war clans it is the same object already in the cache, so this stays free.

### Step 3 — read the payload in `process_clan_war_data()`

Replace the four `getattr(coc_war_obj, ...)` reads with dict access per the table in §2.
The member loop is the only non-trivial part: coc.py's `m.attacks[*].destruction` maps to the
payload's per-attack `destruction` key, and `m.best_opponent_attack.stars` to
`bestOpponentAttack`. **Verify the key names against a real temp file rather than from this
document** — the payload uses a mix of snake_case and camelCase (`opponentAttacks`,
`bestOpponentAttack`, `previousBestOpponentAttack` alongside `townhall`, `map_position`), and
that inconsistency is exactly where a silent zero-stars bug would hide.

### Step 4 — QapBot.py Phase 3

`war_data.get('war_obj')` → use the carried `end_time` directly. The
`_DT_RE.search(str(raw_end))` parse stays as-is (the payload stores the same
`datetime.datetime(...)`-style string).

The `fetch_results[_idx] = None` slot-clearing added in `280537a` stays — it is still correct and
still useful, just less load-bearing once each slot is 3-4x smaller.

## 4. Expected effect

Retention per polled clan drops from ~120-170 KB to ~40 KB, and to **~0 additional** for
prep/in_war clans (shared object with `temp_war_objects`). At the current
`_MAX_INACTIVE_PER_CYCLE = 1500`, the Phase-1/2 boundary peak drops from roughly 250 MB to
under 60 MB.

This is worth doing *even though* `280537a` already bounded the waves, because it makes the
per-cycle peak proportional to cheap data rather than to a redundant object graph — which is
what would let `_MAX_INACTIVE_PER_CYCLE` be raised back up if the 1500 cap ever turns out to
starve the 22h SLA during CWL.

## 5. Risks

1. **Silent stat corruption is the real danger, not a crash.** A mistyped payload key yields 0
   stars / 0 attacks rather than an exception, and it would flow into leaderboards and then into
   war history. Mitigation: before shipping, run both code paths over the same set of real temp
   files on DEV and assert the produced `temp_war_stats` dicts are equal — there are ~31k temp
   files in the DEV snapshot to test against, which is a far better corpus than any unit fixture.
2. **`state` normalisation.** `process_clan_war_data` compares `state in ('preparation','in_war')`
   against the coc enum stringified by the caller, while the payload stores its own normalised
   `state`. Confirm both agree for CWL wars specifically (`war_ended` vs `warEnded` vs
   `WarState.war_ended` — the codebase already has `.lower().replace('_','')` normalisation in
   two places, which is a hint that they historically did *not* agree).
3. **CWL fallback path.** `_find_active_cwl_war_for_clan()` can *replace* `coc_war_obj` after the
   first `save_war_object()` call and save again. The payload returned must be the one built from
   the final object, not the first.
4. **`build_war_payload` returning None.** The `save_skip_no_clan` branch must keep working —
   Phase 3 has to tolerate `war_payload is None` the same way it tolerates a failed fetch today.

## 6. Verification

- `.\run_tests.ps1` — 2729 passing at the time of writing.
- New test: parity between the coc-object path and the payload path over real temp war files
  (risk 1). This is the test that actually justifies the change; write it first.
- DEV soak for at least one full war cycle, checking `CYCLE-SUMMARY` `History: new=N` and
  `Finalization` counts stay in their normal range, and spot-checking a finished war's
  leaderboard against its archive file.
- PROD: compare `[CYCLE-END] [MEMORY CONSUMPTION] RSS=` peaks against the post-`280537a`
  baseline recorded in tracker #0009's test case 1.

## 7. Rollback

Steps 2-4 are one commit's worth of change to two functions plus one call site. Revert the commit;
step 1 (`build_war_payload` extraction) is behaviour-neutral and can stay.

## 8. Also open from tracker #0009

Test case 8 on the tracker: even with the waves bounded, measured live cache content only
accounts for ~1.9 GB plus ~1 GB base process, against an observed floor of ~5.5 GB. If the floor
does not drop after `280537a`, the residual is allocator-level (pymalloc arena pinning /
fragmentation) rather than retention-level, and **this plan will not fix it** — that would need
its own investigation (arena tuning, `MALLOC_ARENA_MAX`, or periodic process recycling). Check
the post-`280537a` floor before starting this work, so the effort goes where the memory actually is.
