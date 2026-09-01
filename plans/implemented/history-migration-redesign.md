# Hot→history migration redesign — single-pass implementation plan

**Status: IMPLEMENTED 2026-09-01.** Kept as the reasoning record; the authoritative
description of what shipped is `qapbot/docs/DATABASE_ARCHITECTURE.md` entries
**2026-09-01 (b)** (the redesign) and **(c)** (the retention retune from 60 to 75 days).
**Origin:** the 2026-09-01 incident — same doc, entry (a).

Two decisions changed *after* this plan was written; where the sections below still say
60 days, entry (c) supersedes them:

- **Retention is 75 days, not 60.** At 60, data written during a CWL season migrated
  exactly two months later — during the *next* CWL season, stacking the two heaviest jobs
  onto the same nights. Shortening to 50 (the first instinct) would have been far worse:
  below 61 the calendar floor binds, freezing the cutoff for ~1/3 of days and then jumping
  up to 12 days at once, on the 1st of the month — the cliff this redesign removed. 71..78
  is the only band that is both smooth and CWL-clean; 75 sits at its centre.
- **`history_migration_enabled` was removed entirely**, not shipped. It was a stopgap for
  the incident; a short, non-blocking, budgeted, resumable nightly job has no state in
  which you would want it permanently off, and an "off" setting would silently stop
  retention with nothing to alarm on. Wherever the sections below reference the switch
  (including §6's staged activation), it no longer exists —
  `HISTORY_MIGRATION_NIGHTLY_ROW_BUDGET=0` is the equivalent no-op if ever needed.

---

## 1. Decisions taken

| decision | value | note |
|---|---|---|
| Retention | **75 days**, with a calendar-month floor | §2 — revised from 60 on 2026-09-01, see DATABASE_ARCHITECTURE.md 2026-09-01 (c): 60 put every CWL-data migration night inside a CWL season, and shortening to 50 would have reintroduced the cliff |
| Rollout | **Single pass**, all six fixes in one change | CWL risk accepted by operator |
| Per-cycle chunking | **Deleted**, not resized | |
| Hot DB steady-state size | ~92M rows / **~40 GB** accepted | SSD-on-NAS has the headroom |
| Nightly bound | **Row budget**, not days | §3 |

### 2. Why a rolling window needs a calendar floor

The documented contract is "hot always holds the current + the immediately preceding calendar
month". The oldest row that contract must retain can be **61** days old — day 31 of a 31-day
month following a 31-day month (e.g. 2026-08-31, where 2026-07-01 is 61 days back). A plain
`today - 60` rule migrates that day out and silently breaks the contract.

Verified over four years: plain `today - 60` violates the contract on **8 dates**; with the
floor, **0**.

```python
cutoff = min(today - history_retention_days, first_day_of_previous_calendar_month)
```

The floor makes ANY retention value contract-safe — it can only ever cause hot to retain
*more* than asked, never less. That is what let the constant be retuned from 60 to 75 later
purely on load-distribution grounds, with no re-derivation of the contract.
(At the shipped 75 the floor never binds at all; it still matters as the guarantee.)

**Backlog measured on the 2026-09-01 16:10 snapshot: 30 rows** at a 60-day window, and zero at
the shipped 75-day one — for all four tables (`war_attacks`, `war_summary`,
`cwl_league_groups`, `cwl_league_rounds`). There is nothing to catch up.
The 2026-09-01 run was chasing the *calendar-month* cutoff (`2026-08-01`), which on the 1st
demands hot shed everything older than ~31 days. That is the cliff being removed.

### 3. Why the nightly bound is rows, not days

Daily volume in the current hot DB spans `2026-07-15`: **37 rows** to `2026-08-05`:
**2,191,245 rows** — a 59,000× spread. It is structural: CWL weeks run ~2× baseline
(2026-08-03..08 held 2.0–2.3M/day against 1.0–1.25M), and early-July dates are near-empty
because the 2026-09-01 run processed rows in `id` order, not `date` order.

`history_migration_nightly_row_budget = 3_000_000` — chosen so a full CWL day (~2.2M) completes
in one night with headroom, rather than spilling. The walk advances the cutoff as far as the
budget allows and stops; in steady state the budget is never reached.

---

## 4. What a single pass makes obsolete

This is the substance of the revision. Five items from the phased plan disappear or collapse.

1. **The two-step migration engine is now a one-step rewrite.** The phased plan had Phase 1
   keep `_migrate_table_batch_by_date()`'s `id IN (?,?,…)` form, then Phase 5 replace it with
   keyset ranges. In one pass we write the final engine once and delete the old one. See §5.C.

2. **Phase 1's `SQLITE_BUSY` mitigation ("temporarily lower batch size to 2000") is dropped.**
   It existed only to survive the window between Phase 1 and Phase 5. Batch size is now chosen
   once, against the final engine, by *transaction duration* (~1–2 s) rather than row count —
   which is the correct knob anyway, since a faster engine moving more rows per transaction
   holds the write lock **longer**, not shorter. Raising the sync pool's `busy_timeout` from
   5000 ms to 15000 ms is kept (cheap, independent).

3. **Phase 3's separate "migration progress + ETA" work folds into the new engine.**
   Retrofitting it to the old engine and then porting it would be writing it twice. The guard
   logging (rejected interactions) stays a standalone item — it is in `QBcore.py`/`ui_common.py`
   and touches nothing the engine touches.

4. **The `bot_metadata` legacy-value migration gets simpler.** The phased plan needed defensive
   parsing because the key would change type under a running system. In one pass we use a **new
   key** (`history_migration_cutoff_reached`, a date) and leave `last_history_migration` alone
   as dead data. No type-punning, no defensive parse, no ambiguity about which writer wrote it.

5. **`is_monthly_migration_due()`'s entire two-mode design goes away** rather than being edited
   twice. The `ignore_in_process_claim` parameter, the in-process claim, and the `day == 1` gate
   all existed to stop a *monthly* job re-firing. A nightly job driven by
   `reached_cutoff < target_cutoff` needs none of them.

**What does not collapse:** the DEV verification (§6) becomes *more* important, not less, because
there is no longer a phased rollout absorbing risk.

**Also revised (2026-09-01, operator):** the date index is built **inline at bot startup**, not in
a nightly maintenance window, following the settled pattern from the 2026-07-30 "Startup Ordering
Fix (Final)" entry — and on `main` only. See §5.E; that change is what keeps the one-time build
inside both the operator's ~15 min tolerance and `initialize_database()`'s 1800 s watchdog.

---

## 5. Work items

### A. Config — `qapbot/config.py`

| change | |
|---|---|
| add | `history_retention_days: int = 75` (`HISTORY_RETENTION_DAYS`) — see the header note |
| add | `history_migration_nightly_row_budget: int = 3_000_000` (`HISTORY_MIGRATION_NIGHTLY_ROW_BUDGET`) |
| remove | `history_migration_cycle_chunk_minutes` + env + constructor arg |
| change | `history_migration_time_budget_minutes` default **90 → 30** — a secondary hard stop; with the migration no longer blocking Discord it does not gate availability, and a smaller value bounds write-lock contention |
| keep | `history_migration_admin_budget_minutes` — `/admin`'s 15-min interaction token still needs it |
| keep | `history_migration_enabled` — ships `True`; the env var is the rollback lever |

### B. `QBcore.py`

- Add `db_migration_active: bool = False`, documented as **advisory**: observability and cycle
  gating only, never an interaction block.
- `_maintenance_interaction_check` must **not** consult it.
- Add guard logging: log at INFO (rate-limited — first rejection per block window, plus a count
  when the block clears) with command name and reason. Today it logs *nothing* when it turns a
  user away, which is why a 14-hour outage produced zero `[MAINTENANCE-GUARD]` lines in a
  596k-line log.

### C. `qapbot/db_manager.py` — the engine

**`_history_cutoff()`** → returns the rolling cutoff from §2 (`min(today - retention, floor)`).

**New `_migrate_date_window_batched()`** replaces `_migrate_table_batch_by_date()`:

- Walks **day by day** from the last reached cutoff toward the target.
- Per day, sub-batches using the date range plus an `id` keyset bound — the
  `_bulk_move_chunk()` pattern, extended with the date predicate:
  ```sql
  SELECT id FROM main.<t> WHERE date >= :ds AND date < :de ORDER BY id LIMIT 1 OFFSET :n-1
  INSERT OR IGNORE INTO history.<t> (cols) SELECT cols FROM main.<t>
      WHERE date >= :ds AND date < :de AND id <= :bid
  DELETE FROM main.<t> WHERE date >= :ds AND date < :de AND id <= :bid
  ```
  This kills three problems at once: no 5000-parameter bind (SQLite's ceiling is 32766), no
  5000 rowid seeks per statement, and — with §5.E's index — an index range scan instead of the
  current full `SCAN war_attacks`.
- Bounded by row budget, then time budget, whichever binds first.
- Sub-batch size targets ~1–2 s per transaction, not a fixed row count.

**`monthly_history_migration()`** → rename to `run_history_migration()`:

- Sets `db_migration_active`, **not** `db_maintenance_mode`. This is the single change that
  turns the 2026-09-01 outage into zero downtime. It is safe because the batched migration
  holds no exclusive lock: it runs short transactions on the async writer connection while
  every user command reads through the separate 8-connection sync pool in WAL mode, and WAL
  readers never block on a writer. `db_maintenance_mode` was inherited from
  `nightly_db_maintenance()`, which genuinely needs it (VACUUM takes `EXCLUSIVE`, drains pool).
- Persists `history_migration_cutoff_reached` (a date) after each completed day, so an
  interrupted run resumes at day granularity.
- Logs progress with rate and ETA: one `COUNT(*)` per day-window at the start, then
  `moved / remaining / rows-per-sec / ETA`. A run that will take hours says so in its first line.
- Hoists `_explicit_column_list()` out of the batch loop (currently one `PRAGMA table_info` per
  batch — ~4,800 needless calls on 2026-09-01).

**CWL tables**: derive `cutoff_month` from the same rolling cutoff so all four advance together.

**`fast_bulk_history_migration()`** — two latent defects, fixed regardless of whether it is used
again, because it is the documented recovery tool:
- The index rebuild runs with `wal_autocheckpoint=0` still in effect (set at the top of the try
  block, restored *after* `_rebuild_war_attacks_secondary_indexes()` in the finally). The
  largest write burst of the run accumulates entirely in the WAL — ~28 GB at current table
  sizes. **Move the `wal_autocheckpoint=1000` restore above the rebuild call.**
- The rebuild is in a `finally`, which covers exceptions but not SIGKILL/OOM/dropped SSH. A hard
  kill leaves `main.war_attacks` with **no secondary indexes**. Add a startup check that
  rebuilds any missing `war_attacks` index before the bot serves traffic.

### D. `QapBot.py`

- **Delete** the `elif _migration_due and ... cycle_chunk_minutes > 0` branch and
  `_cycle_migration_chunk_task`.
- **Replace** `is_monthly_migration_due()` with `is_history_migration_due()` —
  `reached_cutoff < target_cutoff`, plus the `history_migration_enabled` kill-switch. No
  day-of-month gate, no in-process claim, no `ignore_in_process_claim`. Update the `/admin`
  call site in `QBdiscordcmds.py`.
- **Shutdown watchdog** (~line 345, "60 s budget, paused while `db_maintenance_mode` is True")
  must also pause on `db_migration_active` — otherwise a CTRL+C during a long migration fires
  the force-close and races the aiosqlite worker, the exact 2026-08-14 failure that pause exists
  to prevent.
- `db_maintenance_idle_event` gate: unchanged. The next cycle should still wait for a chunk.
- Add a cumulative-block alarm: WARNING when `db_maintenance_mode` exceeds ~30 min in a rolling
  hour. Now scoped to VACUUM/REINDEX only, but it is the generic guard for any future cause.

### E. The `date` index — built inline at startup, `main` only

`war_attacks` has no index on `date` alone; the migration's batch SELECT is a full
`SCAN war_attacks` (verified via `EXPLAIN QUERY PLAN`). It is cheap today only because the
remaining old rows sit at the low end of the id range — but backfill inserts old-date rows at
*high* ids, so with a thin nightly date slice the scan degrades badly.

**Add to `main` only:**
- `idx_wa_date ON war_attacks(date)` — 51.8M rows, ~5–15 min first build
- `idx_ws_date ON war_summary(date)` — 2.96M rows, ~20 s

**Not on `history`.** The migration queries `main` by date and only ever `INSERT`s into
`history`; there are no history date-only filters anywhere in the codebase (grepped). Building
it there would cost 15–40 min for 102.75M rows and buy nothing — and combined with the main
build would exceed `initialize_database()`'s 1800 s watchdog and fail startup outright.

The asymmetry is deliberate and safe: Cardinal Rule 1 is about **column** parity (positional
`SELECT *` corruption across schemas); an index-set difference cannot corrupt data. Add
`idx_wa_date` to `_create_schema()` inline like every other index, and **not** to
`_WAR_ATTACKS_SECONDARY_INDEX_DDL` — that list is the "drop for a bulk move, rebuild after" set,
and `_bulk_move_chunk()` filters on `date <`, so this index should survive a bulk move rather
than be dropped for it. Document both points where the DDL list is defined.

**Where it runs:** inline in `_create_schema()`, i.e. inside `QapBot.initialize_database()` —
`on_ready()`'s Step 1.5, strictly before CoC login and before `periodic_main()`, under
`asyncio.wait_for(timeout=1800.0)` with `db_maintenance_mode = True`. This is the settled
pattern from the 2026-07-30 "Startup Ordering Fix (Final)" entry, and the reason it is safe is
sequencing, not cleverness: **no other code path in the app can write to the DB at that point.**
The two superseded attempts in that entry (fire-and-forget background task; deferral to nightly
maintenance) are on record — do not re-derive either.

Accepted costs, one time only: the bot shows online but rejects commands for the build's
duration, and the first update cycle starts 5–15 min late (delayed, not lost — war files persist
and the next cycle catches up). Every later restart is a cheap `CREATE INDEX IF NOT EXISTS`
metadata check. If a slower volume ever pushes the build near the watchdog, raise the 1800 s —
do not move the build.

Set `temp_store_directory` to the DB directory before building (the 2026-08-02 tmpfs lesson —
SQLite's default temp location can be a small tmpfs on a NAS, unrelated to data-volume free
space).

### F. `/status`

Surface migration state: last reached cutoff, rows remaining to target, last run duration and
rate. So the operator can check without reading logs.

### G. Docs

- `DATABASE_ARCHITECTURE.md`: new entry; mark the 2026-08-01 "fourth same-day follow-up"
  (per-cycle chunking) as **superseded**, pointing here. Do not delete it — that reasoning is
  why the incident happened and is worth keeping legible.
- `README.md`: remove `HISTORY_MIGRATION_CYCLE_CHUNK_MINUTES`, add `HISTORY_RETENTION_DAYS` and
  `HISTORY_MIGRATION_NIGHTLY_ROW_BUDGET`, update `HISTORY_MIGRATION_TIME_BUDGET_MINUTES`.
- `changelog.txt`.

### H. Tests

- **The 60-day invariant**: for every date across ≥4 years, `cutoff <= first day of previous
  calendar month`. This is what makes §2 structural rather than a comment.
- **Guard separation**: `check_maintenance_block()` and `_maintenance_interaction_check()` return
  "not blocked" when only `db_migration_active` is set, and still block on `db_maintenance_mode`.
  The most important regression guard in the change.
- Budget stops the walk mid-target and reports a partial cutoff; the next run resumes from it.
- Day-window batching moves exactly the rows in the window and no others.
- Kill-switch still short-circuits and still does not persist a cutoff.
- Missing `history_migration_cutoff_reached` (first run ever) starts from the oldest data present.

---

## 6. Rollout — the de-risking that replaces the phases

A single pass removes the phased safety net, so it is replaced by a staged *activation*. The
code ships in one deployment; the behaviour turns on in steps.

1. **Build and test on DEV.** DEV holds a pre-fix PROD backup — real table sizes, real data
   shape. Run the new engine there end to end and record actual rows/sec. Every throughput
   number in this plan is extrapolated from 2026-09-01 PROD logs (596–1,199 rows/s); DEV gives
   the real post-change figure before PROD sees it.
2. **Deploy to PROD with `history_migration_enabled = False`.** All new code live, migration
   still inert. The date indexes build during this restart's `initialize_database()` (§5.E) —
   expect a 5–15 min startup with commands blocked, then confirm the bot starts, cycles run
   normally, and no new errors appear. This isolates "did the refactor break the bot" and "did
   the index build behave" from "did the migration behave".
3. **One supervised manual run**: `run_history_migration_now.py` with a small row budget, watched
   live. Confirms the engine moves the right rows at the expected rate against PROD data — and
   confirms the new index is actually being used (`EXPLAIN QUERY PLAN` should show a range scan,
   not `SCAN war_attacks`).
4. **Flip `history_migration_enabled = True`** and watch the first nightly run. At 60 days the
   first night is ~30 rows plus whatever has aged since — minutes, not hours.
5. **Rollback at any point** is the kill-switch: set it false, restart. The engine is resumable
   by date, so a half-finished walk costs nothing. Note the index build is *not* covered by the
   kill-switch — it is part of schema init and will have already happened. That is intended: the
   index is harmless on its own and worth keeping regardless of how the rest lands.

**Note on step 3:** worth doing even though step 1 covered it — DEV and the NAS SSD have
different I/O profiles, and every throughput surprise in this project's history has come from
that difference. Time the index build in step 2 for the same reason.

---

## 7. Deferred

- **Month-partitioned history** (`qapbot_history_2026-07.db`, attached on demand).
  `history.war_attacks` is at 102.75M rows / 35.2 GB and grows ~37M rows/month forever, so every
  nightly insert pays ~7 B-tree writes/row into an ever-larger table — the nightly job gets
  slower every month by construction. Partitioning would let each month land in a fresh,
  index-free table with `CREATE INDEX` run once, and make retention purges a file delete. Large
  change to every hot+history UNION query; do not start until this plan is stable.
- **Shorter retention.** 35 days would put hot at ~43M rows / ~20 GB (smaller than today) but
  requires auditing every hot-only query for a >35-day window first. The mechanism here supports
  it with a one-constant change whenever that audit is done.
