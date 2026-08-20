# CWL Enrollment Redesign — Player Pool & Global Status

## Requirements specification (project owner, verbatim intent, condensed)

**Rule a — DM the whole pool.** Starting enrollment must DM every player in the player pool,
not just players whose current clan happens to be marked `participating`.

**Rule b — pool membership ignores the participating toggle.** When a guild starts a new
season, *all* members of *all* the guild's clans go into the player pool, regardless of each
clan's `participating` status. Check the current code against this and correct it if it doesn't
already work this way.

**Rule c — guest players join the pool immediately.** An individual guest player invited before
enrollment starts is added to the player pool right away, and receives the enrollment DM once
enrollment actually starts (not before).

**Rule d — guest clans join the pool immediately.** A clan invited as a guest has *all* of its
current members added to the player pool right away, same timing as rule c.

**Rule e — remove the "send DM immediately" checkbox.** Delete the "Send enrollment DM
immediately (players only)" checkbox and its backend code entirely from "Configure Participating
Clans." It will be replaced by a different mechanism (rule h) — don't try to guess a
replacement now.

**Rule f — unchecking a guest clan must stop being destructive.**
- Adding a guest clan still adds all its members to the pool immediately (unchanged).
- Unchecking a guest clan now only removes it from the guild's active clan roster — its members
  **stay in the pool**. Today unchecking deletes them from the pool; that must stop.
- Add a new **Remove** button next to each guest clan's checkbox (placed to its right) that does
  the full, deliberate removal instead: takes the clan off the season's roster *and* removes its
  members from the pool. Player removal moves entirely from "uncheck" to this button — after a
  guest clan is added, unchecking/rechecking only ever toggles roster membership, never pool
  membership.

**Rule g — a way to remove guest players directly.** Add a new UI to "Configure Participating
Clans" for removing individual guest players: a button opening a multi-select list of guest
players with Remove/Cancel actions. Removing one takes them out of the player pool and out of
whatever clan-roster assignment they may already hold.

**Rule h — notify players added after enrollment already started.**
- When a guest clan or guest player is added *after* Start Enrollment has already run, there
  needs to be a way to DM just the newly-added players.
- Track, per player, whether they've already received the enrollment DM — then a new action can
  filter the pool down to "not yet DMed" and message only those.
- Surface this as a new button in both the CWL Management Hub message and `/clan management`'s
  "CWL Season Management" view, shown only when enrollment has already started **and** at least
  one pool member hasn't been DMed yet.
- Edge case to handle explicitly: a guest player is invited, enrollment starts and DMs him, he's
  then removed from the pool, then later re-added. He must **not** appear as "never DMed" just
  because he was temporarily removed — his DM-sent status has to persist across removal and
  re-addition, which needs its own table (not something derived from current pool membership).
- The enrollment/DM status is fundamentally **per player**, not per guild or per clan — a player
  only ever needs one enrollment DM for a season, no matter how many guilds or clans have pooled
  him. This needs a normalized data model, not a per-guild/per-event flag.

**On the account-wide "linked accounts" expansion setting — leave untouched.** It already works
exactly as designed and is a distinct, separate concept from the above: when enabled, it pulls a
qualifying guild member's *other* linked player accounts into the pool even when those accounts
aren't members of any of the guild's own clans — this is what makes cross-guild invitations
easier, and deliberately expands the pool beyond the guild's own scope.

**On the DM-batch-abort bug — fix immediately, plus retry policy.** A Discord error for one
recipient must never stop the rest of the enrollment DM run. Retry a failed send up to 3 times.
If it still fails after retries, tell the admin so they can act on it. Exception: if the failure
is because the user has blocked the bot (or otherwise closed DMs), retrying is pointless — skip
the retries, but still report it to the admin as its own distinct case.

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

> **2026-08-19 update**: `_apply_cwl_enrollment_signup_sync` and its endpoint
> (`handle_post_cwl_enrollment_signup`, `POST /api/cwl/enrollment/signup`) have since been removed
> entirely — dead code with no real caller since the board's 1-click admin confirm/withdraw
> control was dropped in favor of drag-and-drop-only (see `CWL_ROSTER_PLANNING_PLAN.md`'s "Foreign
> guest conversion + purge"/"Dead-code correction" entries, which first flagged this as a known
> cleanup opportunity, and the project owner's explicit removal request that closed it out).
> `propagate_cwl_player_response`'s fan-out is now reached only via `CwlSignupResponseButton.callback`
> — the one remaining, genuinely real way a player's status can ever change.

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
  - **Corrected 2026-08-20 (live bug report).** Only the *pool-add* half was actually correct.
    Rule c's other half — "receives the enrollment DM once enrollment actually starts" — never
    worked for an individually-invited guest PLAYER: `start_cwl_enrollment` built its DM targets
    purely from `get_current_clan_members_sync(pool_candidate_tags)`, and a guest's real current
    clan is by definition none of this guild's pooled clans, so they were structurally invisible
    to that scan. They stayed in the pool un-DMed until an admin pressed rule h's "Notify New
    Pool Members."

    The underlying defect was that §5's planned shared `resolve_cwl_pool_players_for_event`
    helper was never actually built — Start Enrollment, that button, and the button's gating
    check `has_cwl_pool_members_missing_dm` each resolved "the pool" their own way, so a player
    could be in one's idea of the pool and not another's. Fixed by finally extracting it:
    `resolve_cwl_pool_dm_targets_sync(guild_id, event_id, season, preloaded_members=None)` in
    `QBdiscocmdshelper_cwl.py`, now the single source for all three. It unions current clan
    members (family ∪ every configured event clan, rule b/f), this event's `cwl_signups` rows
    (the only trace of an individually-invited guest) and shared-clan rosters, honours
    `cwl_permanent_optout` for every source, resolves `discord_id` from `user_players` when a row
    carries none, and owns the `skipped_optout`/`skipped_unlinked` counts. Sending was already
    shared via `_send_cwl_enrollment_dm_batch`. Two side effects, both corrections: the button no
    longer DMs permanently-opted-out players, and no longer runs the heavy board payload builder
    (`_build_enrollment_payload_sync`) just to collect recipients. Guest CLANS were never
    affected (rule d: their members ARE current members of a pooled clan).
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
    - **Reverted 2026-08-20** — this deviation was the bug. Three independent pool computations
      (this check, the button, Start Enrollment) meant a guest player could be in one's idea of
      the pool and not another's, which is exactly what happened. The shared helper this section
      originally called for now exists as `resolve_cwl_pool_dm_targets_sync` and all three use
      it; the sync/async objection dissolves because it's a plain **synchronous** function (a
      handful of indexed lookups, nothing like `_build_enrollment_payload_sync`) that the async
      callers wrap in `asyncio.to_thread()` and the render path calls directly. See the rule c/d
      correction note earlier in this file.
  - `propagate_cwl_player_response`'s sync half was also called directly (not via the async
    wrapper) from `web_bridge.py`'s `_apply_cwl_enrollment_signup_sync`, itself already the sync
    half of one atomic `asyncio.to_thread()` hop — **removed 2026-08-19** along with the rest of
    the board's 1-click admin confirm/withdraw control (see the "2026-08-19 update" note earlier
    in this doc); `propagate_cwl_player_response` now has exactly one real caller,
    `CwlSignupResponseButton.callback`.
  - `start_cwl_enrollment` now seeds each candidate's real global status instead of hardcoded
    `'pending'`; `_send_cwl_enrollment_dm_batch` skips anyone already globally DMed.
  - New tests cover cross-guild DM-button propagation, admin manual-confirm propagation,
    `notify_new_cwl_pool_members`, and button-gating (present/absent). Full suite: 2095 passed,
    4 deselected. Changelog: `2026-08-18 (68)`. (The admin-manual-confirm propagation tests were
    themselves removed 2026-08-19 along with the dead endpoint they exercised.)

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

## Guest-player provenance (2026-08-19, new feature, project owner's spec)

A guest player added because their whole clan was invited as a guest clan must not be
individually removable — only removing the whole guest clan removes them (rule f already
established this for the pool-membership side; this closes the same gap on the removal-UI side).
Project owner's spec, verbatim: "'Remove guest players' should only show individually invited
guest players. Guest players who were added because a guest clan was invited should not be able
to be removed individually. If a user tries so... the bot should inform the user that this is a
player added by the guest clan."

**Design decision: no new "how were you added" column.** Classification is DERIVED from live
current-clan membership on every check (`get_cwl_guest_clan_tags_sync` in
`QBdiscocmdshelper_cwl.py`: every clan currently on the event roster, checked or unchecked, that
isn't part of the guild's own family) rather than persisted at write time. This is what makes both
race conditions the project owner called out fall out for free, with zero write-time flag-flip
logic:

- **Race condition 1** — adding a player individually who is already a current member of a guest
  clan already on the roster: rejected with a 409 by `POST /api/cwl/enrollment/guest` instead of
  creating a redundant signup row.
- **Race condition 2** ("clan invitation beats individual invitation") — a player individually
  invited BEFORE their clan is ever added as a guest clan is automatically reclassified as
  clan-derived the moment that clan later joins the roster: the next classification check just
  starts returning a different answer, with nothing needing to notice and flip anything.

Three endpoints changed:
- `GET /api/cwl/enrollment/guest-players` — now excludes clan-derived players from the list.
- `POST /api/cwl/enrollment/guest-players/remove` — classifies every requested tag; rejects
  clan-derived ones with a reason instead of deleting them; returns
  `{"ok": true, "removed": [...], "rejected": [...]}`.
- `POST /api/cwl/enrollment/guest` — rejects (409) an individual add whose current clan is already
  a guest clan on the roster.

Frontend: a new right-click "Remove guest player" context menu on the Manage Teams board
(`enrollmentBoard.ts`), wired in `main.ts`; the Configure Participating Clans "Remove Guest
Players" panel (`clanConfigTable.ts`) now surfaces any `rejected` entries instead of assuming every
requested removal always succeeds.

Six new regression tests in `tests/discord/test_web_bridge.py`, each verified via a
revert-confirm-restore cycle. Full suite: 2105 passed, 4 deselected. `tsc --noEmit` clean.

## Delete Season DM dedup + stale-DM cleanup (2026-08-19, live bug report)

"Delete Season" (mainly for testing/starting over) silently broke Start Enrollment for any NEW
event later created for the same season string: `cwl_player_season_status.dm_sent` is global,
keyed by `(player_tag, cwl_season)`, and deliberately NOT cascaded from `cwl_events` (so
cross-guild dedup survives event deletion) — but that also meant a deleted-then-recreated season
left every pooled player's `dm_sent=1` flag standing, so the new event's Start Enrollment silently
DMed nobody. Separately, the deleted event's already-sent DMs kept sitting in players' DMs with
live-looking (but now non-functional) Confirm/Opt Out buttons.

Fix, two parts:
- `delete_cwl_event_sync()` (`db_manager.py`) now also clears any `cwl_player_season_status` row
  entirely attributable to the event being deleted (DM sent via this event, never responded to via
  any OTHER still-existing event) — a row whose response came via a different, still-live event
  (the cross-guild shared-clan case) is left untouched, so that dedup still survives.
- New `dm_sent_via_message_id`/`dm_sent_via_channel_id` columns capture the actual DM message sent
  (via a new optional `sent_message_out` out-param on `CacheManager.send_user_dm_detailed()`, kept
  optional so it doesn't ripple to that method's many unrelated callers).
  `CwlDeleteSeasonConfirmView._on_confirm()` reads those refs before the delete, then calls new
  `cleanup_stale_cwl_enrollment_dms()` to best-effort delete (preferred over editing, per project
  owner) the now-orphaned DM messages — every failure mode there is silently non-fatal.

14 new tests, the core dedup fix verified via revert-confirm-restore. Full suite: 2118 passed, 4
deselected.

### Follow-up: make the dedup check itself self-healing (2026-08-19, same day)

The fix above only stops a FUTURE Delete Season from leaving an orphaned row — it does nothing
for a row already orphaned by an OLDER deletion that happened before that fix shipped, which is
exactly what a live retest still hit (project owner: "the DM still doesn't work... No DM for my
test user"). `get_cwl_player_season_dm_status_bulk_sync()` (the actual gate
`_send_cwl_enrollment_dm_batch` checks) now only trusts a `dm_sent=1` row if it's still traceable
to a LIVE `cwl_events` row (the event that sent the DM, or the event that recorded a genuine
response) — an orphaned row from ANY now-deleted event, however old, is excluded from the result
entirely, which reads as "never contacted." Self-healing on every check, no backfill migration
needed. 2 new tests + 2 pre-existing ones updated to back their dm_sent rows with real events
instead of synthetic ids. Full suite: 2119 passed, 4 deselected.

## Rule f gap: shared-clan uncheck still purged the pool (2026-08-19, live bug report)

Rule f's original fix (2026-08-18) only ever addressed the PLAIN (never-shared) guest-clan
uncheck path — it never touched a completely separate destructive branch inside
`detach_guild_from_shared_clan_on_deactivation`'s shared-clan handling (a 2026-08-16 fix that
predated rule f and was never revisited), which still deleted every non-`admin_override`
shared-roster player's local `cwl_signups`/`cwl_assignments` row on every mere uncheck+Save.
Live-tested report, project owner: "Members vanished from player pool after uncheck for
StayCalm" (a genuinely cross-guild-shared, "Managed by another guild" clan).

`_detach_guild_from_shared_clan_on_deactivation_sync()` no longer deletes anything — a
shared-roster player's existing local mirror is left completely untouched on uncheck now,
exactly like the plain-clan branch. The `admin_override` mirror-write behavior is unchanged.
Deletion still only ever happens via the explicit "Remove" button, shared or not. The one
existing test that directly asserted the old (now-wrong) deletion behavior is rewritten to
assert survival; a new end-to-end test hits the real `/api/cwl/clan-config` endpoint with a
genuine two-guild shared clan. Verified via revert-confirm-restore. Full suite: 2121 passed, 4
deselected.

## DRY refactor + Remove-button timing fix (2026-08-19, same day)

Prompted by reviewing the fix above, project owner asked whether it left redundant/dead code
behind, and separately reported a UI bug: a freshly-added guest clan's "Remove" button only
appeared after Save + reopen, not immediately on Add.

- **Redundancy check**: the fix above's own removed code (the deletion loop and its
  now-unused locals) was already fully cleaned up as part of that fix — nothing dead left
  behind by it specifically. A separate, PRE-EXISTING duplication was found and extracted: `
  prune_or_detach_shared_clans_before_deletion` (whole-season delete) and
  `detach_guild_from_shared_clan_on_deactivation` (single-clan uncheck) each carried
  byte-for-byte identical detach/repoint-ownership/prune-if-last logic — both already called the
  other a "narrower sibling" in their own docstrings, acknowledging it. Extracted into
  `_detach_or_prune_one_shared_clan()`; both now call it. Pure refactor, no behavior change.
- **Remove-button timing fix**: `ClanConfig.is_guest` was deliberately left unset on a
  freshly-added-but-unsaved guest clan row (client-side construction, Guests search "Add"
  click) — changed to always `true` (every clan that search can add is a guest by
  construction). A new `persistedClanTags` snapshot lets the Remove handler skip the backend
  call entirely for a row that was never actually saved (it would otherwise 404), instead just
  removing it from the local working list.
- **Admin_override-on-Remove fix** (confirmed by project owner: intended behavior is "assigned
  players remain in their rosters... becomes a guest player automatically" even when the clan is
  removed, not just on Uncheck) — the detach step's shared-roster mirror-write now stamps
  `assignment_source="admin_override"`/`locked=True` (the same live values a real drag-and-drop
  placement already carries) instead of a softer `"orphaned_on_detach"`/`locked=False` marker, so
  `_cleanup_local_pool_for_plain_clan_deactivation_sync` (which runs unconditionally right after,
  as part of `remove_cwl_guest_clan`) actually recognizes and preserves it — on Uncheck AND
  Remove alike. `locked`/`assignment_source` were confirmed not surfaced to the frontend payload
  at all, so nothing UI-facing could regress from the value change. New sibling test
  (`test_remove_guest_clan_still_preserves_a_deliberate_admin_override_placement`) proves the ONE
  exception to Remove's otherwise-total purge; existing mirror test extended to assert the new
  values. Verified via revert-confirm-restore.

Full suite: 2122 passed, 4 deselected. `tsc --noEmit` clean.

## Orphaned shared-roster local mirrors never got purged (2026-08-19, same day, live bug report)

Removing a shared guest clan could leave some of its former roster members permanently stuck in
the local player pool as oddly "individually removable" guest players the admin never actually
invited (project owner, verbatim, after I initially misdiagnosed this: "I NEVER ADDED THOSE
GUESTS MANUALLY THROUGH THE PLAYER GUEST INVITE FEATURE!!!"). Diagnosed by reading the live DEV
`qapbot.log` and querying `data/qapbot.db` read-only to confirm the actual data rather than
guessing further.

Root cause: `sync_cwl_shared_clan_roster_to_local_pools()` mirrors every shared-roster player
into this guild's own local `cwl_signups` as a `source='guest_invite'` placeholder the instant a
shared clan is added, but writes no local `cwl_assignments` row and no `origin_shared_clan_id`.
`_cleanup_local_pool_for_plain_clan_deactivation_sync`'s candidate set only ever looked at live
current members of the clan being removed and local assignments pointing at it — a player who'd
since left that clan in real life matched neither, so their mirror row was permanently invisible
to Remove's cleanup.

`remove_cwl_guest_clan()` now looks up the shared clan once and threads its id into the local
cleanup, which (new optional `shared_clan_id` param) now also includes every player still on
that shared roster in its candidate set regardless of their current live clan — the same
preservation rules (admin_override+locked, family/other-guest-clan membership, linked-account
sweep) still apply on top. New regression test verified via revert-confirm-restore. Full suite:
2124 passed, 4 deselected.

## Stale assignment pointer left behind for a protected family member (2026-08-19, same day)

The cleanup above already correctly refused to delete a genuine family-clan member's pool
membership when their local assignment happened to point at the clan being removed (e.g. a
prior-CWL-history seed placed them there before real-world data caught up to them now being a
family-clan member) — but it only ever `continue`d past them, never clearing that now-dangling
assignment. Live bug report, project owner, verbatim: "the qcrew members were falsely
auto-assigned to staycalm... after removing staycalm the error becomes obvious" — confirmed
against the live DEV DB, where their `cwl_assignments` rows still pointed at StayCalm's
already-removed tag, rendering them stuck in "Assigned to other Guild" forever instead of falling
back to Unassigned.

New `_clear_stale_assignment_if_any()` helper deletes that stale pointer (assignment only, never
the signup) for all three "protected, keep the signup" branches. A genuine deliberate
`admin_override`+`locked` placement remains fully untouched regardless of where it points. An
existing test that had been asserting the old, buggy "left completely untouched" behavior was
corrected; a new test reproduces the exact live scenario. Verified via revert-confirm-restore.
Full suite: 2125 passed, 4 deselected.

## The actual root cause: initial auto-assign seed, not just the removal-side cleanup (2026-08-19, same day)

The fix above only addressed the CLEANUP side (what happens when the clan is later removed). The
project owner correctly pushed back — the real root cause is upstream, in Start Enrollment's own
initial seed: "when staycalm gets added during the very start of adding the new season the
theqcrew members get auto assigned to staycalm and not to the qcrew as they should! when i start
the season newly without stay calm... then the auto assign works correctly."

`resolve_prior_cwl_assignments()` assigns every candidate purely by their own last REAL CWL
attack, anywhere — by design (2026-08-14 spec: "assign to wherever they last actually played,
not wherever they're currently rostered"), so a player is still placed even if their own current
clan isn't participating. But that same rule let a genuine CURRENT family-clan member's
historical last attack (for a different, also-participating clan — typically a guest clan just
added) override their own live, unambiguous current membership.

`start_cwl_enrollment()` now overrides `resolve_prior_cwl_assignments()`'s result for any
candidate who is a genuine current member of a participating clan — but ONLY ever redirects an
EXISTING entry, never creates a new one for a candidate that function correctly left out
(opted-out accounts, players with no real CWL history at all — confirmed by two existing tests
that would otherwise regress). New regression test reproduces the exact scenario, verified via
revert-confirm-restore. Full suite: 2126 passed, 4 deselected.

Both this fix and the cleanup-side fix above remain necessary — this one stops real family
members being placed wrong in the first place; that one still handles the case where a stale
pointer already exists for some other reason and the clan it points at gets removed.

## The second half of the root cause: assign_cwl_player_sync's conflict guard (2026-08-19, same day)

(87) fixed `start_cwl_enrollment`'s auto-assign to redirect a genuine current family-clan member
to their own real clan — but the symptom persisted, because the bug spanned two layers.
`assign_cwl_player_sync`'s `deliberate=False` conflict guard silently discarded that corrected
target: it saw an existing `cwl_shared_clan_players` row for a different clan, treated it as an
untouchable real placement, and mirrored the player right back there via its "orphaned_elsewhere"
path — without ever distinguishing a genuinely deliberate placement from a stale leftover.

Diagnosed by querying the live DEV DB rather than reasoning from code alone: the affected rows
carried `assignment_source='orphaned_elsewhere'`, a value written by exactly one code path, which
identified the culprit immediately. Lesson worth repeating: when a fix doesn't take, check the
actual persisted data for a distinguishing marker before theorizing about a new cause.

The guard now only defers to a conflicting shared placement when it's either genuinely deliberate
(`source='admin_override'` — unconditionally protected, matching the locked-local-assignment rule
beside it) or not contradicted by the player's own live `current_clan_tag`. New regression test
reproduces the exact live scenario; the revert-confirm step reproduced the reported symptom
precisely. The two existing tests covering the deliberate case pass unchanged. Full suite: 2127
passed, 4 deselected.

## Review finding: the mass-scale cause was still present after (88) (2026-08-19)

Requested review of (85)-(88) after several narrow fixes each passed their own test while the
live symptom persisted. The real, mass-scale cause of "the qcrew members were falsely
auto-assigned to staycalm" was still in place; (88) only masked the subset where a player's live
current clan happened to equal their auto-assign target.

`assign_cwl_player_sync`'s `deliberate=False` guard decided "this player already has a real claim
elsewhere" from `shared_clan_ids_to_clear` — a set that by construction contains EVERY
participating shared clan whether or not the player is in it (its actual job is the eviction
sweep further down, where hitting a clan the player isn't in is a harmless no-op delete). With
any shared guest clan on the roster, every auto-assigned player whose current clan didn't equal
their target therefore got an `orphaned_elsewhere` assignment pointing at that guest clan — no
relationship to it, no roster row. The assignment write wasn't even guarded by
`other_shared_row is not None`.

This matched the live DB precisely: affected players carried
`assignment_source='orphaned_elsewhere'` pointing at StayCalm while being wholly absent from
StayCalm's `cwl_shared_clan_players`. It hit every family member of a non-participating family
clan (Marines / Marines II) with history for a participating clan — whose current clan differs
from their target by definition, so (88)'s equality check never applied.

The guard now derives real competing placements from
`find_cwl_shared_clan_memberships_for_player_sync` (`assigned=1` rows), skips placements whose
roster row can't be found, and scans all placements for a deliberate one instead of trusting
enumeration order.

**Process lesson.** Four consecutive fixes targeted this symptom, each verified only by a test
built from the same mental model that produced the bug. What finally isolated it was reading the
persisted data for a distinguishing marker (`orphaned_elsewhere`, written by exactly one line)
and writing a probe for the *unfixed* general case rather than the already-fixed specific one.
`tests/integration/test_cwl_guest_clan_live_scenario.py` now drives the whole admin-level flow
end to end, so a regression anywhere along it fails even when every narrower test still passes.

## An untracked guest clan seeds nobody into the pool (2026-08-19)

Live report: "I added Hoehenloher Land as a guest clan but its members don't show up in the player
pool." Independent of the (84)-(89) chain — the affected code is untouched in HEAD.

Every pool seed here ends at `get_current_clan_members_sync()` → `user_players.current_clan_tag`.
That table is populated exclusively by `coc_cache.py`'s `update_player_info_in_user_accounts()`,
gated since the 2026-08-14 scope-bug incident on `clans.has_active_subscriptions`. And
`has_active_subscriptions` is computed in `update_all_clan_subscription_statuses()` from channel
subscriptions + guild `member_clans` + `member_families` — **a CWL guest clan is in none of those
sets**. So a guest clan nobody tracks for unrelated reasons has no member rows at all, and every
seed reads an empty list.

Live DEV DB confirmed it: `#LGYLJQVP` → `has_active_subscriptions=0`, 0 `user_players` rows;
`#2C9UR9GJY` (StayCalm) → `1`, 47 rows. Which is exactly why months of guest-clan testing never
surfaced this: StayCalm is another guild's own member clan.

New `ensure_cwl_clan_membership_tracked(clan_tags)` fetches any CWL-roster clan with zero tracked
members once from the CoC API and runs the normal population path, then the seeds work unchanged.
Called from both entry points that had the gap — `start_cwl_enrollment()` and
`auto_assign_prior_cwl_members()`. It deliberately does not flip `has_active_subscriptions`, since
that one-way-ratchets `track_war_updates` on permanently for a clan present for one season.

**Process note.** Same technique as the (89) finding, and it worked the same way: query the live
DB for the distinguishing fact rather than reasoning forward from the code. Two rows —
`has_active_subscriptions` and a member count per clan — separated "our recent fixes broke this"
from "this never worked for this kind of clan" in one step.

## Unchecking a plain guest clan wrongly showed its members as "Assigned to other Guild" (2026-08-19)

Live report, right after (90) landed: "Hohenloher Land ist not tracked on another guild so after
unchecking it, Hohenloher Land has no other cwl ongoing. Hence the players should have been moved
back to the Unassigned pool."

Rule f already made unchecking a plain guest clan purely cosmetic — its `cwl_assignments` rows
stay untouched so a re-check brings the roster straight back. The bug was one layer up:
`_build_enrollment_payload_sync` surfaced that dormant `assigned_clan_tag` regardless, and the
frontend's orphaned-column heuristic in `enrollmentBoard.ts` has no way to tell "points at a real
cross-guild shared clan" apart from "points at my own clan I just unchecked" — both are simply
absent from `clans` (participating-only). So a plain unchecked clan's leftover assignments got
mislabeled with the cross-guild "Assigned to other Guild" pseudo-column.

Fix: the payload builder now drops an assignment pointing at a non-participating clan unless
`get_cwl_shared_clan_sync` confirms it's a genuine shared clan. Dropped means `None` →
Unassigned in the UI; the DB row itself is never touched, preserving rule f's "re-check brings it
back" guarantee.

## "Assigned to other Guild" — canonical semantics (2026-08-19)

This took two rounds of live bug reports to get right, so the rule is recorded here as the single
source of truth — any future change to `_cleanup_local_pool_for_plain_clan_deactivation_sync` or
`detach_guild_from_shared_clan_on_deactivation` should be checked against this statement, not
against a specific test case.

**The pseudo-column exists for exactly one situation**: a player who is rightfully part of THIS
guild's own pool — by some measure independent of the clan being removed (a family-clan member, an
individually-invited guest, a Discord account linked to either) — but was deliberately drag-
assigned onto ANOTHER guild's clan roster. The tooltip on the column headline
(`activity/client/src/enrollmentBoard.ts`) states this to admins directly: dragging one elsewhere
evicts them from that other guild's roster, so they should be handled with care.

**It does NOT apply** to a real, current member of the clan being removed itself, even if that
membership was reinforced by a deliberate drag-and-drop into that same clan's own column. Such a
player's only connection to this guild's pool was the now-ended guest invitation — once the clan
is gone, so is their reason to be here. This is true regardless of whether the removed clan was
ever genuinely cross-guild-shared; "shared" answers "is there another guild that might still
manage this clan," not "does this specific player have a stake independent of it."

**Rule of thumb**, restated from the project owner's own correction, verbatim: *"the assigned to
other guild case serves a different purpose, namely a player that is rightfully member of the
current player pool (e.g. because he is a member of this guild) but is assigned to another
guild's roster."* Concretely, the test is always: **would this player still belong in this
guild's pool if the assignment itself didn't exist?** If yes (family clan, guest invite, linked
account) → preserve, "Assigned to other Guild." If no (their only tie was the clan just removed)
→ purge, same as any ordinary member.

**Two enforcement points, not one** — both were found to need the identical check
(`current_clan_by_tag.get(tag) != clan_tag`, gated by whether the placement even makes it into a
local row at all):
- `detach_guild_from_shared_clan_on_deactivation`'s write-time mirror (the only thing that runs on
  a mere Uncheck — the destructive cleanup below never fires there, per rule f).
- `_cleanup_local_pool_for_plain_clan_deactivation_sync`'s preservation check (runs only from the
  explicit Remove button).

Fixing only the second one (as the first pass at this did) left the bug fully intact for a mere
Uncheck, since that path never reaches the cleanup step at all — the write-time mirror was the
only thing standing between "clan just unchecked" and "player shown as Assigned to other Guild."
