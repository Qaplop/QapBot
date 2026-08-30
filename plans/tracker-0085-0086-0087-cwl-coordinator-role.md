# Tracker #0085 / #0086 / #0087 — CWL Coordinator: explanation, notification, guild role, war mentions

Scope agreed with the project owner (2026-08-30), narrowing the four filed tickets:

- **#0085** — implement as filed (explanation text + "Notify CWL coordinators" button).
- **#0086** — implement with the **link-an-existing-role** pattern, *not* bot-created roles.
- **#0087** — do **not** build a separate coordinator DM reminder. Instead, mention the CWL
  coordinators in the **already-existing channel war notification**, plus a generic donation
  nudge line. No clan-castle/donation data check (the CoC API does not expose war clan-castle
  fill state, and nothing in this codebase tracks donations at all).
- **#0088** — duplicate of #0086 + #0087 as scoped above; no separate work. Its one unique
  ask (link the coordinator role the server already has) is exactly what #0086 does here.

## What a CWL Coordinator actually is

From the project owner's own answer in #0085's attachment, which is the authoritative definition
and the text the help must convey:

> The coordinator is the person who **starts CWL in-game** for that clan, is responsible for the
> CWL in that clan, and takes care of everything around it. They therefore have to be a
> **Leader or Co-Leader in the game** (not merely on Discord).

This is nowhere in the UI today — the reported complaint (Lucas, in the same screenshot) is that
coordinators are never told they are coordinators and can't tell what it means.

---

## A. #0085a — Explanation text

Two places, no logic:

1. `cwl.management.coordinators_header` (already exists) — prepend the role definition above to
   the Manage CWL Coordinators screen's header text.
2. `cwl.management.help_details_description` (from #0080) — expand the Setup section's
   *Manage CWL Coordinators* bullet with the same definition, condensed.

Both languages.

## B. #0085b — "Notify CWL coordinators" button

New button on `CwlCoordinatorConfigurationView` (row 2, beside Clear/Save).

**Delta tracking.** `_on_save` persists one clan at a time and then advances
`saved_coordinators_by_clan`, which destroys the "before" state. So `_on_save` computes the
per-clan delta *before* advancing that snapshot and accumulates it into a new
`self._pending_notifications: Dict[clan_tag, {"added": [...], "removed": [...]}]`.

Accumulation rules (a clan can be saved repeatedly before Notify is pressed):
- re-adding someone who is in `removed` cancels out of `removed` rather than appending to `added`
- and vice versa — so a save/undo round-trip leaves nothing pending, and nobody gets a
  pointless "you were removed" followed by "you were added".

**Button state.** Disabled while `_pending_notifications` is empty — same convention the Clear
button already follows.

**DMs.** Added coordinators get "you are now CWL coordinator for *clan*" **including the role
explanation from section A** (this is the message that actually fixes Lucas's complaint).
Removed coordinators get a short "you are no longer CWL coordinator for *clan*". Reuses
`send_cwl_coordinator_start_reminders()`'s existing DM plumbing: `_dm_guard_blocks()` and the
same per-user `t(..., user_id=...)` language resolution.

Clears `_pending_notifications` on success and reports contacted/blocked counts ephemerally,
matching the other CWL DM-batch summaries.

## C. #0086 — CWL coordinator guild role (link an existing role)

**Storage.** One new `guild_config` column, `cwl_coordinator_role_id TEXT` — added to the
`CREATE TABLE`, to the `_add_column_if_missing` migration list, to `load_guild_config`'s dict, and
to `save_guild_config`'s INSERT/UPDATE/params (all four places, per that function's existing
shape).

**Config UI.** New "Configure Coordinator Role" button on the **CWL Settings** screen
(`add_cwl_settings_components`, row 3) opening a small view with a `discord.ui.RoleSelect` +
Clear + Save — modelled directly on `RoleConfigurationView`'s newbie/member role selects.
Deliberately *not* in the general Role Configuration screen: this is CWL config, and it is a
*linked existing* role, unlike the bot-owned CoC/clan roles managed there.

**Membership sync.** New `sync_cwl_coordinator_role(guild)` helper:
- target holders = **union of every `cwl_clan_coordinators` list for the guild**, across *all*
  configured clans, not just this season's participating ones (coordinator config is standing and
  season-independent, so the role should be too).
- The union matters: dropping someone as coordinator of clan A must **not** strip the role while
  they are still coordinator of clan B.
- adds to members in the union who lack it, removes from members holding it who aren't,
  via the existing `assign_role_to_member` / `remove_role_from_member` helpers.
- no-ops when no role is configured.

**Call sites.** `CwlCoordinatorConfigurationView._on_save` (the only place coordinators change)
and the role-config Save (so linking a role backfills it immediately).

**Known limitation, deliberate:** the coordinator role is *not* wired into
`sync_roles_for_user`'s per-member hot path, so a coordinator who leaves and rejoins the guild
does not automatically get it back. Integrating there would mean every member role sync could
strip the role from everyone whenever the CWL config happens to be unloaded — a mass-role-removal
risk far worse than the rejoin gap. Re-saving the coordinator config (or the role config) repairs
it.

## D. #0087 — coordinators mentioned in the channel war notification

`_send_channel_war_notification()` (war_notifications.py) already builds a **custodian** mention
line with `allowed_mentions=discord.AllowedMentions(users=True, ...)`. Add a parallel coordinator
line right beside it, reading `guild_config["cwl_clan_coordinators"][clan_tag]` — identical shape
to `clan_custodians`.

**CWL-only.** Gated on `war_data.get("is_cwl")` (set in `_get_active_wars`, already used by
`_should_notify_for_war_type`). Coordinators are a CWL concept and "before the next war day" is
meaningless for a regular war.

**Donation nudge.** A fixed line appended to that same coordinator mention line — a pure memory
aid reminding them to check donations before the next war day. No data behind it; see the scope
note at the top for why a real check is impossible.

Deduping is unchanged: `_is_channel_notification_sent()` already limits this to one message per
war per guild, so coordinators get pinged once, not per cycle.

---

## Files touched

| File | Why |
|---|---|
| `qapbot/db_manager.py` | `cwl_coordinator_role_id` column: create/migrate/load/save |
| `qapbot/ui_cwl_roster.py` | Notify button + delta tracking; coordinator-role config button and view |
| `qapbot/guild_role_manager.py` | `sync_cwl_coordinator_role()` |
| `qapbot/war_notifications.py` | Coordinator mention line + donation nudge (CWL only) |
| `qapbot/translations/en.json`, `de.json` | All new strings, key parity |
| `tests/discord/test_ui_cwl_roster.py` | Delta accumulation, button state, notify flow |
| `tests/discord/test_war_notifications*.py` | Coordinator mention line, CWL gating |
| `changelog.txt` | Per the changelog convention |

## Verification

- `.\run_tests.ps1 -k "cwl"` and `.\run_tests.ps1 -k "war_notification"`
- en/de key-parity check
- Manual test cases posted per tracker item before marking implemented
