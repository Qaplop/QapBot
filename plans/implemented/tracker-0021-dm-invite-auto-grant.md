# DM invite + auto-grant for reporters who aren't guild members

## Context

The "Reply to requestor" button (ticket #21 follow-up, already shipped) fails with "member not
found" whenever the reporter filed their ticket via DM and never joined the server — which the
user reports is the common case. The user asked for the mechanism to be enhanced so that in this
situation the reporter gets a DM'd server invite, and confirmed (via clarifying question) that
they want the **fully automatic** path: once the reporter actually joins, the bot should apply
the same channel-access overwrite itself — no second button click required — rather than just
sending a bare invite and leaving the rest manual.

## Approach

### 1. New DB column: `tracker_items.access_grant_pending`

`qapbot/db_manager.py`'s `_create_tracker_schema()` ([db_manager.py:2794-2801](qapbot/db_manager.py#L2794-L2801)):
add `await self._add_column_if_missing("tracker_items", "access_grant_pending", "INTEGER NOT NULL DEFAULT 0")`
right after the existing `priority` migration call, same idempotent pattern (Cardinal Rule 12).
Add `"access_grant_pending"` to `_TRACKER_ITEM_UPDATABLE_COLUMNS`
([db_manager.py:2908-2914](qapbot/db_manager.py#L2908-L2914)) so `update_tracker_item()` can set/clear it.
Marks "an admin clicked Reply-to-requestor for this item while the reporter wasn't a member yet
— finish the grant once they join." No new table needed; `list_tracker_items(reporter_id=...,
guild_id=...)` (existing method, [db_manager.py:2873](qapbot/db_manager.py#L2873)) already gives
`apply_pending_requestor_access()` everything it needs to find pending rows for a joining member.

### 2. `_handle_grant_access()`: invite instead of failing outright

In `qapbot/ui_tracker.py`, when `member` resolution fails
([ui_tracker.py:1466-1476](qapbot/ui_tracker.py#L1466-L1476)), replace the immediate
`grant_access_member_not_found` bail-out with a new `_invite_requestor(interaction, item,
reporter_id)` call (keep `grant_access_member_not_found` only for the case where even a
`discord.User` lookup fails — a genuinely deleted/invalid account, nothing to invite):

- Resolve `QBcore.bot.get_user(int(reporter_id))` / `await fetch_user(...)` (same
  get-then-fetch-fallback idiom `_dm_reporter_on_status_change()` already uses,
  [ui_tracker.py:1103](qapbot/ui_tracker.py#L1103)). Not found → `grant_access_member_not_found`.
- `await interaction.channel.create_invite(max_age=604800, max_uses=1, unique=True, reason=...)`
  (7-day, single-use, channel-targeted so joining lands them near the ticket). Wrapped in
  `try/except (discord.Forbidden, discord.HTTPException)` → ephemeral `grant_access_invite_failed`
  on failure (most likely cause: bot lacks `Create Invite` on that channel — worth a one-line
  callout in the doc update below since that's an operational prerequisite, not something code
  can fix).
- On success: `await CACHE.db_manager.update_tracker_item(self.item_number,
  access_grant_pending=1)` — set the flag as soon as the invite exists, independent of whether
  the DM below actually lands (an admin who sees "DM failed" can still forward the invite link
  manually, and the pending flag is what makes auto-grant-on-join work either way).
- DM the reporter (`reporter_user.send(...)`, `grant_access_invite_dm` — item number + invite
  URL) wrapped in `try/except discord.Forbidden` (closed DMs). On DM failure, respond ephemeral
  with `grant_access_invite_dm_failed` which **includes the invite URL** so the admin can paste
  it themselves; on DM success, ephemeral `grant_access_invited` (also includes the URL, for
  visibility/audit).

Factor the existing inline jump-link construction in `_handle_grant_access`'s success path
([ui_tracker.py's added `if item.get("thread_id")` block]) into a small `_item_jump_link(item)`
helper, reused by the new post-join DM in step 3 below — same thread-else-message-link logic,
now needed in two places instead of one.

### 3. `apply_pending_requestor_access(member)` — finish the grant on join

New function in `qapbot/ui_tracker.py`, mirroring `handle_tracker_test_reaction()`'s structure
and its `CONFIG.tracker_enabled` guard ([ui_tracker.py:2107-2128](qapbot/ui_tracker.py#L2107-L2128)):

- **First line: `if not CONFIG.tracker_enabled: return`.** `on_member_join` is a raw gateway
  event (not a component interaction) — it fires on *every* bot present in the guild, including
  DEV, whose DB is a routine PROD-backup copy that can contain the exact same pending row. This
  is the identical failure mode BUG_FEATURE_TRACKER.md's architecture section and Pitfall 39
  already document for `on_raw_reaction_add`; the same reasoning applies here and the doc update
  below says so explicitly.
- `items = await CACHE.db_manager.list_tracker_items(reporter_id=str(member.id),
  guild_id=str(member.guild.id))`; keep those with `access_grant_pending` truthy, `status not in
  ("done", "rejected", "duplicate")`, and a `channel_id`. Nothing pending → return.
- For each: resolve the channel (`member.guild.get_channel`/`fetch_channel`, tolerating failure
  by logging and continuing to the next item), apply the same
  `discord.PermissionOverwrite(view_channel=True, read_message_history=True,
  send_messages_in_threads=True)` used in `_handle_grant_access()`, clear
  `access_grant_pending` back to `0`, then DM the member a confirmation with `_item_jump_link(item)`
  (new `grant_access_pending_applied` key) — wrapped in `try/except discord.Forbidden`, logged
  and skipped on failure (closed DMs shouldn't block the access grant itself, which already
  succeeded).

### 4. Wire into `QapBot.py`'s `on_member_join`

Mirror the `on_raw_reaction_add` → `handle_tracker_test_reaction()` delegation
([QapBot.py:3572-3588](QapBot.py#L3572-L3588)): a small, self-contained `try/except` block near
the top of `on_member_join` ([QapBot.py:3352](QapBot.py#L3352)) that imports and awaits
`apply_pending_requestor_access(member)`, logging (not raising) on failure — consistent with
every other block in that function already being independently try/except-wrapped so one
subsystem's failure doesn't block newbie-role assignment, role sync, or the welcome message.

### 5. Translations (`en.json` / `de.json`)

New keys alongside the existing `grant_access_*` ones: `grant_access_invite_failed`,
`grant_access_invite_dm` (DM content: ticket number + invite link), `grant_access_invite_dm_failed`
(includes `{invite_url}`), `grant_access_invited` (includes `{invite_url}`),
`grant_access_pending_applied` (post-join DM: ticket number + `{jump_link}`). Same 1:1 structure
both files already follow.

### 6. Tests (`tests/discord/test_ui_tracker_items.py`)

- `_handle_grant_access` when the reporter isn't a guild member but resolves as a `discord.User`:
  asserts `interaction.channel.create_invite` called, `access_grant_pending` ends up `1` in the
  DB, the user's `.send()` was awaited, and the ephemeral response contains the invite URL.
- Same, but `reporter_user.send()` raises `discord.Forbidden` → ephemeral response still contains
  the invite URL (the fallback-for-manual-forwarding path); `access_grant_pending` still `1`.
- `create_invite` raises `discord.Forbidden` → `grant_access_invite_failed`, `access_grant_pending`
  stays `0`, no DM attempted.
- Neither `get_user` nor `fetch_user` resolves anything → falls back to `grant_access_member_not_found`
  (existing test already covers the "no Discord user at all" case via the agent-filed item; add
  one for a real-but-unresolvable numeric reporter_id).
- `apply_pending_requestor_access(member)`: a pending, still-open item → `set_permissions` called
  with the expected overwrite, DB flag cleared, member DM'd with the jump link.
- Same, but the item is already terminal (`done`) → skipped entirely, no Discord calls.
- `CONFIG.tracker_enabled=False` → returns immediately, no DB query at all (patch `CONFIG` like
  the file's other tracker_enabled-guard tests do).

`QapBot.py`'s `on_member_join` itself stays untested directly, consistent with today (no test
file touches it) — the delegation is a thin, independently try/except-wrapped one-liner, and all
the actual logic lives in and is tested via `apply_pending_requestor_access()`.

### 7. Docs (`qapbot/docs/BUG_FEATURE_TRACKER.md`)

Extend the grant/revoke paragraph added for the original feature with: the not-a-member path now
DMs a 7-day single-use invite instead of failing outright, `access_grant_pending` tracks it, and
`apply_pending_requestor_access()` (called from `on_member_join`, gated on `CONFIG.tracker_enabled`
for the same DEV/PROD-shared-DB reason as the 👍-reaction shortcut) finishes the grant once they
actually join — plus the operational note that this needs the bot to hold `Create Invite` on the
tracker channel(s).

### 8. Changelog

New top entry in `changelog.txt` (today's date, next sequence number) describing the DM-invite +
auto-grant-on-join addition, referencing ticket #21 and the files touched (including this plan).

## Verification

- `.\run_tests.ps1` (full suite) after the change.
- Manual, live on PROD: click "Reply to requestor" on a ticket filed by a non-member — confirm
  they receive a DM with a working invite, confirm `access_grant_pending` is set; have that
  account join the server and confirm the bot grants access automatically and DMs a jump link,
  with no manual second click.
