# Clan War Tracking — actively tracked / passively tracked Status Reference

All write paths that change `track_war_updates` and `has_active_subscriptions` on clan cache entries.

---

## Write Paths Summary

| Field | Write Location | Function | Trigger | Condition | Value Assigned |
|---|---|---|---|---|---|
| `has_active_subscriptions` | cache_manager.py | `update_clan_subscription_status()` | Subscribe / Unsubscribe | Presence in subscriptions / families / member_clans | Computed |
| `has_active_subscriptions` | cache_manager.py | `update_all_clan_subscription_statuses()` | Startup or bulk family changes | All clans in batch recalc | Computed |
| `track_war_updates` | cache_manager.py | `update_clan_subscription_status()` | Subscribe / Unsubscribe | Only upgrades: sets True if signal is True and current value is False | One-way ratchet via `_calculate_track_war_updates()` |
| `track_war_updates` | cache_manager.py | `update_all_clan_subscription_statuses()` | Startup or bulk family changes | Only upgrades: sets True for newly-eligible passively tracked clans | One-way ratchet via `_calculate_track_war_updates()` |
| `track_war_updates` | qapbot/coc_cache.py | `_update_clan_metadata()` | War league change detected, clan not subscribed | Current value False and league entered Master III+ | True (promotion) |
| `track_war_updates` | qapbot/coc_cache.py | `_update_clan_metadata()` | War league change detected, clan not subscribed | Current value True and league dropped below Master III | False (demotion); also purges the clan's temp war files |
| `has_active_subscriptions` | QBhelperfunctions.py | `_upsert_enemy_clan_on_war_start()` | Enemy clan first encountered | Unconditional | False (enemies start passively tracked) |
| `track_war_updates` | QBhelperfunctions.py | `_upsert_enemy_clan_on_war_start()` | Enemy clan first encountered | M3+ enemy (CWL or regular war) | True (joined 22h pool) |
| `track_war_updates` | QBhelperfunctions.py | `_upsert_enemy_clan_on_war_start()` | Enemy clan first encountered | Below M3 or unknown league | False |
| `has_active_subscriptions` | QBhelperfunctions.py | `_harvest_cwl_group_clans()` | CWL group harvest | All new group clans | False |
| `track_war_updates` | QBhelperfunctions.py | `_harvest_cwl_group_clans()` | CWL group harvest | Group clan in Master III+ | True (22h polling) |
| `track_war_updates` | QBhelperfunctions.py | `_harvest_cwl_group_clans()` | CWL group harvest | Group clan below Master III | False |
| `has_active_subscriptions` | qapbot/ui_clan_management.py | `MemberClansConfigurationView._on_apply()` | Guild member_clan added, clan not yet cached | Unconditional (both fetch-success and fetch-failure branches) | False |
| `has_active_subscriptions` | qapbot/cache_manager.py | `_sync_group_track_war_updates()` | Group league_rank freshly resolved (`_process_league_group_response`) | New group member not yet in `clan_name_cache` | False (inserted) |
| `track_war_updates` | qapbot/cache_manager.py | `_sync_group_track_war_updates()` | Group league_rank freshly resolved (`_process_league_group_response`) | Any non-subscribed member (new insert or existing, promotion or demotion) | Set to match the group's confirmed league (True for M3+, False otherwise) |
| `track_war_updates` | QBhelperfunctions.py | `refresh_stale_passive_clans()` | Monthly passive-clan sweep (Phase 1.6, QapBot.py) | Passively-tracked clan's `get_clan()` ping reveals Master III+ | True (promotion, via the normal `_update_clan_metadata()` path this triggers) |

---

### 7. CWL group league confirmed (background, subscription-independent)

File: qapbot/cache_manager.py `_sync_group_track_war_updates()`, called from `_process_league_group_response()`

The group-wide counterpart to path 5 (`_update_clan_metadata()`) and path 6 (`_harvest_cwl_group_clans()`
above) — those two only reach a clan that is already actively polled, or freshly discovered as an enemy
of a *subscribed* clan, which left a gap: an already-known, non-subscribed clan in a group with no
subscribed member (or no "new enemy" event this season) could sit indefinitely with a stale league and
the wrong `track_war_updates` — the exact "~40,000 Master III+ clans silently stopped being polled"
failure mode (2026-06-24 changelog entry).

`_process_league_group_response()` runs on every fresh `get_league_group()` response (Layer 1 organic +
Layer 2 finalization fallback, see `CWL_ROUND_TRACKING_PLAN.md`) and, the moment a group's `league_rank`
is confirmed (new group discovery only — see that doc's league_rank timing note), sweeps every member in
the response's `clans` list:

- Subscribed clans are skipped entirely — always tracked regardless of league.
- Members already in `clan_name_cache` get `war_league` and `track_war_updates` corrected to match the
  group's confirmed league (promotion or demotion), with the same temp-file cleanup as path 5's demotion.
- Members **not yet** in `clan_name_cache` at all are inserted directly from the already-fetched
  `ClanWarLeagueGroup.clans` objects (tag + name) — no extra `get_clan()` call needed, since every member
  of a CWL group shares one league by construction. Without this, only the single clan
  `_process_league_group_response` may have queried live to resolve `league_rank` itself would get
  inserted, silently dropping the other (up to 7) never-before-seen group-mates.
- **Mid-season guard (added 2026-08-08):** a demotion is deferred (only `war_league` corrected) if
  `db_manager.clan_has_cwl_data_for_season(clan_tag, cwl_season)` is True — i.e. the clan already has
  archived `war_summary` rows for *this* group's season. Without it, demoting mid-season would silence
  polling for the clan's remaining rounds, permanently freezing an incomplete season on record. The
  deferred demotion is not lost — it applies naturally at the next season's group discovery, by which
  point the clan has zero rows for the new season so the guard no longer applies. Discovered when the
  one-time backfill for this write-path (`qapbot/scripts/backfill_group_track_war_updates.py`, applied
  to prod 2026-08-08) derived `track_war_updates` from the currently in-progress season and demoted
  clans mid-way through it; remediated via `qapbot/scripts/repromote_mid_season_clans.py`. See path 5
  above for the equivalent, separately-added guard on the older single-clan demotion path.

This still cannot discover a "wholly foreign" group where **none** of the 8 members has ever been
reachable from a subscribed clan (directly or transitively across past seasons) — every entry point into
the discovery graph (`_upsert_enemy_clan_on_war_start`, `/cwlinfo`, and this sync) requires the querying
clan to already be graph-reachable. See `CWL_ROUND_TRACKING_PLAN.md`'s coverage note for that residual gap.


### 8. Passive clan monthly refresh (added 2026-08-08)

File: QBhelperfunctions.py `refresh_stale_passive_clans()`, called from QapBot.py Phase 1.6

Every other write path above only ever reaches a `track_war_updates=False` clan through the
subscription-rooted discovery graph — a live `get_clan()`/`get_league_group()` call for *some* clan that
happens to touch this one. A clan whose group is never rediscovered this season (no groupmate is ever
polled) sits with a stale `war_league` forever, and if it gets promoted to Master III+ in the real game,
the bot never finds out — promotion detection is itself gated by being polled in the first place.

This closes that gap independently of the discovery graph: every update cycle (no interval gate — chosen
to burn down the initial backlog in about a day instead of trickling it out over a month; see trade-off
note below), QapBot.py's Phase 1.6 calls `refresh_stale_passive_clans()`, which:

- Scans `clan_name_cache` for non-subscribed, non-deleted, `track_war_updates=False` clans whose
  `last_checked_via_api` is missing or older than `qapbot.constants.PASSIVE_CLAN_REFRESH_INTERVAL_DAYS`
  (30 days).
- Takes the most-overdue (or never-checked) `_PASSIVE_REFRESH_BATCH_SIZE` (1000) of them and issues a
  plain `get_clan()` call for each, bounded by a small semaphore (`_PASSIVE_REFRESH_CONCURRENCY`, 15) —
  **not** the full `fetch_clan_war_data()` war-data pipeline, since no war tracking is wanted for these
  clans, only a clan-info refresh.
- Relies entirely on the existing `CoCClanCache.get_clan() -> _update_clan_metadata()` path (write-path 5
  above) to do the actual promotion — no special-casing here; if the clan is now Master III+,
  `track_war_updates` flips to True and it joins the normal 22h/12h polling pool from then on.

At 1000/run × 288 cycles/day this clears a population in the hundreds of thousands within about a day.
Sizing snapshot (2026-08-08, dev DB mirroring a recent prod backup): 275,405 passively-tracked clans,
207,134 of them already >30 days stale.

**Trade-off of running every cycle instead of on an interval gate:** API/DB load isn't the ongoing
concern — once the initial backlog clears, few clans are newly overdue each cycle regardless of how high
the cap is set, so actual `get_clan()` volume drops close to zero naturally. The full `clan_name_cache`
scan (~400K entries) that runs every cycle to find candidates is **not** a meaningful cost either —
benchmarked at ~0.20s against a synthetic cache at real dev-DB scale (416,815 entries), negligible
against the 300s cycle interval. The real, dominant cost is the network-bound `get_clan()` batch itself:
Phase 1.6 sits in the sequential critical path before Phase 2 (orphan/war processing), so up to 1000
calls at concurrency 15 (tens of seconds while the backlog is being burned down, shrinking toward
near-zero once caught up) delays the start of the core war-tracking phases that follow it, every cycle.
`refresh_stale_passive_clans()` logs scan time, fetch time, ms/clan average, and the total on every run
(`[PASSIVE-REFRESH]` log line) so the real, observed cost is directly visible rather than estimated.

## Detailed Assignment Locations


### 1. Subscribe / Unsubscribe (user action)

Files: QBdiscordcmds.py → cache_manager.py `update_clan_subscription_status()`

Triggered by `/subscribe` and `/unsubscribe` commands. After the DB row is created/deleted,
`update_clan_subscription_status()` is called for every affected clan tag. It recalculates both fields:

- `has_active_subscriptions` = True if clan appears in any `subscriptions`, `family_subscriptions`,
  or `member_clans` row
- `track_war_updates` = one-way ratchet: only set to True if the upgrade signal fires and the
  current value is False. Unsubscribing does NOT clear this flag.

Immediately persisted via `persist_clan()`.

Call sites:
- QBdiscordcmds.py — after family subscription
- QBdiscordcmds.py — after direct clan subscription
- QBdiscordcmds.py — after family unsubscription
- QBdiscordcmds.py — after direct clan unsubscription
- QBcore.py — after channel deletion (for all former subscribers)
- qapbot/ui_clan_management.py — admin clan-management function


### 2. Channel deleted (event)

File: QBcore.py `on_guild_channel_delete()`

Same recalculation as path 1 but triggered automatically when Discord fires the channel delete event.
Calls `update_clan_subscription_status()` for every clan that had a subscription in that channel.


### 3. Bot startup — bulk recalc

File: cache_manager.py `update_all_clan_subscription_statuses()`

Runs once on startup. Iterates every clan in `clan_name_cache`, applies the ratchet for both fields.
All clans at Master III+ that are currently False get upgraded to True (Req 2).
No clan is ever downgraded. Uses `bulk_update_clan_subscription_statuses()` for the DB batch write.


### 4. Family / member-clan admin changes

Files: QBdiscordcmds.py, qapbot/ui_clan_management.py → cache_manager.py

When an admin adds or removes a clan from a family or guild member-clan list,
`update_clan_subscription_status()` is called for the affected clan tags.


### 5. War league detected to have changed (background)

File: qapbot/coc_cache.py `CoCClanCache._update_clan_metadata()`

Fires on every CoC API fetch for any clan. If the API returns a different `warLeague` than stored,
and the clan is NOT subscribed (`has_active_subscriptions=False`):

- Promotion: if the clan's current `track_war_updates` is False and the new league is in
  `_WAR_UPDATE_LEAGUES` (Master III+), the flag is set to True.
- Demotion: if the clan's current `track_war_updates` is True and the new league drops out of
  `_WAR_UPDATE_LEAGUES`, the flag is set to False — the clan's temp war files are removed first
  (`_cleanup_temp_war_files()`) to prevent orphans, then it leaves the 22h polling pool. Added
  2026-06-29; this is no longer a pure one-way ratchet (see Critical Constraints below).
  **Mid-season guard (added 2026-08-08):** before applying the demotion, checks
  `db_manager.clan_has_in_progress_cwl_data(clan_tag)` — if the clan's most recent
  `cwl_league_groups` season is still active (`cwl_ended=0`) AND it already has an archived
  `war_summary` row for that season, the demotion is deferred (only `war_league` is corrected)
  so the season already in progress isn't abandoned mid-way. It re-evaluates and correctly
  demotes once that season ends (next season's group discovery finds zero rows for the new
  season, so the guard no longer applies). Discovered after a production incident where the
  one-time backfill for write-path 7 stopped polling ~clans mid-season this way — see
  `qapbot/scripts/repromote_mid_season_clans.py`.
- Subscribed clans (`has_active_subscriptions=True`) are immune and bypass this block entirely —
  their `track_war_updates` stays True regardless of league movement.
- Persisted via `persist_clan()`.


### 6. Enemy clan first seen in a war (war discovery)

File: QBhelperfunctions.py `_upsert_enemy_clan_on_war_start()`

Called when an enemy clan appears in a war for the first time (not yet in `clan_name_cache`).

| War type    | Enemy league              | track_war_updates |
|---|---|---|
| CWL or regular war | Master III – Champion I   | True  (22h polling pool) |
| CWL or regular war | Below Master III / unknown | False |

`has_active_subscriptions` is always False for enemies. Persisted immediately.
CWL group harvest (`_harvest_cwl_group_clans`) still runs only for CWL wars.


### 7. CWL group harvest (background, CWL season)

File: QBhelperfunctions.py `_harvest_cwl_group_clans()`

Runs only when a subscribed clan in Master III+ processes a CWL war. For every previously-unseen
clan found in the league group:

- `has_active_subscriptions = False`
- `track_war_updates = True` if the group clan's league is in `_CWL_HARVEST_LEAGUES`
  (Master III – Champion I), else False

Entry guards prevent this from running for unsubscribed clans or below-threshold leagues.


### 8. New clan added via member-clan UI

File: qapbot/ui_clan_management.py `MemberClansConfigurationView._on_apply()`

Creates a minimal cache entry when the clan is not already cached. Sets `has_active_subscriptions=False`
unconditionally (both the fetch-succeeds and fetch-fails branches). Does NOT populate `war_league` or
`track_war_updates` — the DB COALESCE default (1 = True) fills the gap on persist.

---

## The _calculate_track_war_updates() Tier Logic

Shared core function used by `update_clan_subscription_status()` and
`update_all_clan_subscription_statuses()`.

The function computes a boolean UPGRADE signal. Callers apply a one-way ratchet:
they only write True (never False) — `if signal and not current: set True`.

```
IF has_active_subscriptions = True  (Tier 1 — subscribed)
    → signal = True  (always polled every cycle, regardless of league)

ELSE  (passively tracked clan):
    IF war_league ∈ {Champion I, Champion II, Champion III,
                     Master I, Master II, Master III}
        → signal = True  (qualifies for 22h polling)
    ELSE  (Crystal and below, or unknown league)
        → signal = False  (no upgrade triggered)
```

Within these two callers, signal = False never causes a downgrade — this function/caller pair is
still a pure one-way ratchet. Globally, though, `track_war_updates` CAN go from True back to False
via a separate path: `qapbot/coc_cache.py` `_update_clan_metadata()` actively demotes non-subscribed
clans that drop below Master III (see write path 5 and Critical Constraint 1 above).

The league set is a single source of truth: `qapbot/constants.py` `WAR_UPDATE_LEAGUES`.
`qapbot/coc_cache.py`, `qapbot/cache_manager.py`, and `QBhelperfunctions.py`
(`_CWL_HARVEST_LEAGUES`) all import it rather than defining their own copy — previously these
were three independently-maintained frozensets that had to be edited in lockstep by hand (see
2026-04-11 threshold change below); now changing the threshold means editing one constant.

---

## Database Persistence

File: qapbot/cache_manager.py `persist_clan()` → qapbot/db_manager.py `_save_clan_unlocked()`

```sql
INSERT INTO clans (..., has_active_subscriptions, track_war_updates, is_deleted, ...)
VALUES (..., ?, COALESCE(?, 1), COALESCE(?, 0), ...)
ON CONFLICT(clan_tag) DO UPDATE SET
    has_active_subscriptions = excluded.has_active_subscriptions,
    track_war_updates = COALESCE(excluded.track_war_updates, track_war_updates, 1),
    is_deleted = COALESCE(excluded.is_deleted, is_deleted, 0)
```

Key details:
- `track_war_updates=None` from cache → DB stores 1 (True)
- `is_deleted=None` from cache → existing DB value preserved; INSERT defaults to 0 (False)
- UPDATE uses COALESCE: passed value → existing DB value → default
- Explicit False downgrades for `track_war_updates` are preserved; None cannot accidentally downgrade

---

## Complete Write-Through Flow

```
Cache write (in-memory dict)
    ↓
clan_name_cache[clan_tag] = {..., has_active_subscriptions: bool, track_war_updates: bool, is_deleted: bool}
    ↓
await persist_clan(clan_tag)
    ↓
db_manager.save_clan(..., has_active_subscriptions=bool, track_war_updates=bool/None, is_deleted=bool/None)
    ↓
SQLite INSERT OR REPLACE with COALESCE defaults
```

---

## Consumption Paths (reads)

| Location | Usage |
|---|---|
| QapBot.py polling loop | Skip clans with `track_war_updates=False` entirely |
| QapBot.py polling loop | Skip clans with `is_deleted=True` (after `track_war_updates` check) |
| QapBot.py polling loop | True + has_active_subscriptions=True → polled every cycle (Tier 1) |
| QapBot.py polling loop | True + has_active_subscriptions=False → polled only if >22h since last update (Tier 2) |
| QBdiscordcmds.py /admin stats | Count active / 22h-polled / enemy-only breakdown |
| QBdiscordcmds.py /list Tracked Clans | Filter and split actively tracked vs passively tracked for chart |
| qapbot/chart_clans_per_league.py | Stacked bar chart: solid = actively tracked, hatched = passively tracked |

---

## Critical Constraints

1. track_war_updates is a one-way ratchet in `cache_manager.py` (`update_clan_subscription_status()`,
   `update_all_clan_subscription_statuses()`) — those two paths only ever upgrade, never downgrade.
   This applies to unsubscribe, channel deletion, family removal, and bot restart: none of those
   triggers can clear the flag.
   **Exception (since 2026-06-29)**: `qapbot/coc_cache.py` `_update_clan_metadata()` DOES downgrade
   `track_war_updates` back to False for non-subscribed clans whose league drops below Master III
   (see write path 5 above) — league demotion is no longer ratcheted. Subscribed clans remain immune
   in all cases.

2. Subscribed clans are doubly immune — if `has_active_subscriptions=True`, the upgrade signal
   from Tier 1 fires unconditionally, and the `_update_clan_metadata` league-change block also
   skips such clans entirely, in both directions (qapbot/coc_cache.py).

3. Below-threshold actively tracked clans still get CWL war data — they are in the 22h polling pool, so
   their own CWL wars are fetched and stored. However, `_harvest_cwl_group_clans` has an entry
   guard `if tracked_league not in _CWL_HARVEST_LEAGUES: return`, so group harvesting is NOT
   triggered for subscribed clans below Master III (prevents unbounded cache growth).

4. Enemy discovery timing — enemies are added during war processing. If they are in Master III+
   CWL, they join the 22h polling pool immediately on first encounter.

5. League change recalc is reactive — a passively tracked below-threshold clan's promotion is only
   detected when the CoC API is next hit for that clan (next poll or war processing).

6. Missing fields in ui_clan_management.py — the member-clan setup path does not populate
   `war_league` or `track_war_updates`. The DB COALESCE default (True) fills the gap, which means
   newly added member-clans start at track=True until the next startup bulk-recalc or an API
   fetch corrects the league value.

7. `is_deleted` is NOT a one-way ratchet — it is automatically cleared by any successful
   `GET /clans/{tag}` API call. Supercell occasionally restores clans or reuses tags; the flag
   will self-correct without manual intervention. Do NOT add `is_deleted` to `_ESSENTIAL_CLAN_FIELDS`
   in `fetch_clan_war_data()` — its safe default is False (not deleted), and deleted clans are
   skipped before `fetch_clan_war_data` is even called.

---

## Clan Deletion Detection (`is_deleted`)

When Supercell permanently deletes a clan, the CoC API returns `404 NotFound` on every poll.
The `is_deleted` flag prevents continuous polling of dead clans and suppresses the error noise.

### Detection logic in `fetch_clan_war_data()` (QBhelperfunctions.py)

```
1. If get_clan() TTL gate was NOT active (clan was fetched via API this cycle):
       coc.NotFound from get_clan() → _mark_clan_deleted() → return None

2. If get_clan() TTL gate WAS active (last fetch was recent, API skipped):
       coc.NotFound from get_current_war()
           → verify clan existence: try get_clan()
               → coc.NotFound confirmed → _mark_clan_deleted() → return None
               → get_clan() succeeds   → clan is alive; treat as private warlog / no war
               → get_clan() other error → assume transient; return None without marking deleted
```

### `_mark_clan_deleted()` (QBhelperfunctions.py)

- Sets `clan_name_cache[tag]["is_deleted"] = True`
- Calls `persist_clan()` to write to DB immediately
- Logs exactly one `[CLAN-DELETED]` warning (subsequent cycles are silent — clan is skipped)

### Auto-restore in `_update_clan_metadata()` (qapbot/coc_cache.py)

- Any successful `GET /clans/{tag}` clears `is_deleted` → `False` and calls `persist_clan()`
- Logs `[CLAN-RESTORED]` so the event is visible in logs
- Supercell can restore or reuse clan tags; the system self-corrects with no manual intervention

### Phase-1 loop skip (QapBot.py)

```python
# After track_war_updates check:
if clan_data.get('is_deleted'):
    continue  # No API calls, no errors, no log noise
```

### Write paths for `is_deleted`

| Field | Writer | Condition | Value |
|---|---|---|---|
| `is_deleted` | `QBhelperfunctions.py` `_mark_clan_deleted()` | `get_clan` or `get_current_war` returns `coc.NotFound` | True |
| `is_deleted` | `qapbot/coc_cache.py` `_update_clan_metadata()` | Any successful `get_clan` response, clan was previously deleted | False |
| `is_deleted` | `qapbot/coc_cache.py` `_update_clan_metadata()` | New clan added for the first time | False |

---

## Change Log

| Date       | Change                               | Affected files |
|---|---|---|
| 2026-06-29 | Added CWL league demotion support — non-subscribed clans dropping below Master III now have `track_war_updates` reset to False (temp war files cleaned up); `_update_clan_metadata` is no longer a pure one-way ratchet | qapbot/coc_cache.py |
| 2026-05-31 | Added `is_deleted` clan deletion detection system | QBhelperfunctions.py, qapbot/coc_cache.py, qapbot/cache_manager.py, qapbot/db_manager.py, QapBot.py |
| 2026-04-11 | Master II → Master III (lowered threshold) | _WAR_UPDATE_LEAGUES (×2), _CWL_HARVEST_LEAGUES |
| 2026-04-11 | track_war_updates made a one-way ratchet (never downgrades) | cache_manager.py, qapbot/coc_cache.py |
| 2026-04-11 | Regular-war enemies in M3+ now get track=True (same as CWL enemies) | QBhelperfunctions.py `_upsert_enemy_clan_on_war_start` |
