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
| `track_war_updates` | qapbot/coc_cache.py | `_update_clan_metadata()` | War league change detected | Only for clans where current value is False and league entered Master III+ | True only (promotion); demotions are ignored |
| `has_active_subscriptions` | QBhelperfunctions.py | `_upsert_enemy_clan_on_war_start()` | Enemy clan first encountered | Unconditional | False (enemies start passively tracked) |
| `track_war_updates` | QBhelperfunctions.py | `_upsert_enemy_clan_on_war_start()` | Enemy clan first encountered | M3+ enemy (CWL or regular war) | True (joined 22h pool) |
| `track_war_updates` | QBhelperfunctions.py | `_upsert_enemy_clan_on_war_start()` | Enemy clan first encountered | Below M3 or unknown league | False |
| `has_active_subscriptions` | QBhelperfunctions.py | `_harvest_cwl_group_clans()` | CWL group harvest | All new group clans | False |
| `track_war_updates` | QBhelperfunctions.py | `_harvest_cwl_group_clans()` | CWL group harvest | Group clan in Master III+ | True (22h polling) |
| `track_war_updates` | QBhelperfunctions.py | `_harvest_cwl_group_clans()` | CWL group harvest | Group clan below Master III | False |
| `has_active_subscriptions` | qapbot/ui_clan_management.py | `add_member_clan()` | Guild member_clan added | On failure to fetch clan | False |

---

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

Fires on every CoC API fetch for any clan. If the API returns a different `warLeague` than stored:

- Only upgrades: if the clan's current `track_war_updates` is False and the new league is in
  `_WAR_UPDATE_LEAGUES` (Master III+), the flag is set to True.
- Demotions (e.g. Master III → Crystal I) are silently ignored — the flag stays True.
- Tracked / subscribed clans (`has_active_subscriptions=True`) are doubly immune and bypass
  this block entirely.
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

File: qapbot/ui_clan_management.py `add_member_clan()`

Creates a minimal cache entry when the clan is not already cached. Sets `has_active_subscriptions=False`.
Does NOT populate `war_league` or `track_war_updates` — the DB COALESCE default (1 = True) fills
the gap on persist.

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

Signal = False never causes a downgrade. The only time track_war_updates can be False
is when the clan was added below the threshold and has never since qualified.

The league set (`_WAR_UPDATE_LEAGUES`) is defined identically in both:
- qapbot/coc_cache.py  (module-level frozenset)
- qapbot/cache_manager.py  (class-level frozenset on CacheManager)

And the matching acquisition set (`_CWL_HARVEST_LEAGUES`) in:
- QBhelperfunctions.py  (module-level frozenset)

All three must be kept in sync when the threshold is changed.

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
| qapbot/scripts/chart_clans_per_league.py | Stacked bar chart: solid = actively tracked, hatched = passively tracked |

---

## Critical Constraints

1. track_war_updates is a one-way ratchet — once True it can never be set back to False by any
   automated code path. This applies across all triggers: unsubscribe, channel deletion, family
   removal, league demotion, and bot restart. The only way to clear it would be a manual DB edit.

2. Subscribed clans are doubly immune — if `has_active_subscriptions=True`, the upgrade signal
   from Tier 1 fires unconditionally, and the `_update_clan_metadata` league-change block also
   skips such clans entirely (qapbot/coc_cache.py).

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
| 2026-05-31 | Added `is_deleted` clan deletion detection system | QBhelperfunctions.py, qapbot/coc_cache.py, qapbot/cache_manager.py, qapbot/db_manager.py, QapBot.py |
| 2026-04-11 | Master II → Master III (lowered threshold) | _WAR_UPDATE_LEAGUES (×2), _CWL_HARVEST_LEAGUES |
| 2026-04-11 | track_war_updates made a one-way ratchet (never downgrades) | cache_manager.py, qapbot/coc_cache.py |
| 2026-04-11 | Regular-war enemies in M3+ now get track=True (same as CWL enemies) | QBhelperfunctions.py `_upsert_enemy_clan_on_war_start` |
