# CWL Enrollment Redesign — Player Pool & Global Status

## Context

A test user ("Lucas") with many linked player accounts across several clans in one guild's
clan family only received CWL enrollment DMs for *some* of his accounts. Root cause (confirmed
against DB + logs): `start_cwl_enrollment()` seeds the DM/signup list only from clans currently
marked `participating=1` for the event — clans in the family that aren't toggled participating
this season are skipped entirely, and their members never get a signup row or a DM.

The project owner reviewed this and specified a broader redesign, not just a narrow fix — the
notion of a guild's CWL "player pool" (everyone eligible for an enrollment DM this season) needs
to become explicit and decoupled from the `participating` toggle, guest-clan/guest-player
handling needs to change so unchecking a clan is no longer destructive, and DM/response status
needs to become a normalized, cross-guild concept so no player is ever DMed twice for the same
season. A separate, unrelated bug (a transient Discord 5xx error aborting the whole enrollment
DM batch) was found during investigation and is fixed as part of Phase 1, independent of the
rest.

This plan implements the owner's rules a–h (player pool, guest handling, cross-guild dedup) plus
the DM-reliability fix, as five independently-shippable phases.

## Current code map (verified)

- **Core seed+DM function**: `start_cwl_enrollment(guild_id, season)`,
  `qapbot/QBdiscocmdshelper_cwl.py:1558-1753`. Line 1648
  (`db.get_current_clan_members_sync(participating_clan_tags)`) is the line that wrongly
  restricts the pool to participating clans only.
- **DM choke point**: `CacheManager.send_user_dm`, `qapbot/cache_manager.py:1288-1344` — the
  single funnel for all ~7 DM call sites in the bot. Its `except discord.DiscordServerError`
  branch (1339-1341) re-raises instead of returning `False` like every other failure path; the
  CWL DM loop (1740-1749) has no try/except around it, so one transient 5xx aborts the rest of
  the batch and leaves `cwl_events.status` stuck at `'draft'` (the transition to `signup_open`
  is *after* the loop, line 1751). **Fixed in Phase 1** — see status log below.
- **Guest player invite**: `handle_post_cwl_enrollment_guest`, `web_bridge.py:1278-1340` — always
  seeds a `cwl_signups` row now; only DMs immediately today if the "Send enrollment DM
  immediately" checkbox was checked (rule e removes this).
- **Guest clan invite**: goes through the normal "Configure Participating Clans" save
  (`handle_post_clan_config` → `_prepare_and_save_clan_config_sync` → `set_cwl_event_clans_sync`,
  `db_manager.py:3552`). No DM is sent on add today — already matches rule d's timing.
- **Guest clan uncheck cascade** (the thing rule f must stop): `handle_post_clan_config`'s
  `newly_deactivated_tags` loop (`web_bridge.py:1514-1522`) calls
  `detach_guild_from_shared_clan_on_deactivation` (`QBdiscocmdshelper_cwl.py:1292-1349`), whose
  "not cross-guild-shared" branch calls `_cleanup_local_pool_for_plain_clan_deactivation_sync`
  (1190-1289) — this is what deletes `cwl_signups`/`cwl_assignments` rows for the clan's members
  today. Its protection logic (family members, other active guest clans, locked admin-override
  assignments — lines 1276-1289) is correct and gets **reused**, just triggered from a new place.
- **No per-player-per-season DM/status table exists** — confirmed via grep across
  `db_manager.py`. `cwl_signups.status` (per event) and `cwl_shared_clan_players.status` (per
  shared-clan roster) are the closest things, and are read in exactly 3 places
  (`web_bridge.py:451, 478, 540`, all inside `_build_enrollment_payload_sync`) plus one
  event-scoped count query (`get_cwl_signup_status_counts_sync`, `db_manager.py:3664`) — narrow
  enough that these can stay as synced, read-optimized mirrors of a new global source of truth.
- **Existing precedent for cross-guild fan-out**: `sync_cwl_shared_clan_roster_to_local_pools`
  (`QBdiscocmdshelper_cwl.py:774-808`) already does "one write, propagate to every other guild's
  local pool" — just scoped to one shared clan's roster. Rule h's global status propagation
  generalizes this exact pattern.
- **Button surface for the new "Notify New Members" control**:
  `add_cwl_management_components(view, guild_id)`, `ui_cwl_roster.py:265-386` — shared by both
  `ClanManagementView` and `CwlManagementHubView`, so one change covers both surfaces per rule h.
  Row 2 is free (row 1 = season select, row 3 = the 4 existing buttons).

## Approach

### 1. New table: `cwl_player_season_status` (normalized global status, rule h)

Add to `db_manager.py`'s schema-creation method, near the `cwl_shared_clan_players` block:

```sql
CREATE TABLE IF NOT EXISTS cwl_player_season_status (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    player_tag             TEXT    NOT NULL,
    cwl_season             TEXT    NOT NULL,
    player_name            TEXT,
    discord_id             TEXT,
    dm_sent                INTEGER NOT NULL DEFAULT 0,
    dm_sent_at             TEXT,
    dm_sent_via_event_id   INTEGER,   -- audit only, not a FK (event may later be deleted)
    dm_sent_via_guild_id   TEXT,
    status                 TEXT    NOT NULL DEFAULT 'pending',
    responded_at           TEXT,
    responded_via_event_id INTEGER,
    responded_via_guild_id TEXT,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (player_tag, cwl_season)
);
CREATE INDEX IF NOT EXISTS idx_cwl_player_season_status_season ON cwl_player_season_status(cwl_season);
```

`dm_sent` and `status`/`responded_at` are written by **two separate methods** and never
conflated — mirroring the already-documented `cwl_shared_clan_players.status` vs `.assigned`
split. New `db_manager.py` methods (follow the existing sync-method conventions used by
`upsert_cwl_signup_sync`/`delete_cwl_signup_sync`):

- `get_cwl_player_season_status_sync(player_tag, cwl_season)`
- `get_cwl_player_season_dm_status_bulk_sync(player_tags, cwl_season) -> Dict[str, bool]`
- `get_cwl_player_season_status_bulk_sync(player_tags, cwl_season) -> Dict[str, dict]`
- `mark_cwl_player_dm_sent_sync(player_tag, cwl_season, player_name, discord_id, event_id, guild_id, sent_at)`
  — upsert, touches only the `dm_sent*` columns.
- `set_cwl_player_response_status_sync(player_tag, cwl_season, player_name, discord_id, status, responded_at, event_id, guild_id)`
  — upsert, touches only `status`/`responded_at`/`responded_via_*`.
- `find_cwl_signup_events_for_player_and_season_sync(player_tag, cwl_season) -> List[int]` — every
  event (any guild) whose pool already contains this player, for fan-out.
- `find_cwl_shared_clan_ids_for_player_and_season_sync(player_tag, cwl_season) -> List[int]`
- `update_cwl_signup_status_sync(event_id, player_tag, status, responded_at)` — narrow UPDATE,
  **does not create rows** (only fills a gap in an already-pooled guild, never seeds a new one).
- `delete_cwl_event_clan_sync(event_id, clan_tag)` — for rule f's Remove button.

New orchestration function `propagate_cwl_player_response(...)` in `QBdiscocmdshelper_cwl.py`
(near `sync_cwl_shared_clan_roster_to_local_pools`), generalizing that same pattern: writes the
global table, then fans the resulting status out to every *other* event's `cwl_signups` row and
every shared clan's `cwl_shared_clan_players` row for that `player_tag`+`cwl_season`, returning
the affected guild_ids for the caller's `bump_enrollment_version` fan-out.

Call sites that must invoke it (in addition to, not instead of, their existing own-guild write):
`CwlSignupResponseButton.callback` (`ui_cwl_roster.py:1150-1218`) and
`_apply_cwl_enrollment_signup_sync` (`web_bridge.py:1027-1098`) — both currently only update
their own event's row and bump only their own guild.

### 2. Rule b — family-wide player pool

In `start_cwl_enrollment()`, change line 1648's input set from `participating_clan_tags` to the
union of the guild's whole family (`resolve_guild_member_clan_tags(guild_id)`, unchanged
function) and every clan ever configured for this event, participating or not
(`{c["clan_tag"] for c in all_clans}`, `all_clans` already fetched at line 1630) — this superset
also covers a guest clan that was invited then unchecked before Start Enrollment ever ran (rule f
means unchecking no longer purges it from the pool).

`participating_clan_tags` itself stays exactly as-is for the auto-assignment *target*
restriction (1699-1732) and the cross-guild sharing check (1643-1646) — only the pool-membership
candidate set changes. The existing `cwl_enrollment_include_all_linked_accounts` expansion
(1656-1667) is untouched — confirmed working as intended and orthogonal to this change.

### 3. Rules c/d/e/f/g — guest pool management

- **Rule e**: delete the "Send enrollment DM immediately" checkbox entirely — backend
  (`handle_post_cwl_enrollment_guest`, `web_bridge.py:1278-1340`: remove `send_dm_on_save`
  handling and the immediate-DM branch) and frontend (`activity/client/src/clanConfigTable.ts`
  287-293/415/420-423, `main.ts` 361-378, `types.ts`'s guest-add return type).
- **Rule c/d**: already correct as-is (guest player/clan additions seed the pool without DMing) —
  verify only, no code change beyond rule e's removal.
- **Rule f**: stop the automatic destructive cascade on uncheck — remove the call to
  `_cleanup_local_pool_for_plain_clan_deactivation_sync` from
  `detach_guild_from_shared_clan_on_deactivation`'s non-shared branch (triggered today from
  `handle_post_clan_config`'s `newly_deactivated_tags` loop). Add a new endpoint
  `POST /api/cwl/enrollment/guest-clan/remove` that: rejects any `clan_tag` in
  `resolve_guild_member_clan_tags` (family clans can never be removed this way — fail closed at
  the endpoint, don't rely solely on the cleanup function's internal protection), then calls
  `_cleanup_local_pool_for_plain_clan_deactivation_sync` (reused as-is) and
  `db.delete_cwl_event_clan_sync` (new method — DELETEs the `cwl_event_clans` row entirely, per
  your confirmation that guest clans have no config worth preserving). Frontend: a "Remove"
  button next to each guest clan's checkbox (`clanConfigTable.ts`), guarded by a new `is_guest`
  field computed in `_build_clan_config_payload_sync` from `resolve_guild_member_clan_tags`
  (not the broader `get_guild_clans_including_member_config`, which would misclassify a
  merely-subscribed non-family clan as a guest). Destructive-action confirmation reuses the
  existing inline Yes/Cancel pattern already on this screen (`clanConfigTable.ts:395-407`).
- **Rule g**: new "Remove Guest Players" UI — `GET /api/cwl/enrollment/guest-players` (lists
  guest-invited players plus orphaned ex-guest-clan players still in the pool) and
  `POST /api/cwl/enrollment/guest-players/remove` (per tag: `delete_cwl_signup_sync` +
  `delete_cwl_assignment_sync`, the same two calls the cleanup function already uses — does
  **not** touch `cwl_player_season_status`, which must survive removal per rule h). Frontend:
  multi-select checkbox list + Remove/Cancel, same confirmation pattern as rule f.

Both new remove actions are immediate (POST-on-click), not routed through the batched
Save/`working` state — matching the existing `onEvict`/guest-add pattern already on this screen.

### 4. DM reliability fix (independent bug, fix first)

In `CacheManager.send_user_dm` (`cache_manager.py:1288-1344`), replace the
`except discord.DiscordServerError: raise` with a retry loop (up to 3 attempts, short backoff)
around the `await user.send(...)` call; only return `False` (matching every other failure path)
once retries are exhausted — no signature change, so all ~7 existing callers keep working and
immediately stop being exposed to the crash. Add a `send_user_dm_detailed(...)` variant
returning `(bool, "sent"|"blocked"|"failed")` so the CWL DM loop can distinguish
`discord.Forbidden` (blocked/DMs closed — no retry needed, just report) from exhausted-retries
(report separately).

Extract the CWL DM-sending loop (1740-1749) into a shared
`_send_cwl_enrollment_dm_batch(event_id, guild_id, season, dm_targets)` helper (used by both
`start_cwl_enrollment` and the new rule-h "notify new members" function) that applies the DM
guard, the global `dm_sent` dedup check (rule h — added in Phase 5), and collects
`blocked`/`failed_after_retries` lists. Surface these in the existing ephemeral summary message
(`CwlStartEnrollmentConfirmView._on_confirm`, `ui_cwl_roster.py:995-1060`, which already renders
`start_cwl_enrollment`'s summary dict) — no new UI surface needed.

### 5. Rule h — cross-guild dedup + "Notify New Pool Members" button

- `start_cwl_enrollment`'s seeding loop looks up each candidate's existing global status
  (`get_cwl_player_season_status_bulk_sync`) before appending to `signups_to_create`, seeding the
  *real* current status instead of hardcoded `"pending"` — so a player already confirmed via
  another guild's DM shows correctly the instant a second guild pools them.
- `_send_cwl_enrollment_dm_batch` skips anyone with `dm_sent=True` globally — this alone
  satisfies "never DM the same player twice for the same season," with no extra code needed in
  `start_cwl_enrollment` beyond calling the shared helper.
- New `notify_new_cwl_pool_members(guild_id, season)`: for an event past `draft`, re-resolves the
  actual current pool (extract the existing merge logic from `_build_enrollment_payload_sync`,
  `web_bridge.py:472-549`, into a shared `resolve_cwl_pool_players_for_event` helper used by
  both), then runs it through `_send_cwl_enrollment_dm_batch` — the global dedup means this is
  naturally "only the not-yet-contacted ones."
- New gating helper `has_cwl_pool_members_missing_dm(guild_id, season)`: true when any pooled
  player_tag has `dm_sent=False` globally. Drives a new button on row 2 of
  `add_cwl_management_components`, shown only when `event.status not in ("draft", "cancelled")`
  and this helper is true — appears in both the Hub message and `/clan management` automatically
  since both share this one component.
- Survives remove-then-re-add automatically: `cwl_player_season_status` is never touched by the
  rule f/g removal paths, so a re-added guest player's `dm_sent=True` is still there when the
  dedup check runs again.

## Phased delivery

Each phase independently shippable and testable; run `.\run_tests.ps1` (never raw `pytest`) and
add a `changelog.txt` entry with the real pass count at the end of each, per this repo's
conventions (`.github/copilot-instructions.md`).

1. **DM reliability fix** (§4) — `cache_manager.py`, `QBdiscocmdshelper_cwl.py`,
   `ui_cwl_roster.py`, translations. Isolated bug fix, no behavior change to what gets pooled.
2. **Schema** (§1) — `db_manager.py` only: new table + methods, unused by any caller yet, zero
   behavior change.
3. **Rule b** (§2) — `QBdiscocmdshelper_cwl.py` only.
4. **Rules c/d/e/f/g** (§3) — `web_bridge.py`, `QBdiscocmdshelper_cwl.py`, `db_manager.py`,
   `clanConfigTable.ts`, `main.ts`, `types.ts`.
5. **Rule h** (§5, depends on 1-4) — `QBdiscocmdshelper_cwl.py`, `ui_cwl_roster.py`,
   `web_bridge.py`, translations.

## Critical files

- `qapbot/QBdiscocmdshelper_cwl.py` — `start_cwl_enrollment`, DM batch helper, propagation,
  notify-new-members function, cleanup/detach functions
- `qapbot/web_bridge.py` — guest player/clan endpoints, clan-config save/deactivation, new
  remove endpoints
- `qapbot/db_manager.py` — new schema table + methods
- `qapbot/cache_manager.py` — `send_user_dm` retry fix
- `qapbot/ui_cwl_roster.py` — DM response button, management hub button surface
- `activity/client/src/clanConfigTable.ts`, `main.ts`, `types.ts` — guest UI changes

## Implementation status log

- **Phase 1 (DM reliability fix): DONE (2026-08-18).** `cache_manager.py`'s `send_user_dm` now
  delegates to a new `send_user_dm_detailed()` with an internal retry loop
  (`DM_SEND_MAX_RETRIES = 3`) for `discord.DiscordServerError`, never re-raising; returns
  `(bool, "sent"|"blocked"|"failed")`. `QBdiscocmdshelper_cwl.py`'s DM loop extracted into
  `_send_cwl_enrollment_dm_batch()`, collecting `blocked`/`failed` recipient-name lists into
  `start_cwl_enrollment()`'s summary dict. `send_cwl_signup_template_dm()` now returns
  `(bool, outcome)`; its other caller (`handle_post_cwl_enrollment_guest`, web_bridge.py) updated
  to unpack. `ui_cwl_roster.py`'s `_on_confirm` + `en.json`/`de.json` now surface blocked/failed
  recipients by name in the admin-facing Start Enrollment summary. New tests:
  `TestSendUserDmDetailed`/`TestSendUserDm` (`tests/unit/test_cache_manager_async.py`),
  `test_one_recipients_dm_failure_does_not_abort_the_rest_of_the_batch`
  (`tests/integration/test_cwl_start_enrollment.py`); ~20 existing CWL tests + 2
  `test_web_bridge.py` guest-invite tests updated for the new signatures. Full suite: 2074
  passed, 4 deselected. Changelog: `2026-08-18 (63)`.
- **Phase 2 (schema: `cwl_player_season_status`): DONE (2026-08-18).** New table + 9 new
  `db_manager.py` methods added (see §1 of this plan for the full list), all additive/unused so
  far — no caller wired yet (Phase 5 wires the dedup/propagation logic). New
  `TestCwlPlayerSeasonStatus` test class in `tests/unit/test_db_manager_cwl_roster.py` (6 tests).
  Verified schema on a fresh DB via `PRAGMA table_info`. Full suite: 2080 passed, 4 deselected.
  Changelog: `2026-08-18 (64)`.
- **Phase 3 (rule b: family-wide pool): DONE (2026-08-18).** `start_cwl_enrollment()`'s
  candidate-pool line now unions the whole family with every clan ever configured for the event,
  regardless of `participating`. New regression test reproduces the exact original bug report
  (Lucas's multi-account scenario) and passes. Full suite: 2081 passed, 4 deselected. Changelog:
  `2026-08-18 (65)`.
- **Phase 4 (rules c/d/e/f/g: guest pool management): DONE (2026-08-18).**
  - Rule e: "Send enrollment DM immediately" checkbox removed (backend + frontend).
  - Rule c/d: verified already-correct (no code change needed beyond rule e's removal).
  - Rule f: uncheck+Save no longer purges the pool; new `remove_cwl_guest_clan()` +
    `POST /api/cwl/enrollment/guest-clan/remove` + frontend Remove button do the full removal.
  - Rule g: new `GET /api/cwl/enrollment/guest-players` +
    `POST /api/cwl/enrollment/guest-players/remove` + frontend "Remove Guest Players…" panel.
  - Changelog: `2026-08-18 (66)`, `(67)`. Full suite: 2087 passed, 4 deselected. Frontend
    `tsc --noEmit` clean throughout.
  - **Follow-up bug found live in DEV (2026-08-18, changelog (70)):** all three new endpoints
    404'd unconditionally — `activity/server/src/index.ts` (a separate Cloudflare Worker that
    authenticates the Activity user and proxies to the Python bridge) never got the matching
    proxy routes added alongside `web_bridge.py`'s. Fixed; redeployed to DEV
    (`cwl-clan-config-server-dev`). **Any future new `/api/cwl/...` endpoint needs a route in
    BOTH `qapbot/web_bridge.py` AND `activity/server/src/index.ts` — easy to forget since they
    live in different languages/directories.**
- **Phase 5 (rule h: cross-guild dedup + Notify New Members button): DONE (2026-08-18).**
  Deviated from this section's original sketch in two ways, both for real architectural reasons
  discovered during implementation (see the code's own comments for details):
  - `notify_new_cwl_pool_members` and the button-gating check ended up in TWO different places
    with different sync/async shapes, not one shared `resolve_cwl_pool_players_for_event`
    helper: `add_cwl_management_components` (the button's host) is a plain **synchronous**
    function that runs directly on the event loop (Pitfall 26) — it cannot await the heavier
    `_build_enrollment_payload_sync` reuse `notify_new_cwl_pool_members` itself uses. The gating
    check (`has_cwl_pool_members_missing_dm`, `QBdiscocmdshelper_cwl.py`) is therefore a
    separate, cheap, plain-sync function mirroring `start_cwl_enrollment`'s own pool
    computation instead.
  - `propagate_cwl_player_response`'s sync half is also called directly (not via the async
    wrapper) from `web_bridge.py`'s `_apply_cwl_enrollment_signup_sync`, which is itself already
    the sync half of one atomic `asyncio.to_thread()` hop.
  - `start_cwl_enrollment` now seeds each candidate's real global status instead of hardcoded
    `'pending'`; `_send_cwl_enrollment_dm_batch` skips anyone already globally DMed.
  - New tests cover cross-guild DM-button propagation, admin manual-confirm propagation,
    `notify_new_cwl_pool_members`, and button-gating (present/absent). Full suite: 2095 passed,
    4 deselected. Changelog: `2026-08-18 (68)`.

**All 5 phases complete.**

## Live-testing follow-up fixes (post-Phase-5, all 2026-08-18)

Bugs found by the project owner live-testing in DEV after Phase 5 shipped, each with a
regression test verified via a revert-confirm-restore cycle (temporarily undo the fix, confirm
the new test fails, then restore it):

- **(69)** Cloudflare Worker proxy (`activity/server/src/index.ts`) never got routes for the
  three new Phase 4/5 endpoints — `qapbot/web_bridge.py` had them, but every request 404'd
  before reaching the bridge. Now documented as **Pitfall 30**
  (`qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md`): any new `/api/cwl/...` endpoint needs a route in
  BOTH files.
- **(71)** Pasting into the Guests search box could swallow the native `input` event inside the
  Activity's sandboxed iframe, leaving the search permanently dead until reopening the view.
  Fixed with a redundant `paste` listener in `clanConfigTable.ts`.
- **(73)** `remove_cwl_guest_clan()` never purged the local pool for a SHARED guest clan (only
  the never-shared branch did) — fixed by running the local cleanup unconditionally.
- **(73)** A clan added to the roster while left UNCHECKED never seeded its members into the
  pool at all — the only post-Start-Enrollment seeding hook only fired for clans newly turning
  `participating=True`. Fixed by also triggering it (visibility-seed only, never an actual
  assignment) for any clan genuinely new to the roster this save, regardless of participating
  state.

**Note:** (73)'s two fixes are Python backend changes and need the DEV bot process restarted to
take effect — unlike the Cloudflare deploys, which take effect immediately on `wrangler deploy`/
`pages deploy`.
