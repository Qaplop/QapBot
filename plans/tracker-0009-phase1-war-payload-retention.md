# Tracker #0009 follow-up — stop Phase 1 retaining `coc.ClanWar` objects

**Status:** IN PROGRESS — staged execution, see §10 at the bottom for stage status and gates.
**~~Do not start before 2026-09-11~~** (superseded by §10)
Original gate rationale kept below for the reasoning it records:
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

---

## 9. Interaction with the 2026-09-04 GC work (read before starting)

A day of GC work landed after this plan was written (builds 12-17, see
`qapbot/docs/PERFORMANCE_TUNING.md`). It touches the same object graph, so the overlap needs
stating precisely — **it is adjacent to this plan, not a partial implementation of it.**

### What that work did NOT do: reduce the peak

`release_war_object()` severs coc.py's back-references so a war graph is freed by refcounting
instead of waiting for a sweep. But it is called **in the Phase-3 loop**, after
`process_clan_war_data()`. War objects are still created at `asyncio.gather()` (QapBot.py L1633)
and still live until their turn in Phase 3 (L1855). **The Phase-1/2 boundary peak this plan
targets is completely unchanged.** That peak is what §4's 250 MB -> 60 MB claim is about, and it
is still on the table.

The GC work solved a different problem — stop-the-world pauses making Discord unresponsive —
which happens to share a root cause (coc.py's cyclic war graph).

### What that work DID do for this plan: de-risk §2, and remove one of its incidental benefits

- **§2's audit is independently confirmed.** Tracing war-object consumers from scratch for the
  sever work found exactly the same set: `.end_time` (QapBot.py's backdating), and `.clan`,
  `.opponent`, `.start_time`, `.attacks_per_member` in `process_clan_war_data()`. Two
  independent audits agreeing is the best evidence this table is complete.
- **`_opp_clan` is still a dead local** (QBhelperfunctions.py, in `process_clan_war_data`) —
  §2's note stands.
- **The graph's shape is now measured, not assumed:** 132 sweep-only objects for a 15v15 war,
  ~195/clan on PROD average. Supports §2's "3-4x heavier" framing.
- **The GC cost of these objects as *garbage* is gone.** But see the CORRECTION below — their
  cost as *live retained objects* is not, and that is the dominant remaining pause.

### A merge conflict this plan does not anticipate

Step 4 says to replace `war_data.get('war_obj')` with the carried `end_time`. That call site now
*also* contains `release_war_object(war_data.get('war_obj'))` and a `maybe_chunk_collect()`.
When step 2 lands, `war_obj` no longer exists, so:

- **delete the `release_war_object()` call** at that site — with no coc object retained there is
  nothing to sever, which is a clean win, not a regression;
- **keep `maybe_chunk_collect()`** — it is unrelated to war objects and still governs the
  remaining `coc.Clan` garbage from `coc_clan_cache`;
- **keep `release_war_object()` itself.** It is still needed for the CWL paths that build war
  objects outside this flow (`get_league_war()` call sites in QBhelperfunctions.py), and its
  tests double as the canary for coc.py dropping its back-references.

### §8's precondition, now measured

§8 says to check the post-`280537a` memory floor first, so the effort goes where the memory
actually is. **Measured 2026-09-04 across 80 cycles: RSS peak per build 1,777-3,494 MB, minimum
752 MB** — against the ~5.5 GB floor §8 cites. The floor did drop, so the residual was retention
rather than allocator-level, and this plan's premise holds.

But it also means **the memory-pressure justification is much weaker than when this was
written**: a 3.5 GB peak on a 10 GB box is comfortable, not urgent. The remaining real prize is
§4's last paragraph — making the peak proportional to cheap data so `_MAX_INACTIVE_PER_CYCLE`
(QapBot.py L1413, currently 1500) can be raised. That is a 22h-SLA/throughput argument, not a
memory-safety one. Re-scope the ticket accordingly before starting.

### Status unchanged

Still **do not start before 2026-09-11** — the 2026-09 CWL season is running as of this note.

### CORRECTION (build 17 PROD data, same day): this plan IS justified on GC grounds after all

The note above originally said "do not re-justify this plan on GC grounds". **That was wrong**,
and the build-17 measurement is what corrected it.

Slicing the collection (build 17) cut end-of-cycle garbage by 76% — `freed=` 103,687 -> 24,996 —
and the pause did **not** move: `gc_collect` 1.135s -> 1.292s, `[LOOP-LAG]` max 0.93s -> 1.22s.
(Load differed ~20% between the samples, so per clan it is 0.597 -> 0.565 ms — roughly neutral,
not the regression the raw numbers suggest. Two cycles each; do not over-read either way.)

The arithmetic is the point. At the ~240K objects/s this box measures, freeing 25K objects
should cost ~0.10s. It costs ~1.29s. **So ~1.2s of the remaining pause is walking LIVE
young-generation objects, not freeing garbage.**

That is precisely what this plan removes. Every `coc.ClanWar` held in `fetch_results` between
the gather (QapBot.py L1633) and its turn in Phase 3 (L1855) is a live object the end-of-cycle
collect must walk. Neither `release_war_object()` (which acts after Phase 3) nor slicing (which
removes only dead objects) touches it. Cutting retention from ~120-170 KB to ~40 KB per clan
cuts that walk in the same proportion.

So this plan now has two independent justifications: peak RSS / `_MAX_INACTIVE_PER_CYCLE`
(§4, §9), and **the ~1.2s live-object walk that is the current floor on Discord
responsiveness**. The second is the stronger one.

### Method note: stop trusting dev simulation for GC timing on this box

Dev-side simulation has now mispredicted PROD twice, both times optimistically:

| Predicted (dev) | Actual (PROD) |
|---|---|
| per-cycle collect 0.018s | **2.050s** (70x) |
| slicing cuts max pause 5.8x | **no improvement** |

PROD is Linux on a Celeron with single-channel DDR3L; dev is Windows on a fast desktop CPU.
Allocator behaviour, memory latency and platform all differ. **Object counts** measured on dev
are structural and do transfer (155 per clan graph, 132 per war). **Timings do not.** Before
acting on any dev-measured GC duration, either verify it on PROD or state it as a hypothesis.

---

## 10. AGREED EXECUTION PLAN (2026-09-04) — staged, resumable

**This supersedes the "do not start before 2026-09-11" gate at the top of this file.** Read this
section first if you are picking the work up mid-flight; each stage records its own status.

### Why the date gate was replaced with an evidence gate

qaplop's argument for starting now: recent GC work keeps the CWL cycle at **mean 111s, max 144s
against a 300s budget** — ~156s of headroom at peak, so a performance regression cannot breach
the SLA.

That is true but addresses the wrong axis. The gate's real danger is §5.1: **silent stat
corruption**, which a code revert does not undo once zero-star rows reach war history. Headroom
protects cycle time, not data.

Two things resolve it:

1. **The season cuts both ways, which the original gate did not account for.** §5.3's
   `_find_active_cwl_war_for_clan()` replace-and-resave path *only runs during CWL*. Shipping
   after 2026-09-10 means it is not exercised again until October — a bug there would lurk for a
   month and then fire unobserved during the next season. Doing this while CWL runs puts the
   riskiest path under live test immediately.
2. **Spend the headroom on verification instead of speed.** Shadow mode (Stage 2) makes
   corruption impossible by construction, and the spare 156s is what pays for running both code
   paths at once.

So: proceed now, but each stage is gated on evidence from the previous one, not on a date.

### Stage 1 — evidence, zero PROD risk — **STATUS: harness DONE, extraction OPEN**

- [x] **Parity harness** over the DEV temp-file corpus (**54,192 files** — the §5.1 estimate of
      ~31k was low). Runs the coc-object path and the payload path over the same real files and
      asserts the produced `temp_war_stats` are equal. §6 says write this first; it is the test
      that actually justifies the change, and it is far stronger evidence than any calendar.
- [ ] **Step 1 of §3** — extract `build_war_payload()` from `save_war_object()`. Behaviour-
      neutral; verify byte-identical temp files before going further.

**Gate to Stage 2:** parity exact over the whole corpus. Any mismatch class must be understood
and fixed, not waived — §5.1's failure mode is a silent 0, so "only 3 files differ" is a finding,
not noise.

### Stage 1 results (2026-09-04) — harness DONE, extraction still open

**Parity harness: `tests/integration/test_war_payload_parity.py`. Result: 54,192 / 54,192 real
temp wars, ZERO mismatches** on `Player`, `PlayerID`, `TH_lvl`, `Stars`, `Attacks`,
`Max_Attacks`, `Total_Dest_Pct`. It reconstructs a `coc.ClanWar` from each payload and runs both
extraction paths over it. Runs in ~31s; `PARITY_LIMIT` caps the file count (default 4000 so the
normal suite stays fast), and the whole class skips where the corpus is absent.

That is Stage 1's gate MET **for the fields it can cover**. Three findings change the plan:

#### FINDING 1 — §2's table is wrong about `Defensive_Stars`, and it is the riskiest field

§2 lists `m.best_opponent_attack.stars` -> `bestOpponentAttack` as a pure key mapping. It is not:

* `process_clan_war_data()` reads coc.py's `m.best_opponent_attack`, which resolves
  `_best_opponent_attacker` — populated straight from **the CoC API's own**
  `bestOpponentAttack.attackerTag` field.
* the payload's `bestOpponentAttack` is computed by **our** `find_best_opponent_attack()`, which
  scans every opponent attack *specifically because the API field misses late CWL attacks* (see
  the comment in `simple_member`).

So switching to the payload would **change `Defensive_Stars` behaviour** — probably for the
better, that is why our function exists, but it is a behaviour change and not a refactor.

**And no corpus replay can validate it.** Temp files do not retain the API's original value, so
reconstruction feeds our computed value back in and the comparison goes circular. It is
therefore excluded from the harness by design. **`Defensive_Stars` can only be validated by
Stage 2 shadow mode**, on live data where the coc object still carries the API field. Treat it
as the single highest-risk item in the migration.

#### FINDING 2 — §5.2's state-normalisation risk is real, confirmed on the first run

The payload stores its own normalised `state` (`in_war`); coc.py's `WarState` enum only accepts
the raw API spelling (`inWar`). The harness failed immediately on this until it mapped back.

This matters beyond the harness: `process_clan_war_data()` gates its temp-stats block on
`state in ('preparation','in_war')`. Anything that starts reading `state` from the payload must
not assume the coc spelling — get it wrong and the block is silently skipped for every in_war
clan, which is §5.1's silent-zero failure mode exactly.

#### FINDING 3 — what a green parity run does and does not prove

The harness reconstructs the coc object *from the payload*, so both sides read the same keys.
Corrupting the **data** is therefore invisible to it: both sides degrade to 0 and agree. It only
catches a **reader** that mistypes a key — which is the actual migration risk, since the data is
fixed on disk. The first version of its self-test corrupted the data and passed for the wrong
reason; it now injects a faulty reader instead, and a second test asserts a silent 0 in *every*
numeric field is caught. Do not "simplify" those two tests away.

**Corpus coverage note:** the 54,192 files are `in_war` (49,463) and `preparation` (4,729) only —
no `war_ended`, because those move to archive. Harmless for this harness (the temp-stats block
only runs for those two states), but it means the `war_ended` path is unexercised here and must
be covered by Stage 2's gate, which already requires one `war_ended` finalisation.

#### Still open in Stage 1

- [ ] **Step 1 of §3 — extract `build_war_payload()`.** Not done. First attempt was reverted:
      the payload literal depends on six nested closures (`simple_attack`,
      `find_best_opponent_attack`, `calculate_defensive_stars`, `simple_member`, `simple_badge`,
      `simple_clan`) plus `league_group_data`, `clan_obj`/`opponent_obj` and the member lists, so
      it is a ~160-line move, not a few lines. Extract them **together, verbatim**, into a
      module-level `build_war_payload(coc_war_obj, my_clan, enemy_clan)` placed *outside* the
      class, and have `save_war_object()` call it. `cache_manager.py` **has a UTF-8 BOM** — read
      and write it with `utf-8-sig`/explicit byte handling, and never `ast.parse()` a BOM-bearing
      string (it raises a misleading "invalid non-printable character").

### Stage 2 — shadow mode on PROD — **STATUS: not started**

Implement §3 steps 2-4, but **do not make the payload authoritative yet**:

- build the payload AND keep the coc object,
- compute the Phase-3 stats **both** ways,
- compare; log `[PAYLOAD-PARITY]` with clan tag and differing key on any mismatch,
- **use the coc-object result.**

Corruption is impossible by construction here, because the new path is not authoritative. Costs
extra CPU for a few cycles — that is what the headroom buys. Note this stage does **not** yet
reduce retention, so the ~1.2s live-object GC win (§9's CORRECTION) does not arrive until
Stage 3.

**Gate to Stage 3:** zero `[PAYLOAD-PARITY]` mismatches across at least one full CWL day,
including at least one `war_ended` finalisation and one CWL fallback (§5.3) occurrence.

### Stage 3 — flip to payload-only — **STATUS: not started**

- Drop the coc object from the Phase-1 return value; remove the shadow comparison.
- Delete the `release_war_object()` call at the Phase-3 site (§9 — nothing left to sever there);
  **keep the function** for the `get_league_war()` CWL paths and as the coc.py canary.
- Keep `maybe_chunk_collect()` (unrelated; governs `coc_clan_cache` garbage).
- Measure: `[CYCLE-CLEANUP] gc_collect`, `[LOOP-LAG]` max, and `RSS=` peak against the build-17
  baseline recorded in §9's CORRECTION (gc_collect ~1.29s, LOOP-LAG max ~1.22s, RSS 1.3-1.7 GB).

**Expected:** gc_collect and LOOP-LAG max both fall substantially, because ~1.2s of the current
pause is walking exactly the live objects this stage stops retaining.

### Standing constraints

- Opus, not Sonnet (top of file).
- Do not read the payload back out of `CACHE.temp_war_objects` — §3 step 2 explains why that
  silently yields `None` for `war_ended` clans.
- Verify payload key names against a **real temp file**, not against §2's table — the payload
  mixes snake_case and camelCase and that is where a silent-zero bug hides.
