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

### Stage 1 — evidence, zero PROD risk — **STATUS: COMPLETE (2026-09-04)**

- [x] **Parity harness** over the DEV temp-file corpus (**54,192 files** — the §5.1 estimate of
      ~31k was low). Runs the coc-object path and the payload path over the same real files and
      asserts the produced `temp_war_stats` are equal. §6 says write this first; it is the test
      that actually justifies the change, and it is far stronger evidence than any calendar.
- [x] **Step 1 of §3** — extract `build_war_payload()` from `save_war_object()`. Behaviour-
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

### Stage 2 — shadow mode on PROD — **STATUS: IMPLEMENTED (build 19), awaiting PROD data**

Implemented 2026-09-05. The payload is built and compared but is **not authoritative** — the
coc-object result is still what gets written, so corruption is impossible by construction.

**What shipped**

- `fetch_clan_war_data()` returns `war_payload` and `end_time` alongside `war_obj`. The payload
  is built **at the return statement**, not at either `save_war_object()` call — §5.3's CWL
  fallback can replace `coc_war_obj` and re-save, so only the final object is correct to
  serialise. Building at the return makes that automatic rather than a thing to remember.
  The clan/opponent swap mirrors `save_war_object()`'s; it is duplicated, and both sites must
  move together if it is ever factored out.
- `stats_from_war_payload()` (QBhelperfunctions.py) — the candidate replacement member loop.
  Key names taken from a real temp file per §3 Step 3, not from §2's table.
- `_compare_shadow_stats()` — reports divergence; float comparison is epsilon-based so 1e-9
  drift on `Total_Dest_Pct` cannot spam a warning every cycle.
- `process_clan_war_data()` runs the comparison and logs
  `[PAYLOAD-PARITY] <clan> N field(s) differ ...`. Wrapped so a shadow failure can never affect
  the authoritative path.
- §3 Step 4 done early: QapBot.py's smart-backdating now reads `war_data['end_time']`, falling
  back to the coc object only while Stage 2 carries both. Remove the fallback at Stage 3.
- §5.4 respected: `war_payload` can be `None` (the `save_skip_no_clan` case) and Phase 3 simply
  skips the comparison.

**Offline replay proves nothing about the field that matters.** 6,000 replayed wars produced
0 diffs — as expected and as designed, because reconstruction feeds our computed
`bestOpponentAttack` back in as the API's field, so coc.py returns exactly what we computed.
The circularity is unavoidable offline. `Defensive_Stars` has both values only on live data.

**How to read the first PROD logs**

`Defensive_Stars` differences are *expected*, and their direction is the whole signal:

| observation | meaning |
|---|---|
| payload **higher** than coc | the intended improvement — `find_best_opponent_attack()` caught a late CWL attack the API's `bestOpponentAttack` field missed |
| payload **lower** than coc | **a real problem** — investigate before Stage 3 |
| any OTHER field differs | **a real problem** — Stage 1 proved those equal over 54,192 wars, so a difference means the live path differs from the replayed one |
| no `[PAYLOAD-PARITY]` lines at all | also suspicious — check the payload is non-None and the state gate is being reached |

**Gate to Stage 3:** at least one full CWL day with no non-`Defensive_Stars` mismatch, including
at least one `war_ended` finalisation and one §5.3 CWL fallback, plus a decision on whether the
`Defensive_Stars` change is the improvement it looks like. Stage 1's corpus contained no
`war_ended` files (they move to archive), so that path is still unexercised.

**Checkpoint, 2026-09-05 ~10:15 local, build 19 after ~2h24m on PROD (`src a1b7a105`):**

- Zero `Traceback`s in the log since the build-19 restart (07:49:29 local).
- Zero `[PAYLOAD-PARITY]` lines of *any* kind — no `build_war_payload failed`, no `shadow
  comparison failed`, no field-divergence warnings — across 5,424 `war_summary` finalisations
  and 99,051 `war_attacks` rows written in that window (both figures cross-checked directly
  against the DB, not just the log's own counters, and they match the log's cumulative
  `History: new=` exactly). `defensive_stars` values are all in [0,3], zero anomalies.
- Per the table above, "no `[PAYLOAD-PARITY]` lines at all" is flagged as suspicious on its own
  — checked it: the shadow branch is provably running (the DB write counts above only happen
  downstream of it), so the silence is real zero-divergence, not a skipped code path.
**CORRECTION, same day — the Stage 3 gate above was partly unsatisfiable. Do not re-apply it.**

My first read of this checkpoint said "keep waiting for a CWL day". That was wrong, and wrong in
a specific way worth recording: I wrote a gate condition, then treated its non-satisfaction as
evidence, without ever checking whether the code *can* satisfy it. Two of its three clauses
cannot be satisfied by any amount of runtime.

1. **"at least one `war_ended` finalisation" — the shadow can never see one.** The comparison
   block sits inside `if state in ('preparation','in_war'):`. A `war_ended` war takes the `else`
   branch, which clears temp stats and returns. The shadow is structurally blind to
   finalisation. Waiting a week would not have produced this line.
2. **"one §5.3 CWL fallback" — cannot produce a divergence by construction.** The payload is
   built *at the return statement*, after any fallback has already replaced `coc_war_obj`. Both
   paths then read the same final object. (Also `[CWL-FALLBACK]` is one of ~4 fallback sites and
   the only one logging at INFO, so zero lines was never evidence it had not fired.) This is a
   robustness nicety, not a correctness gate.
3. **"observe a `Defensive_Stars` divergence" — already measured, at scale, on the right data.**
   Of the 5,424 wars finalised in the window, **5,136 were CWL** and **5,388 were `war_ended`**;
   83,164 CWL attack rows carry a non-zero `defensive_stars`, so the field was heavily populated,
   not trivially zero. 22,032 clan-cycle comparisons, zero divergence. The divergence the table
   above predicts (a late CWL attack the API's own field missed) would materialise *at
   finalisation* — exactly where the shadow cannot look. More soak time yields more of the same.

**What the sample actually proves, and it is stronger than I credited.** `_merge_entries()`
merges temp stats into history rows, so the shadowed values *become* the permanent
`war_summary`/`war_attacks` rows. The shadow was therefore testing precisely the data Stage 3
puts at risk — not an ephemeral leaderboard cache, as I briefly assumed.

**The real remaining gap is static, not temporal.** Stage 3 drops `war_obj` from the Phase-1
return. Its complete consumer surface is three sites:

| site | reads | Stage 3 action |
|---|---|---|
| `QBhelperfunctions.py:7829` (`process_clan_war_data`) | `clan`, `opponent`, `attacks_per_member`, `start_time` — that is all four | remap to payload keys (all four exist: `clan`, `opponent`, `attacks_per_member`, `start_time`) |
| `QapBot.py:1949` | `release_war_object(war_data.get('war_obj'))` | delete the call, keep the function |
| `QapBot.py:1980-1981` | `war_obj.end_time` backdating fallback | drop the fallback; `war_data['end_time']` already carries it since Stage 2 |

The shadow comparison never covered those four attributes — it only ever compared the 9 stat
fields — so that is the actual unverified surface, and it is answerable by code audit today.

**Decision: Stage 3 is unblocked.** Gate satisfied as far as it is satisfiable: zero divergence
over 22,032 comparisons on CWL-dominated live data that demonstrably becomes permanent history,
zero exceptions, DB cross-checked. Remaining work is the 4-attribute remap above, not more
waiting.

### Stage 3 — flip to payload-only — **STATUS: IMPLEMENTED (build 20), awaiting PROD data**

Implemented 2026-09-05, same day as the Stage 2 checkpoint above.

**What shipped**

- `fetch_clan_war_data()` returns `war_payload` + `end_time` only — no `war_obj`. Payload-build
  failure is now `logging.error` (`[WAR-PAYLOAD]`), not a warning: it costs the clan its temp
  stats for the cycle. Same outcome the old code produced when `war_obj` came back `None`, so
  the blast radius is unchanged; only the log level rises to match the consequence.
- `release_war_object()` **moved** to the end of `fetch_clan_war_data()` — the coc object's true
  last use — rather than being deleted as this section originally planned. Deleting it outright
  would have left a cyclic graph for the collector to find; severing at the real last use keeps
  the build-14 win. Safe because `get_current_war_from_api()` explicitly does not cache war data
  ("always single-use"). The function is kept for the `get_league_war()` CWL paths and as the
  coc.py canary; only the call site moved.
- `process_clan_war_data()` reads `_payload` for all four attributes. `start_time` is
  byte-identical (both sides are `str(coc_war_obj.start_time)`), and `attacks_per_member`'s
  differing payload default (0 vs 2) is absorbed by the pre-existing `or 2`.
- QapBot.py: Phase-3 `finally` keeps only `maybe_chunk_collect()`; the backdating fallback and
  the now-unused `release_war_object` import are gone.
- Removed `_compare_shadow_stats()` + `TestShadowComparator` (would compare the payload against
  itself). Dropped a dead `_opp_clan` local.

**The one real difference, and why no amount of Stage 2 data could have found it.** The coc loop
ABORTED the clan (`logging.error` + `return False`) on a member with no name/tag; the payload
loop skipped the member. That `return False` executes *before* the shadow block, so the
comparison was never reached in exactly the case where the two disagreed. It was found by
reading both loops side by side during the flip. `stats_from_war_payload(strict=True)` now
returns `None` and the caller reproduces the abort; `@overload` keeps `strict=False`
non-Optional. Also fixed the abort's log message, which concatenated adjacent string literals
with only the first f-prefixed and therefore logged the literal text `{tag_m}`/`{clan_tag}`.

**Verification.** Full 30,975-file temp corpus through the parity harness, zero mismatches;
3,042 tests green (4 deselected); pyright clean.

**Cross-instance validation, 2026-09-05 — the check shadow mode could not do.** DEV ran one
build-20 cycle while PROD was still on build 19, giving a direct A/B on the same wars from the
same API: build 19 wrote `Defensive_Stars` from coc.py's `m.best_opponent_attack` (the API's own
field), build 20 writes it from the payload's `find_best_opponent_attack()` scan. Comparing the
two databases (PROD read-only, one-time, explicitly authorised) over 40 wars finalised in the
window and present in both — **38 of them CWL** — across `war_summary` (23 columns) and
`war_attacks` (20 columns, 744 rows): **14,880 field comparisons, zero mismatches.**
`defensive_stars` was non-zero in **684 of 744** rows, so the field whose source actually changed
was heavily exercised rather than trivially zero.

This is precisely the observation the Stage 2 gate asked for and could never obtain: the shadow
block cannot see a finalisation, but two instances writing history from two different sources
can be diffed afterwards. Worth reaching for on the next data-path migration — a live A/B across
builds gets you the finalisation-path evidence that in-process differential logging cannot.

`id` and `created_at` were excluded as instance-local. The comparison relies on the wars being
settled (all `war_ended`), which is the right condition for diffing finalised history; two
instances polling mid-war could legitimately differ on a live snapshot.

**DEV timing, treat as hypothesis only.** That cycle showed `gc_collect=0.633s (freed=33,865)`
and `[LOOP-LAG] max=0.56s`, against the build-17 PROD baseline of ~1.29s / ~1.22s. But it was a
single cold-cache DEV cycle carrying 5,350 API fetches versus PROD's steady-state ~2,350, on
different hardware. This plan's own history has two dev simulations that mispredicted PROD, both
optimistically. The PROD numbers are the ones that count.

**Still to measure on PROD** (build 20): `[CYCLE-CLEANUP] gc_collect`, `[LOOP-LAG]` max and
`RSS=` peak against the build-17 baseline in §9's CORRECTION (gc_collect ~1.29s, LOOP-LAG max
~1.22s, RSS 1.3-1.7 GB). The predicted win is the ~1.2s spent walking live `ClanWar` objects.
Treat dev-measured GC timing as a hypothesis only — this plan's own history has two cases where
a dev simulation mispredicted PROD, both optimistically.

### Method note — the gate-reachability trap

Worth carrying to the next staged migration. Stage 2's gate had three clauses; two of them named
observations the code **cannot emit**, and their absence was then read as "not enough evidence
yet". A gate condition is only evidence if it is reachable. Before treating a missing signal as
a reason to wait, check that some code path can actually produce it — and prefer gates phrased
against a *reachable* observation (a log line that provably fires, a DB row that provably lands)
over ones phrased against an event you have merely assumed occurs. The reachability check here
took three greps; the waiting it would have replaced was open-ended.

**Expected:** gc_collect and LOOP-LAG max both fall substantially, because ~1.2s of the current
pause is walking exactly the live objects this stage stops retaining.

### Standing constraints

- Opus, not Sonnet (top of file).
- Do not read the payload back out of `CACHE.temp_war_objects` — §3 step 2 explains why that
  silently yields `None` for `war_ended` clans.
- Verify payload key names against a **real temp file**, not against §2's table — the payload
  mixes snake_case and camelCase and that is where a silent-zero bug hides.
