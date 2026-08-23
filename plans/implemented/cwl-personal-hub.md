# Player CWL Settings Hub — Implementation Plan

## Context

`CWL_ROSTER_PLANNING_PLAN.md` (project root) designed a "Personal CWL Hub" — a per-guild
anchored Discord message where members view their CWL status and set personal preferences — as
**Phase 2 Slice 3**. Code verification confirmed the rest of that plan (admin-side season setup,
template DM carry-over, the Activity-based assignment board) shipped and was heavily live-tested,
but this slice never did: the `guild_config.cwl_hub_*` and
`user_players.cwl_permanent_optout`/`cwl_default_preferred_league_rank` columns exist in the
schema and are *read* in several places (DM-exclusion logic, board sort order, signup pre-fill),
but **nothing writes them and no UI exists**.

**2026-08-23 — scope revised by the project owner.** The feature is now specified as:

- The screen is called **"Player CWL Settings Hub"** (a second anchored-message block on the
  `/clan management` → CWL Settings screen, alongside the existing "CWL Management Hub" block),
  with its own enable/disable toggle and its own channel selection.
- The anchored message explains what the hub is for and carries **exactly one** button:
  **"Your CWL Preferences"**.
- That button opens a **Discord Activity web UI** (not an ephemeral discord.py view) showing two
  blocks: **I. the member's general preferences per linked account** and **II. this CWL season's
  actual state per account**, as a table.
- General preferences per account: **preferred league tier**, **general opt-out**, **general
  opt-in** (mutually exclusive with opt-out), and — only when opt-out is on — **"send the CWL
  invitation DM anyway"**, defaulting to **off**.
- Every preference is editable **per account and for all accounts at once**.
- **General opt-out** semantics at Start Enrollment: the account is automatically marked
  **declined**. With DM off, that's the end of it. With DM on, the invitation DM is still sent so
  the member can override their own auto-decline with the DM's own button.
- **General opt-in** (feature request from tracker **#21**, deliberately implemented as specified
  here rather than as the reporter proposed) needs a **new reply status "Auto-Confirmed"** on the
  Manage Teams board — same icon shape as Pending, light-green background instead of yellow. At
  Start Enrollment an opt-in account is set to **auto_confirmed** and **always** gets the
  invitation DM, so it can still be switched to confirmed or declined.
- Block II's table also shows the **assigned clan** (or "unassigned"), that clan's **league tier**
  and its **CWL start time**.
- Block II also shows **this season's invitation status per account** (Pending / Confirmed /
  Declined / Auto-Confirmed) and lets the member **change it right there**. Only possible once
  enrollment has started, by definition. A change made here must land **exactly as if the member
  had answered the DM** — same write, same global propagation, and the still-unanswered DM itself
  reconciled so it can't keep sitting in their inbox contradicting what they just did.
- The Activity screen should **reuse the "Configure Participating Clans" view's code** where it
  fits.
- The Activity is **member-facing, so it must be multi-language**, like every other user-facing
  surface in the bot.
- Preferences are **global per Clash account**, not per guild (confirmed 2026-08-23).
- A **`/cwl preferences`** slash command is a second entry point, so the screen stays reachable
  when a guild has the hub message disabled (confirmed 2026-08-23).

This plan supersedes the earlier two-button ephemeral-view design. What survives from it: the
Phase 0 landmine fix, the direct-SQL write-path approach, and the channel-slot/toggle/repost
wiring — all orthogonal to whether the UI is a discord.py view or an Activity.

All file:line references below were spot-checked against the live repo this session.

---

## Spot-check results (what's already true, and what the new spec breaks)

1. **The admin channel-config idiom is real and data-driven.** `ChannelSlotConfig` /
   `DEFAULT_CHANNEL_SLOTS` / `CWL_CONFIG_CHANNEL_SLOTS` in
   `qapbot/ui_clan_management.py:2774-2848`. Its docstring: *"Adding a new channel slot ... is a
   new instance in `DEFAULT_CHANNEL_SLOTS` below — no new select/button/handler code needed."*
   `CWL_CONFIG_CHANNEL_SLOTS` currently filters to `("cwl_management",)`. Adding a `cwl_player_hub` slot
   and widening that filter is a two-line change with zero new UI code.
2. **`guild_config.cwl_hub_*` columns already exist and are unused** (`db_manager.py:2067-2070`,
   migrations at `:2541-2544`, read/write at `:9185-9188` / `:9237-9302`). They are free for this
   feature — but **not under that name**; see "Naming convention" below. The bare `cwl_hub_*`
   prefix is currently split across both features (columns → player hub, `custom_id`s → admin hub,
   `ui_cwl_roster.py:1974-2010`) and is retired outright by this plan.
   Renaming is cheap because the infrastructure already exists: `_rename_column_if_present()`
   (`db_manager.py:2602-2630`) is an idempotent `ALTER TABLE … RENAME COLUMN` helper, already used
   for the `discord_id` → `dmed_discord_id` rename (`:2564`, and
   `plans/implemented/cwl-dmed-discord-id-rename.md`).
3. **`_start_cwl_enrollment_locked()` currently *skips* opted-out players entirely**
   (`QBdiscocmdshelper_cwl.py:2400-2401`: `if participant["cwl_permanent_optout"]: continue`) — no
   `cwl_signups` row at all. The new spec requires a row with `status='declined'`. This is a real
   behavior change, not an addition.
4. **`resolve_cwl_pool_dm_targets_sync()`** (`QBdiscocmdshelper_cwl.py:2557-2682`) is the single
   shared definition of "who is pooled and DM-able", used by both Start Enrollment and "Notify New
   Pool Members". Its opt-out branch (`:2675-2677`) unconditionally skips. The "DM anyway" flag has
   to land **here**, so both callers inherit it for free.
5. **League-rank pre-fill already exists.** `_start_cwl_enrollment_locked()` seeds
   `signups_to_create` with `participant["preferred_league_rank"]`, sourced from
   `cwl_default_preferred_league_rank` via `get_current_clan_members_sync()`
   (`db_manager.py:4507-4510`) / `get_all_players_for_discord_ids_sync()` (`:4567-4570`). This plan
   only makes the *write* path exist so that read finally has data.
6. **`cwl_signups.status` / `cwl_player_season_status.status` are plain `TEXT` with no CHECK
   constraint** (`db_manager.py:2183`, `:2380`) — adding `auto_confirmed` needs no migration, but
   every *consumer* that branches on the string does need reviewing (enumerated in Phase 4).
7. **Confirmed landmine, adjacent to this feature.** `get_user()` (`db_manager.py:8828-8858`)
   builds each player dict from an explicit key allowlist that omits the CWL preference columns,
   and `_replace_user_players_rows()`'s INSERT (`:8959-8972`) mirrors that omission. Any
   `CACHE.persist_user(discord_id)` — `set_primary_account()`, `unlink_player()`, a fresh `/link` —
   does a full delete+reinsert of that user's `user_players` rows and silently resets every CWL
   preference to its default. Must be closed (Phase 0) *before* real preference data exists.
8. **The Activity's screen routing is bot-driven and trivially extensible.**
   `_launch_cwl_activity()` (`ui_cwl_roster.py:793-836`) records
   `CACHE.pending_cwl_activity_screen[(guild, user)] = screen`; `GET /api/cwl/screen`
   (`web_bridge.py:1220-1247`) reads it non-destructively and defaults to `"clan_config"`;
   `main.ts` branches on it. Adding a third screen is additive.
9. **The Worker verifies caller identity server-side.** `verifiedDiscordUserId()`
   (`activity/server/src/index.ts:92-104`) validates the access token against
   `https://discord.com/api/users/@me` and returns the *real* user id, which every route forwards
   as `discord_user_id`. This is what makes a member-facing (non-admin) Activity screen safe — see
   Phase 5's account-protection rule.
10. **Both Activity halves deploy separately.** `activity/client` (Pages) and `activity/server`
    (Worker) ship via their own `deploy:dev`/`deploy:prod` scripts. See `activity/README.md`'s
    2026-08-23 incident: a client-only deploy left a new Worker route missing in PROD. Every deploy
    step in this plan means **both**.
11. **`repost_anchored_message()` deletes and reposts — it never edits in place** (`QapBot.py:3941`
    fetches the tracked message and `.delete()`s it, `:3957` sends a fresh one), and the
    startup/maintenance calls pass `only_if_not_bottom=True`, which **skips** entirely when the
    tracked message is already the channel's last message (`:3929-3933`). An anchored message
    sitting at the bottom of a quiet channel can therefore live unchanged indefinitely. This is
    what makes renaming a **persistent view's `custom_id`** cost something — see "Naming
    convention".
12. **Pre-existing quirk to match, not fix**: `ChannelConfigurationView._on_apply()`
    (`ui_clan_management.py:2992-3001`) force-reposts only `registration` immediately after a
    channel change; `cwl_management` (and the new `cwl_player_hub` slot) pick it up on the next
    maintenance cycle. Matched deliberately rather than silently fixed here.

---

## Naming convention — `cwl_hub_*` is retired

**Rule: every identifier belonging to either hub names which one it belongs to.** After this plan
there is no bare `cwl_hub_*` anything, in any layer — no config column, no `custom_id`, no
function name, no i18n key, no log label. A future reader never has to know which of two features
historically claimed the prefix, because neither does.

| | admin hub | player hub |
| --- | --- | --- |
| `guild_config` columns | `cwl_management_*` *(already correct, keep)* | `cwl_hub_*` → **`cwl_player_hub_*`** |
| `custom_id`s | `cwl_hub_*` → **`cwl_admin_hub_*`** | **`cwl_player_hub_*`** |
| view class | `CwlManagementHubView` *(keep — unambiguous already)* | `CwlPlayerHubView` |
| repost function | `repost_cwl_management_messages` *(keep)* | `repost_cwl_player_hub_messages` |
| i18n namespace | `cwl.settings.*` / `cwl.management.*` *(keep)* | `cwl.player_hub.*` |

### The two renames, and what each costs

**a) `guild_config.cwl_hub_*` → `cwl_player_hub_*` — free.** Four columns
(`_channel_id`, `_message_id`, `_message_enabled`, `_message_last_bump_iso`), zero rows of real
data anywhere, and an idempotent rename helper already exists. In `db_manager.py`: four
`_rename_column_if_present("guild_config", "cwl_hub_x", "cwl_player_hub_x")` calls in the
migration block (`~:2539`, alongside the existing `:2564` precedent), the `CREATE TABLE` body
(`:2067-2070`), `_row_to_server_config` (`:9185-9188`) and the upsert (`:9237-9302`). Also
`_old_cwl_player_hub_channel_id` → `_old_cwl_player_hub_channel_id` (an in-memory
`CACHE.server_config` key only, no column). Every later phase in this plan is written against the
new names.

**b) `CwlManagementHubView`'s `custom_id`s `cwl_hub_*` → `cwl_admin_hub_*` — costs one forced
repost.** Three ids: `cwl_hub_mode_settings`, `cwl_hub_mode_management`, `cwl_hub_refresh`
(`ui_cwl_roster.py:1974-2010`).

Per spot-check 11, live guilds' already-posted admin Hub messages carry the **old** ids inside
Discord's stored message components. `add_view()` will register only the new ones, so after the
rename a click on a not-yet-reposted message dispatches to nothing and Discord shows "This
interaction failed" — and because the maintenance repost skips a message that is already at the
bottom of its channel, a quiet CWL channel would stay broken indefinitely rather than self-heal.

Mitigation, shipped in the same commit as the rename — an idempotent one-time startup repair,
matching the `_repair_*`/Cardinal Rule 12 idiom the codebase already uses:

```python
# One-time forced repost after the cwl_hub_* -> cwl_admin_hub_* custom_id rename. Old anchored
# messages carry the OLD ids in Discord's stored components and add_view() no longer answers
# them, so they must be replaced once — and the ordinary maintenance repost won't do it, since
# only_if_not_bottom=True skips a message that's already last in its channel.
if not guild_config.get("cwl_admin_hub_customid_migrated"):
    await repost_cwl_management_messages(only_if_not_bottom=False)
    guild_config["cwl_admin_hub_customid_migrated"] = True
    await CACHE.persist_server_config(guild_id_str)
```

Persisted per guild (not a process-level flag) so it runs exactly once per guild ever, rather
than once per bot restart forever — and so a guild that configures the hub later still gets a
correct message from its own first post.

**This is the honest price of the convention**, and it is worth stating plainly: renaming (b) is
the half that carries risk, and it buys nothing functional — only that no identifier anywhere is
ambiguous about which hub owns it. Renaming (a) alone would already remove the *collision*
(`cwl_hub_*` would then unambiguously mean the admin hub's custom_ids); renaming (b) as well is
what removes the *bare prefix*, so nobody ever has to remember that rule in the first place. Both
are in the plan. If the forced repost is judged not worth it, (a) can ship without (b) — the
convention table above just gains one documented legacy exception, which is precisely the kind of
footnote this whole exercise is meant to eliminate.

---

## Data model

### `user_players` columns

| column | type | default | meaning |
| --- | --- | --- | --- |
| `cwl_permanent_optout` | INTEGER | 0 | **exists already.** "Never plan me for CWL." |
| `cwl_default_preferred_league_rank` | TEXT | NULL | **exists already.** Preferred league tier; NULL = "no preference". |
| `cwl_permanent_optin` | INTEGER | 0 | **new.** "Always plan me for CWL." |
| `cwl_optout_send_dm_anyway` | INTEGER | 0 | **new.** Only meaningful while `cwl_permanent_optout=1`. Default **0** — per the spec, fully opting out must be the easier of the two. |

**Why two booleans instead of one tri-state `cwl_participation_default TEXT`:**
`cwl_permanent_optout` already has readers in `get_current_clan_members_sync`,
`get_all_players_for_discord_ids_sync`, `get_player_links_sync`, `_build_enrollment_payload_sync`,
`enrollmentBoard.ts`'s `isOptedOut()`, `resolve_cwl_pool_dm_targets_sync` and
`resolve_cwl_pending_reminder_targets_sync`. A tri-state column would mean rewriting all of them
for no behavioral gain. Mutual exclusivity is instead enforced **in the single write path**
(setting one clears the other, in the same UPDATE), plus a defensive read-side precedence rule
documented once: **if both are somehow 1, opt-out wins.**

`get_player_links_sync` (`db_manager.py:4576-4620`) currently returns only `cwl_permanent_optout`
alongside the identity fields — it must return `cwl_permanent_optin`/`cwl_optout_send_dm_anyway`
too, since it is the authoritative live source `resolve_cwl_pool_dm_targets_sync` uses for pool
sources 2/3. Same for `get_current_clan_members_sync` and `get_all_players_for_discord_ids_sync`.

### Preference scope: global per Clash account, not per guild

**Confirmed by the project owner, 2026-08-23.** The columns live on `user_players`, keyed by
`(discord_id, player_tag)` with no guild dimension — so a member in two guilds sets "never play
CWL" once and every guild that pools that account honors it.

This is the right scope and worth writing down, because the UI could easily imply otherwise: the
hub message is per-guild (each guild configures its own channel), while the setting it edits is
not. Consequences to keep straight:

- `resolve_cwl_pool_dm_targets_sync()` and the Phase 4b seed read the preference with no guild
  filter, which is what they already do for `cwl_permanent_optout` today — no new guild-awareness
  anywhere.
- It matches how `cwl_player_season_status` already works (one global per-season answer per
  account, fanned out to every guild's mirror by rule h). A per-guild preference would be the odd
  one out, capable of contradicting a global response.
- A member who wants to play in guild A but sit out guild B handles it per season from the
  invitation DM (or block II), which is per-event and therefore per-guild. The standing preference
  is the default; the season answer is the override.
- **Block I's wording must say so** — "applies to every server that invites this account" — so
  nobody sets it expecting one guild's scope.

Should per-guild preferences ever be wanted, they are a new
`(discord_id, player_tag, guild_id)` table plus a guild argument threaded through the pool
resolver, the seed and the board's opt-out sort — explicitly out of scope here.

### New signup status: `auto_confirmed`

A fourth value alongside `pending` / `confirmed` / `declined` (and legacy-only `withdrawn`).
Written **only** by Start Enrollment's automatic seed, never by an admin action and never by a
member's own DM click — a member clicking through always produces a real `confirmed`/`declined`,
which overwrites it. It therefore means exactly one thing: *"seeded as participating from a
standing preference, still overridable."*

**`auto_confirmed` is deliberately NOT added to `ADMIN_SETTABLE_ENROLLMENT_STATUSES`**
(`web_bridge.py:1435`). An admin setting it by hand would assert a standing preference the member
never expressed; the admin's three existing choices remain the complete set.

### New `cwl_signups.source` values

`auto_optout` and `auto_optin`, alongside `template_confirm` / `template_optout` / `guest_invite`.
**Audit-only** — per Cardinal Rule 24 nothing may branch on `source`; it exists so a later "why is
this row declined?" question is answerable.

---

## Phase 0 — close the silent-reset landmine (independent, do first)

Cheap, isolated, and must land before any real preference data exists to lose.

- `qapbot/db_manager.py`, `get_user()` (~`:8848-8858`): add the CWL preference keys to the
  per-player dict (`SELECT *` already fetches them).
- `qapbot/db_manager.py`, `_replace_user_players_rows()` (~`:8959-8972`): extend the INSERT's
  column list and values tuple with the same keys, sourced from that dict.
- **Test**: extend `tests/unit/test_db_manager_cwl_roster.py` — set each preference, call
  `set_primary_account()` / `unlink_player()` for a *different* linked account of the same
  `discord_id`, re-read, assert every flag survived.

Ships alone with its own regression test, before Phase 1's columns exist — write it against the two
existing columns first, then extend it in Phase 1 for the two new ones.

---

## Phase 1 — schema + `db_manager.py` write path

### 1a. Migration

`qapbot/db_manager.py` (`CREATE TABLE` block ~`:2006`, `_add_column_if_missing` block ~`:2539`):

```python
await self._add_column_if_missing("user_players", "cwl_permanent_optin", "INTEGER NOT NULL DEFAULT 0")
await self._add_column_if_missing("user_players", "cwl_optout_send_dm_anyway", "INTEGER NOT NULL DEFAULT 0")
```

Mirror both into the `CREATE TABLE IF NOT EXISTS user_players` body so a fresh DB matches a
migrated one.

**In the same commit, rename (a) from "Naming convention" above** — four
`_rename_column_if_present("guild_config", "cwl_hub_<x>", "cwl_player_hub_<x>")` calls plus the
`CREATE TABLE` body / `_row_to_server_config` / upsert updates. Grouping it here keeps every
schema change of this feature in one migration commit, and it must land before Phase 2 starts
writing those keys.

**Rename (b)** — `CwlManagementHubView`'s `custom_id`s plus the one-time forced-repost repair —
ships in **Phase 2**, where the CWL settings screen is already being touched.

### 1b. Read-path widening

Add both columns to the SELECT list and returned dict of:

- `get_current_clan_members_sync` (`:4476`, `:4509-4510`)
- `get_all_players_for_discord_ids_sync` (`:4544`, `:4569-4570`)
- `get_player_links_sync` (`:4595`, `:4620`)

Keep the existing dict-key convention (`cwl_permanent_optout` stays as-is; the rank is already
exposed under the shorter alias `preferred_league_rank` — match that, don't rename).

### 1c. Write method

Next to `get_current_clan_members_sync`/`get_player_links_sync` (`db_manager.py:4439-4619`), same
`self._sync_conn()` direct-SQL pattern as its neighbours — **not** routed through
`CACHE.user_accounts`/`save_user()` (that pipeline is what Phase 0 patches, not what these writes
should use):

```python
def set_cwl_preferences_sync(
    self,
    discord_id: str,
    player_tag: Optional[str],           # None = every linked account of this discord_id
    *,
    mode: Optional[str] = None,          # 'none' | 'optin' | 'optout'; None = leave unchanged
    send_dm_anyway: Optional[bool] = None,
    league_rank: Optional[str] = None,
    rank_provided: bool = False,         # True + league_rank=None means "clear to no preference"
) -> int:
    """One UPDATE against user_players, scoped by discord_id (+ player_tag when given).
    Returns the rowcount.

    `mode` writes BOTH boolean columns in the same statement, which is what makes opt-in and
    opt-out structurally unable to be true at once:
        'optin'  -> optout=0, optin=1
        'optout' -> optout=1, optin=0
        'none'   -> optout=0, optin=0
    Switching away from 'optout' also forces send_dm_anyway=0, so the flag can never survive as a
    stale leftover on an account that is no longer opted out.

    `league_rank=None` is ambiguous on its own (unset vs. "clear it"), so an explicit
    `rank_provided` flag disambiguates rather than a sentinel string.
    """
```

**One combined setter, not the three separate ones the previous draft proposed.** The new UI saves
a whole row (or a whole "apply to all") at once, so per-control setters would mean the bridge
issuing three UPDATEs where one does — and would lose the atomicity that makes the mutual-exclusion
invariant hold.

- **Test**: extend `tests/unit/test_db_manager_cwl_roster.py` — each `mode` transition writes both
  booleans; `optout -> optin` clears `send_dm_anyway`; `rank_provided=True, league_rank=None` clears
  to NULL while `rank_provided=False` leaves it untouched; `player_tag=None` hits every account of
  that `discord_id` **and nobody else's**, returning the right rowcount; a nonexistent
  `(discord_id, player_tag)` returns `0` without raising; writes are immediately visible to
  `get_current_clan_members_sync`/`get_all_players_for_discord_ids_sync`/`get_player_links_sync`
  with no cache warm-up.

---

## Phase 2 — admin config entry point (channel + enable toggle + settings block)

### 2a. Channel slot

`qapbot/ui_clan_management.py`:

- Add `_track_cwl_player_hub_channel_change(guild_id_str, old_channel_id, new_channel_id)`, a structural
  copy of `_track_cwl_management_channel_change` (`:2806-2811`), writing
  `CACHE.server_config[guild_id_str]["_old_cwl_player_hub_channel_id"]`.
- Add to `DEFAULT_CHANNEL_SLOTS` (`:2814-2835`):
  ```python
  ChannelSlotConfig(
      key="cwl_player_hub",
      label="Player CWL Settings Hub",
      config_key="cwl_player_hub_channel_id",
      disable_flag_keys=("cwl_player_hub_message_enabled",),
      on_apply=_track_cwl_player_hub_channel_change,
  ),
  ```
- Widen `CWL_CONFIG_CHANNEL_SLOTS` (`:2845-2847`) to `slot.key in ("cwl_management", "cwl_player_hub")`.
  The existing "Configure Channels" button on the `cwl_settings` screen then renders a second
  channel-select row automatically.

### 2b. Enable/disable toggle

`qapbot/ui_cwl_roster.py`, `add_cwl_settings_components()` (`:41-102`): a new button on **row 1**
(which currently holds two of a possible five), structurally copying
`_make_cwl_settings_toggle_callback()` (`:144-179`):

permission check → `defer` → guard "no channel set" (new i18n key) → flip
`guild_config["cwl_player_hub_message_enabled"]` → `CACHE.persist_server_config()` →
`QBcore.spawn_tracked("repost-cwl-player-hub-msg", repost_cwl_player_hub_messages(only_if_not_bottom=False))`
→ `_refresh_parent(view, interaction, "cwl_settings")`.

custom_id: `cwl_settings_toggle_player_hub`.

### 2c. Settings-screen embed block

`qapbot/QBdiscocmdshelper_cwl.py`, `format_clan_management_cwl_settings()` (`:288-364`): add a
`player_hub_block` immediately after the existing `hub_block` (`:321-325`), same
`"Status: {emoji} {text}" / "Channel: {channel}"` shape, reading
`cwl_player_hub_channel_id`/`cwl_player_hub_message_enabled`. Register it with a third
`embed.add_field(name="", value=player_hub_block, inline=False)` between the hub and retention
fields.

### 2d. Rename (b) — the admin hub's `custom_id`s

Ships here because this phase is already inside `ui_cwl_roster.py`'s CWL settings code.

- `CwlManagementHubView._add_toggle_buttons()` (`ui_cwl_roster.py:1974-2010`):
  `cwl_hub_mode_settings` → `cwl_admin_hub_mode_settings`, `cwl_hub_mode_management` →
  `cwl_admin_hub_mode_management`, `cwl_hub_refresh` → `cwl_admin_hub_refresh`.
- Add the one-time forced-repost repair from "Naming convention" above, at the same startup point
  the other `_repair_*` routines run from, writing the per-guild
  `cwl_admin_hub_customid_migrated` marker.
- Grep `tests/` for the old ids before renaming — `tests/discord/test_ui_cwl_roster.py` asserts on
  `custom_id`s in several places.

- **Test**: extend `tests/discord/test_ui_cwl_roster.py` — the new toggle's presence/label/style in
  both states, and the guard when no channel is set. Grep `tests/` for `CWL_CONFIG_CHANNEL_SLOTS`
  and extend whatever already round-trips the slot list with the new entry. Add a case asserting
  the forced-repost repair runs exactly once per guild (second call is a no-op once the marker is
  set).

---

## Phase 3 — repost wiring (`repost_cwl_player_hub_messages`)

`QapBot.py`, new function immediately after `repost_cwl_management_messages()` (`:4034`), using the
generic `repost_anchored_message()` driver (`:3778-3968`) — whose own docstring already names "the
CWL Management/Personal Hub messages" as its intended clients:

```python
async def repost_cwl_player_hub_messages(*, only_if_not_bottom: bool = False,
                                  bump_cooldown_seconds: int = PLAYERREGISTRATION_BUMP_COOLDOWN_SECONDS) -> None:
    from qapbot.ui_cwl_roster import build_cwl_player_hub_content_and_view
    await repost_anchored_message(
        log_label="Player CWL Settings Hub",
        enabled_key="cwl_player_hub_message_enabled",
        channel_key="cwl_player_hub_channel_id",
        message_id_key="cwl_player_hub_message_id",
        old_channel_key="_old_cwl_player_hub_channel_id",
        last_bump_key="cwl_player_hub_message_last_bump_iso",
        build_content_and_view=build_cwl_player_hub_content_and_view,
        dev_mode_allowed_channel_id=CONFIG.dev_playerregistration_channel_id or None,
        only_if_not_bottom=only_if_not_bottom,
        bump_cooldown_seconds=bump_cooldown_seconds,
    )
```

Call it alongside `repost_cwl_management_messages` at all three existing sites: `QapBot.py:817`,
`:1940`, `:3348`.

- **Test**: extend the repost-lifecycle tests that already cover
  `repost_playerregistration_messages`/`repost_cwl_management_messages` (delete-on-disable,
  channel-migration only after a Discord-confirmed delete, cooldown-gated bump) with the
  `repost_cwl_player_hub_messages` mirror case.

---

## Phase 4 — enrollment semantics (the behavior half)

This phase is what makes the preferences *do* something, and is the riskiest part of the plan — it
changes what Start Enrollment produces for existing guilds. It is independently testable before any
UI exists (set the columns by hand in a test DB).

### 4a. `auto_confirmed` end to end

Grep-driven checklist of every consumer of the status string:

| site | change |
| --- | --- |
| `db_manager.get_cwl_signup_status_counts_sync` (`:4335`) | docstring only — it already returns whatever statuses exist |
| `QBdiscocmdshelper_cwl.format_clan_management_cwl_management` (`:475-477`) | add `auto_confirmed` to the `("confirmed", "declined")` tuple, **before** `confirmed`, + new i18n key |
| `split_cwl_pending_signups_by_link_sync` (`:582`) | **no change** — filters `status == "pending"`; an auto-confirmed row is not pending and must not inflate the "Ausstehend" counters |
| `resolve_cwl_pending_reminder_targets_sync` (`:2707`) | **no change** — same reasoning: "Remind Pending" must not re-ping someone whose standing opt-in already answered for them. Note it in the docstring so the omission reads as deliberate |
| `web_bridge.ADMIN_SETTABLE_ENROLLMENT_STATUSES` (`:1435`) | **no change** — see "Data model" for why |
| `web_bridge._build_enrollment_payload_sync` (`:488`, `:518`, `:588`) | none — passes the raw string through |
| `activity/client/src/types.ts` | add `'auto_confirmed'` to `EnrollmentPlayer.signup_status`'s union |
| `activity/client/src/enrollmentBoard.ts` (`:23-56`) | add to `VisibleStatus`, `STATUS_ICON`, `STATUS_LABEL` (`'Auto-Confirmed'`), `isVisibleStatus()` |
| `enrollmentBoard.ts`'s legend (`:707-709`) | new `buildLegendItem(autoConfirmedIconUrl, 'Auto-Confirmed', 'Standing opt-in seeded this — the invitation DM was still sent.')` |
| `enrollmentBoard.ts`'s `isOptedOut()` (`:65-67`) | **no change** — auto-confirmed is not opted out, so it must not sort to the bottom |

New asset `activity/client/src/assets/autoconfirmed.svg`: a byte-for-byte copy of `pending.svg`
with the rect's `fill="#c9992e"` (yellow) swapped for a light green — **`#7bc47f`** (chosen to sit
clearly apart from `gcheck.svg`'s saturated green while staying legible against the board's
`#313338` background). The white question-mark path is unchanged, per the spec's "same icon as
Pending".

### 4b. Start Enrollment seeding

`qapbot/QBdiscocmdshelper_cwl.py`, `_start_cwl_enrollment_locked()`'s seed loop (`:2398-2411`).
Replace the `continue`-on-opt-out with a derived status. Precedence, highest first:

1. **`existing_global`** (`cwl_player_season_status` for this player+season) — a real response the
   member already gave *any* guild this season always wins over a standing preference.
2. **`cwl_permanent_optout`** → `status='declined'`, `source='auto_optout'`.
3. **`cwl_permanent_optin`** → `status='auto_confirmed'`, `source='auto_optin'`.
4. otherwise → `status='pending'`, `source='template_confirm'` (unchanged).

```python
for participant in participants:
    existing_global = global_status_by_tag.get(participant["player_tag"])
    if existing_global:
        status, source = existing_global["status"], "template_confirm"
    elif participant["cwl_permanent_optout"]:
        status, source = "declined", "auto_optout"
    elif participant["cwl_permanent_optin"]:
        status, source = "auto_confirmed", "auto_optin"
    else:
        status, source = "pending", "template_confirm"
    signups_to_create.append({..., "source": source, "status": status})
```

**The automatic seed deliberately does NOT write `cwl_player_season_status`.** That table is the
global record of what a *member actually answered*; an automatic derivation is not an answer. Every
guild's own Start Enrollment independently derives the identical value from the same global
per-account preference, so cross-guild consistency is achieved without a propagation path — and
without any risk of one guild's automatic seed clobbering another guild's real response.
`propagate_cwl_player_response()` (`:1014`) is untouched and still only ever fires from a genuine
button click.

**No retroactive application.** Changing a preference after Start Enrollment has already run does
not rewrite existing `cwl_signups` rows — the spec says "when enrollment is started". Block II of
the Activity shows the member's *actual* current status, which is what keeps that from being
confusing.

#### 4b-bis. There is a SECOND seed site — it must not be missed

⚠️ **Start Enrollment is not the only place `cwl_signups` rows are created.**
`_send_cwl_enrollment_dm_batch()` seeds rows for the players it is about to DM
(`QBdiscocmdshelper_cwl.py:2822-2850`, added by tracker #0016 so no DM carries a dead button),
hardcoding `global_status or 'pending'`. That path is what the **"Notify New Pool Members"** button
(`web_bridge.notify_new_cwl_pool_members`, `:725-759`) uses for members who joined the pool after
enrollment started. Left alone, it produces three inconsistencies against 4b:

| newly pooled member | Start Enrollment gives | "Notify New Pool Members" would give |
| --- | --- | --- |
| opt-in | `auto_confirmed` + DM | `pending` + DM ❌ |
| opt-out + DM-anyway | `declined` + DM | `pending` + DM ❌ |
| opt-out, no DM | `declined`, no DM | **no row at all** ❌ — invisible on the board |

The third is the worst: the resolver filters those members out before the DM batch ever sees them,
so nothing seeds them and they simply don't appear.

**Fix, in three parts:**

1. **Extract the precedence into one function** so it can never be written twice:
   ```python
   def resolve_seeded_cwl_signup_status(
       existing_global: Optional[Dict[str, Any]], permanent_optout: bool, permanent_optin: bool,
   ) -> Tuple[str, str]:
       """(status, source) for a freshly-seeded cwl_signups row — the single definition of 4b's
       precedence (existing global response > opt-out > opt-in > pending). Every seed site calls
       this; none re-implements it."""
   ```
   `_start_cwl_enrollment_locked()`'s loop in 4b above becomes a call to it.
2. **`_send_cwl_enrollment_dm_batch()`'s seeding block calls it too.** It already has the flags in
   hand — its live-ownership re-check (`:2806-2818`) calls `get_player_links_sync`, which after
   Phase 1b returns all three preference columns. Use that same `live_links` dict rather than
   adding a query.
3. **`resolve_cwl_pool_dm_targets_sync()` returns the skipped members, not just a count** — add
   `"optout_no_dm": [entry, ...]` alongside the existing `skipped_optout` integer. Both Start
   Enrollment and `notify_new_cwl_pool_members` then seed `declined` rows for those entries.
   This also closes a case Start Enrollment's own loop can't reach on its own: its `participants`
   list is the clan-member scan, so an opted-out **guest player** or **shared-clan** member (pool
   sources 2 and 3, which only the resolver sees) would otherwise never get a row from either
   site. One list, used by both, covers all three pool sources uniformly.

- **Test**: the table above, as three explicit cases against `notify_new_cwl_pool_members` — plus
  an assertion that Start Enrollment and "Notify New Pool Members" produce the **identical**
  `(status, source)` for the same member with the same preferences. That equivalence is the real
  invariant; the individual cases are how it's demonstrated.

### 4c. DM targeting

`resolve_cwl_pool_dm_targets_sync()` (`:2557-2682`), the shared resolver — so Start Enrollment and
"Notify New Pool Members" can't disagree:

- Track `optin_by_tag` and `dm_anyway_by_tag` alongside the existing `optout_by_tag`, populated
  from the same sources (`members`, and `get_player_links_sync` for `unknown_tags`).
- Final loop (`:2675-2681`):
  ```python
  for entry in pool.values():
      tag = entry["player_tag"]
      if optout_by_tag.get(tag) and not dm_anyway_by_tag.get(tag):
          result["skipped_optout"] += 1
      elif entry["discord_id"]:
          result["targets"].append(entry)
      else:
          result["skipped_unlinked"] += 1
  ```
  Opt-in needs no branch — it already falls through to the normal "DM them" path, which is exactly
  the spec's "a CWL invitation DM should always be sent".
- `skipped_optout`'s meaning narrows from "opted out" to "opted out and didn't ask for the DM".
  Update its docstring and the i18n line that reports it in the Start Enrollment summary.

`resolve_cwl_pending_reminder_targets_sync()`'s own `cwl_permanent_optout` guard (`:2716-2718`)
stays as-is: its input is already restricted to `status='pending'` rows, which an opted-out account
no longer produces, so the guard is now belt-and-braces rather than load-bearing. Say so in its
docstring rather than deleting it.

- **Tests**: extend `tests/unit/test_cwl_enrollment_hardening.py` (or `test_cwl_helpers.py`,
  whichever already fixtures a seeded event) — opt-out seeds a `declined` row **and** no DM;
  opt-out + `send_dm_anyway` seeds `declined` **and** a DM target; opt-in seeds `auto_confirmed`
  **and** a DM target; no preference is unchanged from today; an `existing_global` response beats
  every preference; a DM-button click on an auto-seeded row overwrites it with a real
  `confirmed`/`declined` and propagates normally.

---

### 4d. Unify the two DM response patterns

**The problem.** Two `DynamicItem` button classes answer the same question ("did you confirm or
decline?") with different end-of-click behavior:

- `CwlSignupResponseButton` (`cwl:signup:*`, invitation DM) — `interaction.response.edit_message(
  content=<single-account text>, view=None)`, replacing the whole message.
- `CwlReminderResponseButton` (`cwl:remind:*`, tracker #0038 reminder DM) — re-derives the user's
  still-pending accounts live from the DB and re-renders the message from that fresh query.

The split was deliberate, not accidental: `plans/implemented/tracker-0038-remind-pending-cwl.md`'s
"Design note" records it. Invitation DMs are sent **one per account**
(`_send_cwl_enrollment_dm_batch`'s per-participant loop, `QBdiscocmdshelper_cwl.py:2853-2872`),
reminder DMs **one per user covering up to 5 accounts** — and wiping a whole message on one click
is correct for the first shape and destructive for the second. The button pattern diverged because
the *message* shape diverged.

**Which is more robust: the reminder's.** Three reasons, in order of weight:

1. **It is driveable without an `Interaction`.** `interaction.response.edit_message()` needs the
   click. "Re-derive from the DB and re-render this message" needs only `(channel, message)`, so a
   bridge handler can call it — which is exactly what Phase 5c's DM reconciliation needs, and what
   an admin override or the tracker #0019 reroute sweep could use later.
2. **It generalizes; the other doesn't.** N=1 is a degenerate case of the reminder pattern (scope
   empties → "all done", `view=None` — bit-for-bit today's invitation behavior). The reverse is
   impossible.
3. **It is the convention the repo already states** — Pitfall 24, "re-derive from live state rather
   than trust a stored snapshot", which the design note itself cites.

**Why this is cheap: the bookkeeping already exists.** `send_cwl_reminder_dm_group()`
(`:2968-2974`) calls `mark_cwl_player_dm_sent_sync` once **per account** with that chunk's shared
`message_id`/`channel_id`, and `_send_cwl_enrollment_dm_batch()` (`:2857-2863`) does the same for
its single account. So `cwl_player_season_status.dm_sent_via_message_id` **already** records, per
player_tag, which DM message currently covers it — for both shapes, with no new columns and no
snapshot in any `custom_id`.

**The change**, in `qapbot/ui_cwl_roster.py`:

```python
async def rerender_cwl_dm_after_response(
    message: Any, event_id: int, season: str, discord_id: str,
    *, action: str, interaction: Optional[discord.Interaction] = None,
) -> None:
    """Re-render one enrollment/reminder DM from live state after its owner answered — from a
    button click, from the Activity, or from anywhere else.

    Scope (which accounts this message covers) is re-derived, never assumed: the player_tags whose
    cwl_player_season_status.dm_sent_via_message_id == message.id for this season. Of those, the
    ones still 'pending' get re-rendered with their buttons; when none remain the message becomes
    a plain confirmation with view=None.

    Applies the edit through `interaction.response.edit_message()` when an interaction is
    available (keeping the click's own response slot, as today) and through `message.edit()`
    otherwise. That second path is the whole point: it is what lets a non-Discord caller
    reconcile a DM."""
```

- Both button callbacks become `_apply_cwl_signup_response(...)` → `rerender_cwl_dm_after_response(
  interaction.message, ..., interaction=interaction)`. `_apply_cwl_signup_response()` already
  guarantees the *write* half can't fork; this makes the *render* half match.
- **Both `custom_id` templates stay registered.** DMs already sitting in members' inboxes carry
  `cwl:signup:*` or `cwl:remind:*` and must keep working — this unifies behavior, not identifiers.
  The two classes shrink to thin shells over one shared callback body.
- **Preserve today's exact text.** When the re-derived scope covered exactly one account, finalize
  with the existing `cwl.template.confirmed_msg`/`declined_msg` (which names the player); when it
  covered several, the reminder's group "all done" key. Keyed off scope size, so no member sees
  their message wording change.

**Explicitly NOT unified: the message grouping.** Invitation DMs stay one-per-account. Combining
them per user would break tracker #0019's re-route sweep, which retracts and re-sends *one*
account's DM when that account changes Discord owner — impossible without disturbing siblings once
several accounts share a message. (The reminder DM only escapes this because no sweep targets it.)
The unification above makes per-user invitation grouping *feasible* later — the renderer already
handles a shrinking scope — but it is a real UX change nobody has asked for, so it stays out.

- **Tests**: extend `tests/unit/test_cwl_remind_pending.py` (which already fixtures the
  multi-account reminder path) — a single-account scope finalizes with the per-player text and
  `view=None`, byte-identical to the pre-unification invitation behavior; a 3-account scope with 2
  still pending re-renders 2 rows; the last of 3 finalizes with the group text; scope derivation
  ignores accounts pointing at a *different* message id; the no-interaction path edits via
  `message.edit()`.

---

## Phase 5 — the Activity screen (`player_prefs`)

### 5a. Screen routing

- `ui_cwl_roster.py`: new `_make_cwl_player_hub_open_callback()`, structurally a copy of
  `_make_cwl_management_open_enrollment_web_callback()` (`:854-866`) but with **no permission gate
  at all** — this screen is for every member — calling
  `_launch_cwl_activity(interaction, guild_id, "player_prefs")`.
- `_launch_cwl_activity()`'s fallback branch (`:830-836`) currently picks its i18n key with a
  two-way `screen == "clan_config"` ternary. Turn that into a dict lookup keyed by screen and add
  `cwl.player_hub.open_fallback`.
- `web_bridge.handle_get_cwl_screen` (`:1220`): no code change (it returns whatever was recorded);
  extend its docstring from "which of two screens" to three.
- `activity/client/src/types.ts`: `ScreenPayload['screen']` gains `'player_prefs'`.
- `main.ts`: a third branch, before the `clan_config` fall-through.

  ⚠️ The `"clan_config"` default when nothing was recorded (`web_bridge.py:1246`) is unchanged, so a
  member who somehow reaches the Activity with no pending screen still lands on the admin-gated
  clan-config fetch and gets a 403. That is pre-existing behavior and out of scope — worth a line
  in the manual test case, not a fix.

### 5a-bis. Second entry point — `/cwl preferences`

**Added on the project owner's decision, 2026-08-23.** The anchored hub message must not be the
only way in: a guild that disables it (or hasn't configured it yet) would otherwise leave members
with no self-service way to reach their own preferences — including no way to opt out — with the
enrollment DM as their only lever.

`QBdiscordcmds.py` has **no `/cwl` group today** (only `/clan` at `:2719` and `/analyse` at
`:2877`, plus flat top-level commands). So this adds one:

```python
cwl_group = app_commands.Group(name="cwl", description=dev_mode+"Personal CWL commands")

@cwl_group.command(name="preferences", description=dev_mode+"View and change your personal CWL settings.")
```

A new group rather than a flat `/mycwl`, because it leaves room for the member-facing CWL commands
the original `CWL_ROSTER_PLANNING_PLAN.md` sketched (e.g. `/cwl signup`) without a second rename
later. Register it alongside the existing groups wherever those are added to the command tree.

The callback is three lines — guild guard, then `_launch_cwl_activity(interaction, guild_id,
"player_prefs")`:

⚠️ **It must not `defer()` or `send_message()` first.** Per `_launch_cwl_activity()`'s own
documented hard constraint (`ui_cwl_roster.py:800-812`), LAUNCH_ACTIVITY (type 12) has to be the
interaction's very first response and has no deferred or followup form. A slash-command
interaction is subject to exactly the same rule as the component interactions that already use it.

Deliberately **not** gated on `cwl_player_hub_message_enabled` — being reachable when the hub is
off is the entire reason this exists. It is gated only on being in a guild (it needs a `guild_id`
to resolve the season and the language).

- **Test**: extend `tests/discord/test_commands.py` — the command registers, is guild-only, records
  `player_prefs` in `CACHE.pending_cwl_activity_screen`, and works with the hub message disabled.

### 5b. Anchored message + view

`qapbot/ui_cwl_roster.py`:

```python
async def build_cwl_player_hub_content_and_view(channel, guild_id_int):
    """The (content, view, embed) callback repost_anchored_message() expects."""
```

Embed: title + a short explanation — what the hub is for, that preferences apply to each of your
linked accounts individually, and that a standing choice can still be overridden month by month
from the invitation DM. All via `t()`.

```python
class CwlPlayerHubView(discord.ui.View):
    """Persistent per-guild anchored message, player-facing counterpart to CwlManagementHubView.
    Holds no per-guild instance state (one instance serves every guild — the same generic-dispatch
    constraint CwlManagementHubView documents). Exactly one button, which launches the
    'player_prefs' Activity screen."""
    def __init__(self) -> None:
        super().__init__(timeout=None)
        # single button, custom_id="cwl_player_hub_open_prefs"
```

Registered in `QapBot.py`'s `_setup_hook()` next to the other persistent views (`:2842-2850`):
`QBcore.bot.add_view(CwlPlayerHubView())`.

**No `interaction_check()` override.** The `fully_initialized` gate is applied globally to every
`discord.ui.View` by the monkey-patch in `QBcore.py:188-210` — `CwlManagementHubView` relies on
exactly that and has no override; this view mirrors it.

**No `refresh_*_on_mutation` companion.** The anchored message is per-guild and static; nothing a
member does in the Activity changes it. (Contrast `CwlManagementHubView`, whose embed shows live
season state.)

### 5c. Bridge endpoints — `qapbot/web_bridge.py`

**Account protection (Cardinal Rule 2) is the single most important thing in this phase.** These
are the first bridge endpoints that are *not* admin-gated, so the gate becomes an ownership check
instead:

> The set of accounts a request may read or write is derived **solely** from
> `get_all_players_for_discord_ids_sync([str(discord_user_id)])`, where `discord_user_id` is the
> value the Worker independently verified against Discord (`verifiedDiscordUserId()`), never a
> client-supplied field. Any `player_tag` in a POST body that is not in that set is a **403**, not a
> silent no-op.

```
GET /api/cwl/player-prefs?guild_id=&discord_user_id=
```

```jsonc
{
  "season": "2026-08",              // null when the guild has no CWL event at all
  "event_status": "signup_open",    // null likewise
  "accounts": [                     // block I — primary account first, then by name
    { "player_tag": "#ABC", "player_name": "Foo", "verified": true,
      "preferred_league_rank": "Master League II",    // null = no preference
      "mode": "optout",                               // "none" | "optin" | "optout"
      "send_dm_anyway": false }
  ],
  "season_rows": [                  // block II — one row per account, [] when season is null
    { "player_tag": "#ABC", "player_name": "Foo",
      "signup_status": "declined",                    // incl. the new "auto_confirmed"; null = no row
      "assigned_clan_tag": "#XYZ", "assigned_clan_name": "Some Clan",
      "assigned_clan_tier": "Master League I",
      "assigned_clan_start_at": "2026-09-01T05:00Z" }  // UTC, same format ClanConfig.cwl_start_at uses
  ]
}
```

Built by a `_build_player_prefs_payload_sync(guild_id, discord_user_id)` that resolves the event via
`get_current_cwl_event_sync(guild_id)` (`QBdiscocmdshelper_cwl.py:239`) — the correct "which event is
relevant to this member right now" resolver, **not** the admin's `cwl_selected_season` UI state —
then joins `get_cwl_signups_for_event_sync` (`:4885`), `get_cwl_assignments_sync` (`:5577`) and
`get_cwl_event_clans_sync` (`:4317`, which already carries `tier`/`cwl_start_at`). Wrapped in one
`asyncio.to_thread()` hop by the handler (Pitfall 26).

```
POST /api/cwl/player-prefs
{ "guild_id": "...", "changes": [ { "player_tag": "#ABC" | null, "mode": "...",
                                    "send_dm_anyway": bool, "league_rank": "..." | null } ] }
```

`player_tag: null` means "all of my accounts" (the bulk control). Each change maps to one
`set_cwl_preferences_sync()` call. The response is the **freshly rebuilt GET payload**, so the client
always re-renders from server truth rather than from its own optimistic state.

```
POST /api/cwl/player-prefs/status
{ "guild_id": "...", "player_tag": "#ABC", "action": "confirm" | "optout" }
```

The member changing their own invitation status from block II. **`action`, not `status`** —
deliberately the same vocabulary as the DM button's own `custom_id` (`cwl:signup:confirm|optout`),
because this endpoint's entire contract is "do exactly what that button does":

```python
from qapbot.ui_cwl_roster import _apply_cwl_signup_response

result = await _apply_cwl_signup_response(event_id, player_tag, action, str(discord_user_id))
```

`_apply_cwl_signup_response()` (`ui_cwl_roster.py:1447-1525`) was already extracted as shared
logic for exactly this reason (tracker #0038 split it out so the reminder DM's buttons couldn't
drift from the original's). Reusing it gets, for free and provably identically:

- the account-ownership check (Cardinal Rule 2) against **both** the live `user_players` owner and
  the DM's recorded recipient;
- the `event['status'] != 'signup_open'` guard — which **is** the spec's "only after enrollment
  started", enforced by existing code rather than a new condition;
- `upsert_cwl_signup_sync` with `source='template_confirm'|'template_optout'` and a real
  `responded_at`, so the row is indistinguishable from a DM answer;
- `propagate_cwl_player_response()` — the global row plus every other guild's mirror (rule h);
- `bump_enrollment_version()` for this guild and every affected one, so open Manage Teams boards
  update live.

Its return codes (`db_unavailable` / `no_longer_valid` / `not_your_signup` / `signup_closed` /
`ok`) map 1:1 onto existing `cwl.template.*` i18n keys, so the client can render the failure text
straight from the i18n catalog (Phase 6) with no new strings.

**Then reconcile the DM** (the second half of the spec's "as if the reply were given directly
through the dm"). A member who answers here while their invitation DM is still unanswered would
otherwise be left with a live-looking Confirm/Opt Out pair in their inbox contradicting what they
just did. On a successful `ok`, best-effort:

- Read `dm_sent_via_message_id` / `dm_sent_via_channel_id` / `dmed_discord_id` from
  `cwl_player_season_status` (the same fields `handle_post_cwl_enrollment_status`'s
  `_apply_status_sync` already reads, `:1550-1553`), reach the message via `bot.fetch_user()` →
  `create_dm()` → `fetch_message()` (the established pattern from
  `cleanup_stale_cwl_enrollment_dms()`, `QBdiscocmdshelper_cwl.py:3028-3063`), then hand it to
  **`rerender_cwl_dm_after_response()` from Phase 4d with no `interaction`**.

  This is why Phase 4d is a prerequisite rather than a nice-to-have: that function is the *only*
  thing here that knows how to correctly re-render a DM covering an unknown number of accounts,
  and it handles both DM shapes identically. Without it this handler would have to hand-roll a
  second, subtly different renderer — precisely the fork Phase 4d exists to close.
- **Best-effort only**, with `cleanup_stale_cwl_enrollment_dms()`'s exact swallow-everything
  posture (`NotFound` → already gone; `Forbidden` → DMs closed; anything else → log and move on).
  The status write has already committed; a DM that can't be edited must never fail the member's
  action.

Register all three in `create_app()` (`:2741+`).

### 5d. Worker — `activity/server/src/index.ts`

Three routes — `api.get('/cwl/player-prefs')`, `api.post('/cwl/player-prefs')` and
`api.post('/cwl/player-prefs/status')` — exact copies of the `/cwl/clan-config` GET/POST pair's
verify-identity-then-proxy shape (`:105-144`), plus the `/i18n` route from Phase 6. No new security
logic in any of them — the Python side owns ownership scoping.

### 5e. Client — `activity/client/src/playerPrefs.ts`

New module, `renderPlayerPrefs(root, payload, onSave, onClose)`, called from `main.ts`'s new branch.
**Reuse from `clanConfigTable.ts`** (942 lines, and its structure is the model here):

- `utcStringToLocalParts()` (`:57-65`) — for block II's start-time column. **Extract it (with
  `pad`, `:40`) into a small shared `activity/client/src/timeFormat.ts`** rather than copying; it is
  the only genuinely shared helper and a copy would drift.
- The `.table-scroll` / `table` / `th,td { white-space: nowrap; height: 34px }` /
  per-cell-`box-shadow` divider CSS in `activity/client/index.html`'s `<style>` block — already
  global, so both tables inherit it with no new CSS. Its comment block records several hard-won
  iPad/WebKit fixes; do not re-solve them.
- The header / footer-status-line / Save+Close button layout, and the "disable controls while a save
  is in flight, surface the error inline in the footer" pattern from `renderClanConfigTable`.
- `errorFromResponse()` (`main.ts:47-55`) — already shared at the `main.ts` level.

**Block I — "Your general CWL preferences"** — one row per linked account:

| Account | Preferred league | CWL participation | Send invitation DM anyway |
| --- | --- | --- | --- |
| Foo `#ABC` | `<select>` — "No preference" + the 19 `CWL_LEAGUE_RANKS` values | `<select>` — "Ask me each season" / "Always play" / "Never play" | `<input type=checkbox>`, **`disabled` unless "Never play"** |

Plus an **"Apply to all my accounts"** row pinned above the per-account rows, with the same three
controls and an Apply button — that is the spec's "changeable for all players at once", and it posts
a single `{player_tag: null}` change.

The league list is **duplicated as a TypeScript constant** in `playerPrefs.ts` rather than fetched —
`CWL_LEAGUE_RANKS` (`ui_cwl_roster.py:26-34`) is a static CoC-defined ladder that has not changed
since it was written. Add a comment in **both** places naming the other, so a future change to the
ladder is caught. The "No preference" sentinel is a leading `<option value="">` mapping to
`league_rank: null` — **not** a 20th entry appended to the shared Python list, which `/cwl signup`'s
own picker also uses and must not offer.

The tri-state select is what enforces mutual exclusivity in the UI; the DB write path (Phase 1c)
enforces it independently, so a hand-crafted POST cannot break the invariant either.

**Block II — "This season" (`{season}`)** — one row per account, with the status column
**interactive**:

| Account | Status | Clan | League tier | CWL start |
| --- | --- | --- | --- | --- |
| Foo `#ABC` | ❓ Auto-Confirmed · **[I'm in] [I'm out]** | Some Clan | Master League I | 1 Sep 2026, 07:00 (local) |

- Status icon + label reuse the Manage Teams board's — **import the SVG assets and the
  `STATUS_LABEL` map from a small shared module** extracted out of `enrollmentBoard.ts`
  (`signupStatus.ts`) so the two screens can never disagree about what Auto-Confirmed looks like.
- **The two action buttons** post `{action: 'confirm'|'optout'}` to
  `/api/cwl/player-prefs/status`. The button matching the current status is rendered `disabled`
  (nothing to change), so `confirmed` shows only "I'm out" and vice versa; `pending` and
  `auto_confirmed` show both.
- **Buttons appear only when the season is actually answerable** — the payload's `event_status`
  is `signup_open`. Any other status (`draft`, `finalized`, `announced`, `cancelled`) renders the
  status read-only with a short "enrollment isn't open" note. This is the spec's "only after
  enrollment started", enforced client-side for clarity **and** server-side by
  `_apply_cwl_signup_response()`'s own `signup_closed` guard — the client-side half is UX, the
  server-side half is the actual rule.
- **No optimistic update.** A successful POST returns the rebuilt payload and the block re-renders
  from it, so the status shown is always what the DB holds — the same discipline the preferences
  Save uses, and what keeps this screen honest when a DM answer and an Activity answer race.
- A failure renders `result.code`'s `cwl.template.*` string in the footer status line (e.g. the
  member's account was re-linked to someone else since the screen opened → `not_your_signup`).
- No assignment → "Unassigned", with the tier and start columns showing "—".
- No CWL event for the guild → the whole block is replaced by a one-line "No CWL season is set up
  yet." notice; block I stays fully usable (preferences are season-independent).
- **No linked accounts at all** → both blocks are replaced by a single "You have no linked Clash
  accounts yet — use `/link` first." line. `accounts: []` is a perfectly reachable state (any
  guild member can press the button), so it needs a defined screen rather than an empty table.
- Start time is rendered in the viewer's local timezone via the shared helper, matching the
  `.tz-note` convention the clan-config screen already sets.

**Why buttons rather than a status `<select>`:** the two available transitions *are* the DM's two
buttons, and mirroring that shape is what makes "it works like answering the DM" self-evident to
the member. A dropdown would also invite offering `pending`, which a member must not be able to
set (it triggers a DM re-send and is admin-only — `ADMIN_SETTABLE_ENROLLMENT_STATUSES`).

**Localization**: every string on this screen comes from the catalog the bridge serves — see
Phase 6. No hardcoded English in `playerPrefs.ts`, including the shared `signupStatus.ts` labels
(which therefore become keys, with `enrollmentBoard.ts` keeping its English defaults — see Phase 6
for why the admin board is deliberately not converted in this plan).

### 5f. Deploy

`cd activity/client && npm run deploy:dev` **and** `cd activity/server && npm run deploy:dev`, per
`activity/README.md`'s two-halves rule and its 2026-08-23 stale-Worker incident. PROD needs its own
explicit go-ahead.

- **Tests**: extend `tests/discord/test_web_bridge.py` — the payload builder for 0/1/3 linked
  accounts, no-event and event-present, assigned and unassigned; and above all the **ownership
  rejection**: a POST naming a `player_tag` belonging to someone else returns 403 and writes
  nothing. `npm run typecheck` in both Activity projects is part of this phase's exit criteria.

---

## Phase 6 — i18n, including the Activity

### 6a. Architecture decision: **one catalog, served by the bridge at runtime**

The Activity gets its strings from `qapbot/translations/{en,de}.json` — the same files, the same
keys, the same parity test — fetched at launch through a new bridge endpoint. It does **not** get
its own catalog, and there is **no build step** generating TypeScript from the JSON.

**What decides it: the bot already has a per-user language preference, and only the bot can see
it.** `t()` (`qapbot/i18n.py:226-286`) resolves *user language → guild language → default*, where
the user's own preference lives in `CACHE.user_accounts[user_id]["user_language"]`
(`get_user_language`, `:321-355`). For a screen whose whole point is that it is personal, using
the member's own language is the correct behavior, and it is already implemented — but it is
knowable **only** on the bot side. A client-side catalog would have to guess from the Discord
client's locale instead, which would mean a member who set a bot language preference sees it
honored everywhere in QapBot except the one screen that is entirely about them.

The other two options, and why not:

- **A build step generating a TS module from the JSON.** Rejected on the same evidence
  `activity/README.md` already records twice: anything baked into the client bundle only reaches
  users on a redeploy, and this repo has a documented incident (2026-08-19) where an
  implemented-and-verified change read as a live regression purely because the bundle wasn't
  rebuilt. Translations would inherit that failure mode permanently — a German string fix would
  need a Pages deploy. It also still can't see the per-user preference.
- **A hand-maintained client-side catalog.** Rejected: two catalogs means two parity tests, two
  places for translators to work, and guaranteed drift. `tests/unit/test_i18n.py` /
  `test_i18n_coverage.py` and `qapbot/scripts/check_translation_files.py` already enforce en/de
  parity over the JSON; keeping one catalog means Activity strings are covered by that on day one,
  for free.

**Cost of the chosen approach**: one extra HTTP round-trip at launch (issued via `Promise.all`
alongside the screen's own payload fetch, so it adds no serial latency), and Activity strings must
be enumerated in a namespace rather than written inline. Both are cheap; neither is a
correctness risk.

### 6b. `qapbot/i18n.py` — new namespace accessor

```python
def get_namespace(namespace: str, language: Optional[str] = None) -> Dict[str, str]:
    """Flattened {dotted_key: raw_template} for one namespace subtree, for bulk delivery to a
    non-Python client. Placeholders are left UNINTERPOLATED ({name} intact) — the caller
    substitutes. Falls back to `en` PER KEY, not per namespace, matching get_translation()'s own
    chain: a de.json missing one key must yield English for that key only, never drop the rest."""
```

Per-key fallback is the one subtle requirement here — a whole-subtree `de or en` would silently
blank every sibling key the moment one is missing.

### 6c. Bridge — `GET /api/i18n?guild_id=&discord_user_id=&ns=cwl.activity`

Resolves the language with the **exact same chain `t()` uses** — call
`get_user_language(discord_user_id) or get_guild_language(guild_id) or default` rather than
re-deriving it, so the Activity can never disagree with a DM the same member receives — and
returns `{"lang": "de", "strings": {...}}`. No permission gate: a translation catalog reveals
nothing. Register in `create_app()`; proxy through the Worker with the same
verify-identity-then-proxy shape as everything else.

### 6d. Client — `activity/client/src/i18n.ts`

~30 lines: `createTranslator(strings) -> (key, vars?) => string`, doing a flat lookup plus
`{placeholder}` replacement, and returning the key itself when missing (the same last-resort
behavior `TranslationManager` has, so a missing key is visible rather than blank). `main.ts`
fetches the catalog in parallel with the screen payload and passes the translator into
`renderPlayerPrefs()`.

### 6e. Scope boundary — the admin screens stay English

`clanConfigTable.ts` and `enrollmentBoard.ts` are **not** converted here. They are admin/leader
tools, they have hundreds of strings (including long explanatory tooltips), and converting them is
a large mechanical change with real regression surface that has nothing to do with this feature.
The infrastructure built in 6b–6d is what makes that a later, cheap, incremental job — one screen
at a time, no new decisions needed. `signupStatus.ts`'s four status labels are shared by both
boards, so they get keys **and** keep English string defaults, letting `enrollmentBoard.ts` go on
passing no translator.

State this boundary in the plan's own tracker item so the mixed-language state during the interim
reads as deliberate rather than half-finished.

### 6f. Keys

New `cwl.activity.*` namespace (the one the Activity fetches — deliberately its own namespace, not
scattered through `cwl.player_hub.*`, so `ns=cwl.activity` is one clean subtree):

```
cwl.activity.prefs_title / .season_title / .no_season
cwl.activity.col_account / .col_league / .col_participation / .col_dm_anyway
cwl.activity.col_status / .col_clan / .col_tier / .col_start
cwl.activity.mode_none / .mode_optin / .mode_optout
cwl.activity.no_preference / .unassigned / .apply_to_all / .apply / .save / .close
cwl.activity.status_pending / .status_confirmed / .status_declined / .status_auto_confirmed
cwl.activity.button_im_in / .button_im_out / .enrollment_not_open
cwl.activity.saved / .saving / .tz_note
```

Existing `cwl.template.*` keys are reused as-is for the status-change failure codes (see Phase 5c).

New `cwl.player_hub.*` namespace for the Discord-side anchored message, mirroring the sibling
`cwl.management.*` block's shape (`en.json:887+`):

```
cwl.player_hub.title / .description
cwl.player_hub.button_preferences          ("Your CWL Preferences")
cwl.player_hub.open_fallback
```

Additions to the existing `cwl.settings.*` block:

```
cwl.settings.button_activate_player_hub / .button_deactivate_player_hub
cwl.settings.no_player_hub_channel_set
cwl.settings.player_hub_block_title / .player_hub_status / .player_hub_channel
```

Addition to `cwl.management.*`:

```
cwl.management.signup_status_auto_confirmed
```

Plus a reworded Start Enrollment summary line for `skipped_optout` (its meaning narrows in
Phase 4c).

en/de key-set parity is enforced by `tests/unit/test_i18n.py` / `test_i18n_coverage.py` and
`qapbot/scripts/check_translation_files.py` — extend and run those rather than hand-diffing. Because
the Activity's strings live in these same files, that existing parity check covers them with no new
test infrastructure, which is the main practical payoff of the 6a decision.

- **Tests**: `get_namespace()` returns a flat dict with placeholders intact; per-key (not
  per-namespace) fallback when `de.json` is missing one key; an unknown namespace returns `{}`
  rather than raising. Bridge: the `/api/i18n` language resolution honors a user preference over
  the guild's, and the guild's over the default — assert against the same fixtures
  `tests/unit/test_i18n.py` already uses so the two can't drift.

---

## Notes for whoever implements this

Written for an implementer who does **not** have the planning conversation's context. Everything
below is decided — none of it is left to judgment.

- **Read `.github/copilot-instructions.md` in full first** (CLAUDE.md's own instruction). The
  cardinal rules this plan leans on by number: **2** (account protection — Phase 5c's ownership
  check), **12** (idempotent startup repairs — Phase 2d's marker), **15** (plan storage + a manual
  test case per tracker item), **24** (never branch on a static write-time marker — why
  `cwl_signups.source` is audit-only). Pitfalls **26** (wrap sync DB work in one
  `asyncio.to_thread` hop), **37** (`dmed_discord_id` is not ownership), **38** (never DM without a
  `cwl_signups` row).
### ⚠️ Scope of the current implementation run: **Phases 0–4 only. Stop after Phase 4.**

Decided by the project owner, 2026-08-23, for an unattended run while they are away.

**Implement Phases 0, 1, 2, 3 and 4. Do not start Phase 5 or Phase 6.** Those two span the
Cloudflare Worker and the browser client, whose only automated check is `typecheck` — most of that
work cannot be meaningfully verified without a human looking at a running Activity, and a wrong
call early in Phase 5 would propagate through the rest with nobody to catch it. Phases 0–4 are pure
Python, fully covered by `.\run_tests.ps1`, and contain the riskiest change in the whole plan
(Phase 4 alters what Start Enrollment produces for existing guilds) — which is exactly the part
worth reviewing before anything is built on top of it.

Phase 2 has one dependency worth noting under this split: it adds the **channel slot and the
enable toggle** for a hub message whose Activity screen does not exist yet. That is fine and
intentional — the toggle guards on "no channel set" and the anchored message simply won't be
enabled by anyone until Phase 5 ships. Phase 3's repost wiring likewise stays dormant while
`cwl_player_hub_message_enabled` is false, which it is by default for every guild.

**Consequently: no Activity deploy in this run.** Neither half. Nothing in Phases 0–4 changes
`activity/server`, and the only `activity/client` change is Phase 4a's new `autoconfirmed.svg`
plus its `enrollmentBoard.ts`/`types.ts` wiring — commit it, run `npm run typecheck` to prove it
builds, and leave deploying to the project owner. (Until it is deployed, the Manage Teams board
renders an `auto_confirmed` player with no status icon, exactly as it treats any unknown status
today — a cosmetic gap on an admin screen, not a break.)

- **One commit per phase**, in the order below, each with `.\run_tests.ps1` green before the next
  starts. Phase 4d is a pure refactor and gets its own commit *inside* Phase 4.
- **Work on a feature branch — `cwl-player-hub` — and leave it checked out when you finish.** The
  DEV bot runs from this working tree, so the branch must stay checked out for the project owner to
  test on return. Do not merge to `main`, and do not switch back to `main` at the end.
- **The Activity projects have no test runner** — `activity/client` and `activity/server` expose
  only `dev` / `build` / `typecheck` / `deploy:*`. `npm run typecheck` in **both** is the complete
  automated check for TypeScript; do not add a test framework. All behavioral tests for this
  feature are Python-side, against the bridge.
- **No deploys in this run** (see the scope box above — Phases 0–4 need none). For later runs:
  DEV deploys are pre-authorized and PROD deploys are **not**, and a DEV deploy always means
  **both halves** (`activity/client` *and* `activity/server`) — see `activity/README.md`'s
  2026-08-23 incident, where a client-only deploy left a new Worker route missing and the feature
  failed live with a bare "not found". Never run `deploy:prod` for either half.
- **Every phase's "verify live" step is for the project owner, not for you.** The owner is away
  during this implementation. Implement, get the automated tests green, deploy DEV, and record the
  manual test cases on the tracker item for them to run on return — do **not** mark any phase
  verified or any tracker test case passed on the strength of automated tests alone.
- **File one tracker item** covering this feature and reference **#21** in it (that request is the
  origin of the opt-in half and is superseded by this design — comment on #21, don't silently close
  it). Post the manual test cases from "Verification" below onto the item before finishing.
- **German strings**: write `de.json` entries for every new key *that Phases 0–4 introduce* (the
  Phase 6f `cwl.activity.*` block belongs to the later run; the keys reached here are
  `cwl.management.signup_status_auto_confirmed`, the Phase 2 `cwl.settings.*` player-hub lines, and
  the reworded `skipped_optout` summary line). The project owner is a German
  speaker and will review the wording — flag the new German strings in the tracker item as
  pending-review rather than presenting them as final. The en/de parity check must pass regardless
  (`tests/unit/test_i18n.py`, `qapbot/scripts/check_translation_files.py`).
- **The bridge secret check is not optional.** "No permission gate" on `/api/i18n` and the
  player-prefs routes means no *admin* check — every bridge handler still begins with
  `_check_secret(request)`. Copy an existing handler's opening lines rather than writing new ones.
- **Do not convert `clanConfigTable.ts` / `enrollmentBoard.ts` to i18n** (Phase 6e). Their English
  strings stay. Only the new `playerPrefs.ts` is translated. (Not reached in this run anyway.)
- **If a phase can't be made green**: fix it if you can; if you genuinely can't, **stop there**
  rather than starting a phase that depends on it, and record what blocked you on the tracker item.
  Completed earlier phases stay committed — they are sequenced so each is independently valid.
- **If something in this plan turns out to be wrong**, stop and record it on the tracker item
  rather than improvising a different design — several decisions here look arbitrary but are
  load-bearing, and their reasoning is written down next to each one. The most likely candidates
  are the exact line numbers (the repo moves) — those are navigation aids, so re-locate by symbol
  name and carry on; a *design* claim that doesn't hold is the kind worth stopping over.

## Suggested order / incremental commits

1. **Phase 0** — landmine fix, ships alone with its own regression test.
2. **Phase 1** — migration (incl. rename (a)) + read widening + `set_cwl_preferences_sync` + unit
   tests. No UI, no behavior change.
3. **Phase 4** — enrollment semantics (`auto_confirmed`, seeding incl. **4b-bis's second seed
   site**, DM targeting), the DM-pattern unification (4d), and the board's icon/label/legend. Fully
   testable by setting the columns directly, so the riskiest phase ships before any way for a
   member to *set* them exists. **4d ships as its own commit within this phase** — it is a pure
   refactor with no behavior change and its own regression tests, so it is worth landing and
   verifying separately before anything depends on it. The new icon needs an `activity/client`
   deploy to become visible, which is deliberately left to the project owner.
4. **Phase 2** — admin channel slot + toggle + settings block + rename (b) and its one-time repair
   (configurable; nothing posts yet).
5. **Phase 3** — repost wiring. Dormant until a guild enables the hub, which needs Phase 5.
--- *(end of the current run — everything below is a later session)* ---

6. **Phase 6a–6d** — the i18n plumbing (`get_namespace`, `/api/i18n`, `i18n.ts`), ahead of the
   screen that consumes it so Phase 5 can be written against a working translator from the first
   line rather than hardcoding English and converting later.
7. **Phase 5 + Phase 6f** — the Activity screen goes live; every string must exist before the hub
   message can post. Requires **both** `activity/` deploys.

## Verification

**In the current run (Phases 0–4), only the first three bullets are actionable** — everything
naming Phase 5 or 6 belongs to the later session, and every "live" check is for the project owner
on their return, not for the implementer (see "Notes for whoever implements this").

- `.\run_tests.ps1` must pass after each phase before starting the next.
- Per Cardinal Rule 15 / CLAUDE.md: file this as a tracker item (linking **#21**, whose request this
  supersedes) and post a manual test case before marking Phase 4 or Phase 5 implemented.
- After Phase 4 (before any UI exists), with preferences set by hand in the DEV DB: opt-out account
  → seeded `declined`, no DM; opt-out + DM-anyway → `declined` + DM, and clicking the DM's Confirm
  button flips it to `confirmed`; opt-in account → `auto_confirmed` on the Manage Teams board with
  the light-green ❓ icon **and** a DM; the season overview's counts add up.
- After Phase 5, end to end on a DEV guild: configure and enable the Player CWL Settings Hub
  channel; confirm the message posts with its single button; launch it as a **non-admin** member and
  confirm the Activity opens (this is the first non-admin Activity screen — the admin-only path is
  not a valid test of it); set a league preference, switch modes, toggle DM-anyway (and confirm it
  greys out when the mode isn't "Never play"); use "Apply to all my accounts" on a multi-account
  user; confirm block II shows the right clan, tier and local start time; reopen and confirm
  everything round-tripped; then run `set_primary_account` on that user and confirm the preferences
  survive (Phase 0's regression, exercised live).
- **After Phase 4d (pure refactor — verify no behavior changed):** answer a single-account
  invitation DM and confirm the message reads exactly as it did before the refactor; answer one
  account in a multi-account reminder DM and confirm its siblings' buttons survive; answer the last
  one and confirm the group finalizes.
- **Block II's status change, live** (the flow with the most moving parts): with enrollment open
  and an unanswered invitation DM sitting in a test member's inbox, press "I'm out" in the Activity
  → the row flips to Declined, **the DM in their inbox turns into the same "declined" text with its
  buttons gone**, an open Manage Teams board updates without a reload, and a second guild that
  pooled the same account shows Declined too. Then press "I'm in" and confirm the reverse. Repeat
  once with the DM already deleted by hand, to confirm the reconciliation failing is silent and the
  status change still succeeds.
- **After rename (b)**: on a guild whose admin Hub message predates the change, confirm the message
  is replaced exactly once on the first startup after deploy and that its Settings / Season
  Management / Refresh buttons all work; restart again and confirm it is *not* reposted a second
  time.
- **Second entry point**: with the Player CWL Settings Hub message *disabled*, run
  `/cwl preferences` and confirm the Activity still opens on the right screen.
- **Language**: set a test member's own language to German while the guild's is English, open the
  Activity, and confirm the screen is German (this is the case that a client-side catalog could not
  have got right — it is the whole justification for 6a, so it belongs in the test case).
