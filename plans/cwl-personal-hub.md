# Personal CWL Hub — Implementation Plan

## Context

`CWL_ROSTER_PLANNING_PLAN.md` (project root) designed a "Personal CWL Hub" — a per-guild
anchored Discord message where members view their CWL status and set two personal
preferences: a permanent CWL opt-out toggle and a default preferred league-tier — as
**Phase 2 Slice 3**. Independent code verification this session confirmed the rest of that
plan (admin-side season setup, template DM carry-over, the Activity-based assignment board)
was built and heavily live-tested, but this slice never shipped: the `guild_config.cwl_hub_*`
and `user_players.cwl_permanent_optout`/`cwl_default_preferred_league_rank` columns exist in
the schema and are *read* in several places (DM-exclusion logic, board sort order, signup
pre-fill), but **nothing writes them and no UI exists**. The intended outcome is to close
that gap: give members a self-service way to set these preferences, and make the existing
read sites (which already silently assume someone will eventually populate the columns)
finally have real data.

Two research passes and one design pass (all read-only, this session) produced the plan
below. All file:line references were spot-checked against the live repo, not taken on
faith from the plan doc's own claims.

---

## Scope confirmation from spot-checks

1. **League-rank pre-fill already exists and needs no new work.**
   `_start_cwl_enrollment_locked()` (`qapbot/QBdiscocmdshelper_cwl.py:2354`) already seeds
   `signups_to_create` with `participant["preferred_league_rank"]`, sourced from
   `cwl_default_preferred_league_rank` via `get_current_clan_members_sync()`
   (`db_manager.py:4507`) / `get_all_players_for_discord_ids_sync()` (`:4567`). This plan only
   needs to make the *write* path exist so that pre-fill finally has data to read.
2. **The admin channel-config idiom is confirmed real**: `ChannelSlotConfig` /
   `DEFAULT_CHANNEL_SLOTS` / `ChannelConfigurationView` in `qapbot/ui_clan_management.py:2774-2848`.
   Its own docstring: *"Adding a new channel slot... is a new instance in
   DEFAULT_CHANNEL_SLOTS below — no new select/button/handler code needed."* Verified by direct
   read — `DEFAULT_CHANNEL_SLOTS` currently holds `registration`, `war`, `cwl_management`
   entries; `CWL_CONFIG_CHANNEL_SLOTS` filters to `("cwl_management",)` for the `cwl_settings`
   screen. Adding a `cwl_hub` slot and including it in that filter is a two-line change with
   zero new UI code required.
3. **Pre-existing quirk to match, not fix**: `ChannelConfigurationView._on_apply()`
   (`ui_clan_management.py:2992-3001`) only force-reposts `registration` immediately after a
   channel change — `cwl_management` (and the new `cwl_hub` slot) only pick up the change on
   the next periodic maintenance cycle, not instantly. This plan intentionally matches that
   existing behavior rather than silently fixing it as a side effect. (Optional one-line
   follow-up noted in Phase 3 if instant repost-on-channel-change is later wanted for both.)
4. **No custom `interaction_check()` needed on the new view.** The `fully_initialized` gate is
   applied globally to every `discord.ui.View` via a monkey-patch in `QBcore.py:188-210` — not
   per-view. `CwlManagementHubView` relies on this and has no override; `CwlHubView` should
   mirror that (not `RegistrationView`'s `TrackedView`-based custom override, which is solving
   a different problem).
5. **Confirmed real landmine, independent of this feature but adjacent to it.**
   `get_user()` (`db_manager.py:8828-8858`) builds each player dict from an explicit 6-key
   allowlist that omits `cwl_permanent_optout`/`cwl_default_preferred_league_rank`, and
   `_replace_user_players_rows()`'s INSERT (`:8959-8972`) mirrors that same omission. Any call
   to `CACHE.persist_user(discord_id)` — `set_primary_account()`, `unlink_player()`, a fresh
   `/link` — does a full delete+reinsert of every `user_players` row for that `discord_id` and
   will silently zero both CWL columns back to defaults. This is worth closing (Phase 0) before
   real preference data exists to lose, even though the Hub's own writes go through a separate,
   safe direct-SQL path (Phase 1) that never touches this pipeline.
6. **`get_current_cwl_event_sync(guild_id)`** (`QBdiscocmdshelper_cwl.py:239-257`) is the
   correct "which event is relevant to this member right now" resolver for **My CWL Status** —
   not the admin's `cwl_selected_season` UI-selection state.
7. **No new read method needed.** `get_all_players_for_discord_ids_sync([str(discord_id)])`
   (`db_manager.py:4512-4570`) already returns both CWL columns per linked account — exactly
   what both ephemeral screens need for reading.

---

## Phase 0 — close the silent-reset landmine (independent, do first)

Cheap, isolated fix before real preference data exists to lose.

- `qapbot/db_manager.py`, `get_user()` (~8848-8858): add
  `"cwl_permanent_optout": bool(p_row["cwl_permanent_optout"])` and
  `"cwl_default_preferred_league_rank": p_row["cwl_default_preferred_league_rank"]` to the
  per-player dict (the `SELECT *` already fetches both columns).
- `qapbot/db_manager.py`, `_replace_user_players_rows()` (~8959-8972): extend the INSERT's
  column list and values tuple with both columns, sourced from the same dict.
- **Test**: extend `tests/unit/test_db_manager_cwl_roster.py` — set opt-out via the Phase 1
  write method, call `set_primary_account()`/`unlink_player()` for a *different* linked
  account of the same `discord_id`, re-read, assert the flag survived.

---

## Phase 1 — `db_manager.py` write path (direct SQL, same pattern as existing CWL reads)

Add three sync methods next to `get_current_clan_members_sync`/`get_player_links_sync`
(`db_manager.py:4439-4619`), same `self._sync_conn()` pattern — direct SQL, not routed through
`CACHE.user_accounts`/`save_user()` (that pipeline is what Phase 0 is patching, not what these
writes should use):

```python
def set_cwl_optout_sync(self, discord_id: str, player_tag: str, optout: bool) -> bool:
    """UPDATE user_players SET cwl_permanent_optout=? WHERE discord_id=? AND player_tag=?.
    Returns True iff a row was updated."""

def set_cwl_preferred_league_rank_sync(self, discord_id: str, player_tag: str,
                                        league_rank: Optional[str]) -> bool:
    """UPDATE user_players SET cwl_default_preferred_league_rank=? WHERE discord_id=? AND
    player_tag=?. league_rank=None is a valid explicit value (clears to 'No preference')."""

def set_cwl_optout_for_all_accounts_sync(self, discord_id: str, optout: bool) -> int:
    """UPDATE user_players SET cwl_permanent_optout=? WHERE discord_id=? (no player_tag
    filter). Returns rowcount, for the bulk opt-out/opt-in-all buttons."""
```

Two separate setters rather than one combined method with sentinel defaults — each UI
control (toggle button, Select) calls exactly one setter with a concrete value.

- **Test**: extend `tests/unit/test_db_manager_cwl_roster.py` — single-account set/clear
  round-trip for both columns; league-rank explicit clear to `None`; bulk set-all scoped to
  one `discord_id` only, correct rowcount; no-op on a nonexistent `(discord_id, player_tag)`
  pair returns `False`/`0` without raising; confirm the write is immediately visible to
  `get_current_clan_members_sync`/`get_all_players_for_discord_ids_sync`/`get_player_links_sync`
  with no cache warm-up needed.

---

## Phase 2 — admin config entry point (`cwl_hub_channel_id` + enable toggle)

### 2a. Channel selection

`qapbot/ui_clan_management.py`:
- Add `_track_cwl_hub_channel_change(guild_id_str, old_channel_id, new_channel_id)`, exact
  structural copy of `_track_cwl_management_channel_change` (`:2806-2811`), writing
  `CACHE.server_config[guild_id_str]["_old_cwl_hub_channel_id"]`.
- Add a `ChannelSlotConfig` entry to `DEFAULT_CHANNEL_SLOTS` (`:2814-2835`):
  ```python
  ChannelSlotConfig(
      key="cwl_hub",
      label="Personal CWL Hub",
      config_key="cwl_hub_channel_id",
      disable_flag_keys=("cwl_hub_message_enabled",),
      on_apply=_track_cwl_hub_channel_change,
  ),
  ```
- Extend `CWL_CONFIG_CHANNEL_SLOTS` (`:2845-2847`) to
  `slot.key in ("cwl_management", "cwl_hub")` — the existing "Configure Channels" button on
  the `cwl_settings` screen then renders a second channel-select row automatically, no new
  select/button/handler code (per the class's own docstring guarantee, verified above).

### 2b. Enable/disable toggle

`qapbot/ui_cwl_roster.py`, `add_cwl_settings_components()` (`:41-102`): new button (row 4),
structural copy of the existing toggle at `_make_cwl_settings_toggle_callback()`
(`:144-179`) — permission check → defer → guard "no channel set" (new i18n key) → flip
`guild_config["cwl_hub_message_enabled"]` → `persist_server_config()` →
`QBcore.spawn_tracked("repost-cwl-hub-msg", repost_cwl_hub_messages(only_if_not_bottom=False))`
→ refresh the settings screen in place.

### 2c. Settings-screen embed

`qapbot/QBdiscocmdshelper_cwl.py`, `format_clan_management_cwl_settings()` (`:288-364`): add a
`personal_hub_block`, structurally identical to the existing `hub_block` (`:321-325`) but
reading `cwl_hub_channel_id`/`cwl_hub_message_enabled`.

- **Test**: extend `tests/discord/test_ui_cwl_roster.py` — channel-slot presence, toggle
  label/state, guard-when-no-channel-set. Extend whatever existing test covers
  `CWL_CONFIG_CHANNEL_SLOTS`/`DEFAULT_CHANNEL_SLOTS` round-tripping (grep for
  `CWL_CONFIG_CHANNEL_SLOTS` in `tests/` first) with the new slot.

---

## Phase 3 — repost wiring (`repost_cwl_hub_messages`)

`QapBot.py`, new function immediately after `repost_cwl_management_messages()`
(`:4028-4074`), exact structural copy using the generic `repost_anchored_message()` driver
(`:3772-3968`):

```python
async def repost_cwl_hub_messages(*, only_if_not_bottom: bool = False,
                                    bump_cooldown_seconds: int = PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS) -> None:
    from qapbot.ui_cwl_roster import build_cwl_hub_content_and_view
    await repost_anchored_message(
        log_label="Personal CWL Hub",
        enabled_key="cwl_hub_message_enabled",
        channel_key="cwl_hub_channel_id",
        message_id_key="cwl_hub_message_id",
        old_channel_key="_old_cwl_hub_channel_id",
        last_bump_key="cwl_hub_message_last_bump_iso",
        build_content_and_view=build_cwl_hub_content_and_view,
        dev_mode_allowed_channel_id=CONFIG.dev_playerregistration_channel_id or None,
        only_if_not_bottom=only_if_not_bottom,
        bump_cooldown_seconds=bump_cooldown_seconds,
    )
```

Call it alongside the other two reposts at all three existing sites: `QapBot.py:816-817`,
`:1939-1940`, `:3331`/`:3342`.

- **Test**: extend the repost-lifecycle tests that already cover
  `repost_playerregistration_messages`/`repost_cwl_management_messages` (delete-on-disable,
  channel-migration only-after-Discord-confirmed-delete, cooldown-gated bump) with the
  `repost_cwl_hub_messages` mirror case.

---

## Phase 4 — `CwlHubView` and the ephemeral flows

### 4a. Anchored message content

`qapbot/ui_cwl_roster.py`, new `build_cwl_hub_content_and_view(channel, guild_id_int)` (the
exact callback shape `repost_anchored_message` expects). Static-ish embed (per-guild, not
per-user) with a short explainer + the two buttons below — no live per-member data baked in,
so unlike `CwlManagementHubView` this needs no `refresh_*_on_mutation` companion function;
the ephemeral follow-ups it opens are rebuilt fresh on every click.

### 4b. `CwlHubView` — persistent view, structural copy of `CwlManagementHubView`

```python
class CwlHubView(discord.ui.View):
    """Persistent per-guild anchored message (CWL_ROSTER_PLANNING_PLAN.md Phase 2 Slice 3) —
    player-facing counterpart to CwlManagementHubView. Buttons open ephemeral, per-user
    follow-ups; the shared anchored message is never edited by either."""
    def __init__(self) -> None:
        super().__init__(timeout=None)
        # two @discord.ui.button methods: custom_id="cwl_hub_my_status" / "cwl_hub_my_preferences"
```

Registered in `QapBot.py`'s `_setup_hook()` next to the other two persistent views
(`:2842-2850`):
```python
QBcore.bot.add_view(CwlHubView())
```

### 4c. "My CWL Status" — read-only

`custom_id="cwl_hub_my_status"`: defer ephemeral → `players =
db.get_all_players_for_discord_ids_sync([str(interaction.user.id)])` → `event =
get_current_cwl_event_sync(interaction.guild.id)` (no event → "no CWL event configured" and
return) → build one status line per linked account (signup status, assigned clan if any,
opt-out flag if set) → `followup.send(text, ephemeral=True)`, no view (pure read-only).

### 4d. "My Preferences" — read/write, in-place edits

New short-lived `CwlPreferencesView(discord.ui.View)` (NOT persistent/`add_view()`-registered
— scoped to one ephemeral interaction, like `AccountActionView`). Modeled directly on
`AccountActionView`'s account-select-plus-buttons layout
(`qapbot/ui_registration.py`, class ~578, `_build_player_select` ~992-1045, `_on_player_select`
~1150-1199):

- **Row 0 — account `Select`**: one option per linked account, built from
  `CACHE.user_accounts[user_id]["players"]` for display fields only (name/tag/verified emoji
  — identity fields are safe from the cache). **Never** read the two CWL columns off these
  cached dicts (Phase 0 fixes the cache round-trip, but the Preferences screen should still
  fetch fresh via `get_all_players_for_discord_ids_sync` on open and after every account
  switch, since it's the authoritative source either way).
- **Row 1 — opt-out toggle button**: label/style reflect the selected account's live
  `cwl_permanent_optout`. Callback: `db.set_cwl_optout_sync(discord_id, selected_tag, not
  current)`, then `interaction.response.edit_message()` to rebuild in place.
- **Row 2 — league-tier `Select`**: options from `CWL_LEAGUE_RANKS`
  (`ui_cwl_roster.py:26-34`) plus a leading sentinel `"__none__"` option labeled "No
  preference" (not a 20th entry appended to the shared list — that list is also used
  elsewhere, e.g. `/cwl signup`'s picker per the original plan doc, which may not want a
  "No preference" option mixed into the real ladder). Callback maps `"__none__"` → `None`,
  calls `set_cwl_preferred_league_rank_sync`, rebuilds in place.
- **Row 3 — bulk pair**: "Opt out ALL my accounts" / "Opt back in ALL my accounts" →
  `set_cwl_optout_for_all_accounts_sync(discord_id, True/False)`, rebuild in place (refresh
  the toggle button too, since the bulk action may have changed the selected account's state).

All writes end with `interaction.response.edit_message(...)` — the shared anchored message is
never touched.

- **Test**: new `tests/discord/test_ui_cwl_hub.py` (parallel to existing
  `test_ui_registration.py` `AccountActionView` tests): button presence/labels, account-select
  rebuild-in-place, opt-out toggle persists (assert via direct DB read, not cache), league
  "No preference" round-trips to `NULL` and back, bulk-apply scoped to caller's own accounts
  only, multi-account picker for 0/1/2+ linked accounts, My CWL Status for "no event yet".

---

## Phase 5 — i18n

New `cwl.hub.*` namespace in `qapbot/translations/en.json` and `de.json`, mirroring the
sibling `cwl.management.*` block's shape (`en.json:887+`):

```
cwl.hub.title / .description
cwl.hub.button_my_status / .button_my_preferences
cwl.hub.no_event
cwl.hub.status_pending / .status_confirmed / .status_declined / .status_withdrawn / .status_not_signed_up
cwl.hub.account_line / .assigned_clan / .optout_active_note
cwl.hub.preferences_title / .account_select_placeholder
cwl.hub.button_optout / .button_optin
cwl.hub.league_select_placeholder / .no_preference
cwl.hub.button_optout_all / .button_optin_all
```

Plus, in the existing `cwl.settings.*` block:
```
cwl.settings.button_activate_personal_hub / .button_deactivate_personal_hub
cwl.settings.no_personal_hub_channel_set
cwl.settings.personal_hub_block_title / .personal_hub_status / .personal_hub_channel
```

Verify en/de key-set parity via whatever existing i18n test enforces it (per this session's
earlier tracker-plan verification, an automated key-parity check already exists and passed at
87+13 tracker keys — find and extend it, don't hand-diff).

---

## Suggested order / incremental commits

1. **Phase 0** — landmine fix, ships alone with its own regression test.
2. **Phase 1** — write methods + unit tests, no UI yet, safe standalone.
3. **Phase 2** — admin channel slot + toggle + settings embed (configurable, nothing posts yet).
4. **Phase 3** — repost wiring (message starts appearing).
5. **Phase 4** — `CwlHubView` + `CwlPreferencesView` + registration (feature goes live).
6. **Phase 5** — i18n, done alongside Phase 4 (every string must go through `t()` before Phase 4 can ship).

## Verification

- `.\run_tests.ps1` must pass after each phase before moving to the next.
- Per this project's tracker workflow (Cardinal Rule 15 / CLAUDE.md), file this as a tracker
  item and post a manual test case before marking Phase 4 (the only phase with real
  end-to-end user-visible behavior) as implemented.
- Manual end-to-end check once Phase 4 lands: configure the Hub channel on a test guild,
  enable it, confirm the message posts; click "My Preferences", switch between 2+ linked
  accounts, toggle opt-out, set/clear league preference, use bulk opt-out-all; click "My CWL
  Status" and confirm it reflects the same values; confirm a subsequent `/bug`-style unrelated
  account mutation (e.g. `set_primary_account`) doesn't reset the preferences (Phase 0's
  regression case, exercised live).
