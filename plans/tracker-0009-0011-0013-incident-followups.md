# Implementation plan: tracker items #9, #11, #13 + the new "My Accounts" crash

## Context

Following the CWL DM-blast incident (tracker #7 deploy → race condition → `ec76063`), four more
issues surfaced from log/DB forensics on 2026-08-21: three filed tracker items (#9 memory, #11
CWL enrollment, #13 grey linked-status) plus one newly-discovered bug (My Accounts message-length
crash, not yet filed). #12 needs no further work — already fixed by `ec76063` (confirmed
byte-identical traceback).

**This plan was revalidated against the live code and the copied-PROD DB on 2026-08-21 after an
initial draft.** Three of the four steps contained errors; Step 4's original fix would have been
dead code. Corrections are marked **[CORRECTED]** with the evidence that drove them.

---

## Step 0 — Clean up the half-applied working-tree change (do this first)

An earlier interrupted implementation attempt left `qapbot/ui_registration.py` modified in the
working tree with the truncation logic **already applied**, but its companion i18n edit rejected.
Verified: `account_management_truncated` appears **0 times** in both `en.json` and `de.json`,
while the code now calls it. Per `qapbot/i18n.py`'s fallback chain (requested_lang → en →
key_path), this renders the literal string `playerregistration.account_management_truncated` to
users instead of crashing — ugly, not fatal, but it must not ship.

Either revert (`git checkout -- qapbot/ui_registration.py`) and redo cleanly as part of Step 1,
or keep it and add the missing keys immediately. Recommend revert-and-redo so Step 1 lands as one
reviewable, tested commit. `.github/copilot-instructions.md` is also modified (the Cardinal Rule
15 tightening) — that one is intentional and should be kept/committed.

---

## Step 1 — Fix "My Accounts" (📋 Meine Konten) crashing for users with many accounts

**Root cause** (verified): `AccountManagementView._build_message_content()`
(`qapbot/ui_registration.py:1040`) builds one unbounded message — one block per linked player, no
cap. With enough accounts (confirmed live: the #11 reporter has 12) it exceeds Discord's 2000-char
limit, and `show_overview()` has no try/except around `send_message()`, so the button is
permanently unusable for that user. Confirmed: 10 identical
`HTTPException 400 (50035): Must be 2000 or fewer in length` crashes in the PROD log, 20:40–20:44.

**Fix**: cap the player list (the ~1400-char budget from the interrupted attempt is a reasonable
starting point), append a new `playerregistration.account_management_truncated` i18n key in
**both** `en.json` and `de.json`, and — **[CORRECTED]** — also clamp the **final** returned
`overview_text` to a hard 2000-char ceiling as a last-resort guard. The original plan only capped
the player-list portion, but the title, the overview template, an arbitrary caller-supplied
`status_message`, and the selection prompt are all appended *after* that cap, so the list budget
alone does not actually guarantee the total stays under the limit — which is the only thing that
prevents the crash.

Pure safety-net fix, not a UX redesign; real pagination is a reasonable future enhancement, out of
scope here.

**Tests**: extend/add coverage for `_build_message_content` (check `tests/discord/` first, per
Cardinal Rule 4) — enough players to force truncation, asserting the returned string is `< 2000`
chars and contains the truncation note; plus a case with a long `status_message` to exercise the
final clamp.

---

## Step 2 — #13: deterministic tiebreak + one-off cleanup of stray UNASSIGNED rows

**Root cause** (verified live): a `player_tag` can legitimately have rows under two different
`discord_id`s (the schema's UNIQUE is `(discord_id, player_tag)`, not `player_tag` alone). The
dedup that picks the winner sorts only by `verified DESC` — and since a normal (non-API-token)
`/link` never sets `verified=1`, a real owner row and a stray `UNASSIGNED` row are usually **tied**,
leaving the winner to undefined row order. When the UNASSIGNED row wins, callers map its
`discord_id` to `None` (e.g. `db_manager.py:4314`), and the CWL board's
`if (player.discord_id == null)` check (`activity/client/src/enrollmentBoard.ts:794`) renders it
grey/unlinked. Confirmed live for `#GP9VJ0RGC` (STY • Lucas — the #11 reporter's own account).

### [CORRECTED] It is seven functions, not six

The original plan listed six and **omitted `get_all_players_for_discord_ids_sync`**. Verified
complete list of `ORDER BY verified DESC` sites in `qapbot/db_manager.py`:

| Function | SQL line | Chunked? | Python re-sort? |
|---|---|---|---|
| `get_current_clan_members_sync` | 4290 | yes | yes (4302) |
| `get_all_players_for_discord_ids_sync` | 4354 | **no** | no |
| `get_player_links_sync` | 4405 | yes | yes (4415) |
| `get_current_clan_tags_for_players_sync` | 4454 | yes | yes (4464) |
| `get_cached_th_levels_for_players_sync` | 4501 | yes | yes (4509) |
| `get_player_owners_for_tags_sync` | 4543 | yes | yes (4553) |
| `get_player_owners_for_clan_sync` | 4582 | **no** | no |

### [CORRECTED] The SQL-only fix is insufficient — the Python sort key must change too

This is the most important correction in this document. For the five **chunked** functions,
adding `ORDER BY verified DESC, (discord_id = 'UNASSIGNED') ASC` alone **does not work**.
`_chunked_in_query_sync`'s own docstring (`db_manager.py:4243-4245`) states it outright:

> "Callers relying on ORDER BY semantics across the merged result (e.g. verified-wins dedup) must
> re-sort the returned rows themselves — a per-chunk ORDER BY does not hold across chunk
> boundaries."

Those five functions therefore re-sort in Python with `key=lambda r: not r["verified"]`. Python's
`sorted()` is **stable**, so among rows tied on `verified` it preserves *concatenation* order
(i.e. chunk order) — silently discarding the SQL's new UNASSIGNED preference. A stray UNASSIGNED
row landing in an earlier chunk would still win.

**Correct fix — change both, in all five chunked functions:**

```python
# SQL
ORDER BY verified DESC, (discord_id = 'UNASSIGNED') ASC
# Python re-sort (must mirror the SQL ordering exactly)
rows = sorted(rows, key=lambda r: (not r["verified"], r["discord_id"] == "UNASSIGNED"))
```

For the two non-chunked functions (`get_all_players_for_discord_ids_sync`,
`get_player_owners_for_clan_sync`) the SQL change alone is sufficient — a single `conn.execute`
means the SQL ordering holds globally. Note `get_current_clan_tags_for_players_sync` and
`get_cached_th_levels_for_players_sync` don't select `discord_id` today, so the Python-side
comparison there needs `discord_id` added to their SELECT lists (the SQL `ORDER BY` can reference
it regardless, but the Python mirror cannot).

### One-off cleanup of already-poisoned rows

New idempotent startup step (Cardinal Rule 12) in `db_manager.py`, e.g.
`cleanup_stray_unassigned_duplicates()`:

```sql
DELETE FROM user_players
WHERE discord_id = 'UNASSIGNED'
  AND player_tag IN (SELECT player_tag FROM user_players WHERE discord_id != 'UNASSIGNED');
```

**Verified against the copied-PROD DB: this matches exactly 1 row today** — the known
`#GP9VJ0RGC` stray. Safe and correctly scoped. Call once from `initialize_database()`, log the
count removed; a no-op on every subsequent start.

**Tests**: one real + one UNASSIGNED row for the same tag, asserting the real owner wins
**regardless of physical row order** (insert in both orders — the current bug passes a naive
single-order test); a chunk-boundary case for at least one chunked function (or a direct unit test
of the sort key) so the Python-side half can't silently regress; cleanup removes exactly the stray
row, leaves the real one, and leaves unrelated single-owner tags untouched.

**Docs**: update `COPILOT_PITFALLS_COOKBOOK.md` Pitfall 35's "recommended follow-up fix" note to
record this as done. Worth adding as a general lesson: *when a SQL `ORDER BY` feeds a Python
re-sort, the two orderings must be kept in lockstep — changing only one silently half-applies.*

---

## Step 3 — #11: harden the confirmed gaps (root trigger still unconfirmed)

You weren't sure whether any clan got toggled off "Participating" that day, so this step does not
claim to fix one confirmed trigger — it closes the gaps the investigation *did* confirm, and adds
the logging needed to pin the mechanism if it recurs.

### 3.1 Concurrency guard on `start_cwl_enrollment()` — verified valid

Verified: **zero** `asyncio.Lock` usages exist anywhere in `qapbot/QBdiscocmdshelper_cwl.py`, and
the only guard in `start_cwl_enrollment()` (line 2084) is a single `if event["status"] != "draft"`
check near the top, with the transition to `signup_open` only written at the very end
(line ~2368) — after the whole DM batch. For a ~120-target run under load (15–25s per-clan API
calls, per Pitfall 35) that window is minutes long, during which a second trigger is not blocked
server-side; the UI button-disable only guards one rendered message.

Add a module-level `Dict[str, asyncio.Lock]` keyed by `(guild_id, season)`, same pattern as the
`CoCClanCache._update_locks` fix in `ec76063`, held for the whole function body.

### 3.2 [CORRECTED] DM retraction — the original fix is architecturally impossible

The original plan said to call `cleanup_stale_cwl_enrollment_dms()` "after the delete loop" inside
`_cleanup_local_pool_for_plain_clan_deactivation_sync()`. **That cannot work**, verified:

- `_cleanup_local_pool_for_plain_clan_deactivation_sync` (line 1562) is a **plain `def`** (sync),
  invoked via `asyncio.to_thread(...)` (line ~2004).
- `cleanup_stale_cwl_enrollment_dms` (line 2555) is an **`async def`** that also needs a live
  `bot` object.

You cannot await the latter from inside the former. **Restructure instead**: have the sync
function *return* the list of player_tags it purged, and perform the DM retraction in the **async
caller**, after the `to_thread` hop returns. Note there is more than one reference to this
function (`QBdiscocmdshelper_cwl.py:2004` and discussion/usage around `web_bridge.py:1956/1993`) —
enumerate every actual call site during implementation and make sure each async caller either
retracts or deliberately documents why it doesn't.

### 3.3 Observability

Add one `logging.info(...)` to `start_cwl_enrollment()` on completion (event id, guild, season,
counts) and one to `_cleanup_local_pool_for_plain_clan_deactivation_sync()` (clan deactivated,
tags purged, tags protected). Both log nothing on the success path today — which is precisely why
this investigation could not pin the trigger from the full-day log. Match the `[CWL-...]` tag
convention.

**Tests**: concurrency test for `start_cwl_enrollment()` mirroring `TestUpdatePlayerInfoConcurrency`
in `test_coc_cache_coverage.py` (same-key serializes, different-key doesn't); a test that the sync
cleanup returns the purged tags and that its async caller passes them to the retraction helper.

---

## Step 4 — #9 memory: **[CORRECTED — original approach invalidated]**

### The original fix would have deleted zero rows

The draft proposed pruning `clan_name_cache` entries whose `last_checked_via_api` is older than
90 days. Measured against the copied-PROD DB:

| Metric | Count |
|---|---|
| total clans | 438,524 |
| passive + unsubscribed | 281,499 |
| actively tracked (`track_war_updates=1`) | 157,025 |
| **prunable @ 90 days** | **0** |
| **prunable @ 30 days** | **0** |
| of which NULL timestamp | 0 |

**Zero, at either threshold.** The premise that stale entries accumulate untouched is false:
`refresh_stale_passive_clans()` (`QBhelperfunctions.py`, Phase 1.6) already sweeps every passive
clan roughly monthly (1000/run × 288 cycles/day) and refreshes `last_checked_via_api`. Nothing is
ever 90 days stale. The prune would have shipped as dead code.

### The FK danger was also understated

The draft said to "confirm the others don't have anything stricter" than
`user_players.current_clan_tag`'s `ON DELETE SET NULL`. They are stricter — **five**
`ON DELETE CASCADE` FKs reference `clans(clan_tag)`: `clan_family_members`, `guild_member_clans`,
`guild_welcome_clans`, `cwl_event_clans`, and `cwl_assignments`. FK enforcement **is** active at
runtime (`PRAGMA foreign_keys=ON` at `db_manager.py:571/1197/1310`; the `0` from the sqlite3 CLI
is just that tool's default). Deleting a `clans` row would cascade into live CWL rosters and
assignments. Today 0 prunable clans hold a `cwl_event_clans` row, but that is a snapshot, not a
guarantee — a CWL **guest** clan has `has_active_subscriptions=0` by design (Pitfall 31), so it is
exactly the population a naive prune would target.

### What the evidence actually points to

`clan_name_cache` is a flat metadata dict per clan; at ~438K entries a reasonable estimate is a few
hundred MB — real, but nowhere near the observed 7.1 GB RSS. The far larger consumer is
`temp_war_objects`, and it is **deliberately sized that way**. Its own cap comment
(`cache_manager.py:61-66`) states:

> "Each entry is ~61 KB (measured); 100 000 entries ≈ 6.1 GB (server-machine has 10 GB RAM, 9 GB
> reserved for the bot)."

Live snapshot: **21,643 entries ≈ 1.3 GB**, at only ~22% of its permitted cap. So RSS is not
obviously "leaking" so much as operating inside a very large by-design budget — and a CWL season
pushing that cache toward its cap would add several more GB on a 9 GB allowance.

### Revised Step 4: measure first, decide second

1. **Fix the memory-profile blind spots** (this is now the whole deliverable, not a side note):
   - Add real `estimated_size_mb` for `clan_name_cache`, `temp_war_objects`, and `temp_war_stats`
     to `_build_cache_summary()` (`QBdiscocmdshelper_admin_command.py:~1350`, alongside the
     existing `coc_clan_cache` estimate). Today these report entry counts only, which is exactly
     why this took a live investigation to even bound.
   - Label the tracemalloc section with its actual trace-window duration. Tracing starts
     on-demand (`QBdiscocmdshelper_admin_command.py:~1851`) minutes before the snapshot, so its
     "top 50" covers ~5 minutes, not the process lifetime — comparing that ~150 MB against 7.1 GB
     RSS is what sent the first analysis chasing phantom native allocations.
   - Raise or remove the 200,000-object cap on the `[GC OBJECT COUNTS]` scan, which currently
     truncates before saying anything useful.

2. **Then decide**, with real numbers in hand. Do **not** ship a staleness-based prune. If
   `temp_war_objects` proves dominant, the lever is its cap / eviction policy (a tunable constant,
   no FK risk), not deleting `clans` rows. If `clan_name_cache` genuinely proves large enough to
   matter, any eviction must first prove it cannot touch a clan referenced by those five CASCADE
   FKs.

**Tests**: extend whatever covers `_build_cache_summary()` for the new size fields.

**#9 stays open** pending the post-instrumentation numbers — it is a diagnosis-first item now, not
a fix-ready one.

---

## Sequencing

Step 0 (clean the tree) → Step 1 → Step 2 → Step 3, each its own commit + changelog entry + tests,
matching this session's pattern. Step 4 is now instrumentation-only and carries no data-deletion
risk, so it can land any time after; the *actual* memory fix is deferred until it produces numbers.

Steps 2 and 3 are the two that genuinely protect bot integrity — Step 2 fixes wrong linked-status
being shown (and silently mis-driving CWL pool logic), Step 3 closes a real concurrency hole in a
120-DM broadcast path.

## Verification

`.\run_tests.ps1` after each step — must stay green. For Step 2, re-run the same read-only DB
checks used to validate this plan (`sqlite3 -readonly data/qapbot.db`) to confirm the tiebreak
returns the real owner for `#GP9VJ0RGC` and that the cleanup matches exactly the expected row
count before trusting it against the live database.

## Tracker items

Post a manual test case for #11 and #13 when their steps ship (Cardinal Rule 15). File the "My
Accounts" crash as its own tracker item — it was never filed. #9 stays open until Step 4's
instrumentation produces real per-cache numbers.
