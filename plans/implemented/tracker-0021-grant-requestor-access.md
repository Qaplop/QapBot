# Reactive "Reply to requestor" access grant for tracker items

## Context

Ticket #21's reporter can't see the automation/reports channel their item was posted in, so a
staff reply that `@mention`s them for a push notification silently fails — they have no
permission to view the channel or its discussion thread. Opening the whole channel to
`@everyone` isn't acceptable (it's an internal automation channel). We discussed the Discord
mechanisms available and the user explicitly chose the simpler, broader option over a
per-thread-scoped private-thread approach: a new **"Reply to requestor" button** on each posted
tracker item that grants the reporter a channel-wide permission overwrite (view + read history +
reply-in-threads), accepting that this exposes *other* tickets in the same channel to that
reporter too ("people who take the effort of filing a ticket I reply to can see the channel").
To avoid an ever-growing set of outside members with standing access, that overwrite is revoked
automatically once the item is archived (moved to the Implemented channel on a terminal status),
unless the same reporter still has another open item in that channel.

## Approach

All changes live in `qapbot/ui_tracker.py`, its two translation files, its test file, and its
doc — no DB schema change needed (`reporter_id`/`guild_id`/`channel_id` already exist on
`tracker_items`).

### 1. New button action: `grantaccess`

- `TRACKER_ITEM_BUTTON_TEMPLATE` regex ([ui_tracker.py:1246](qapbot/ui_tracker.py#L1246)): add
  `grantaccess` to the `action` alternation.
- `TrackerItemButton._LABEL_KEYS` / `_STYLES` ([ui_tracker.py:1257-1268](qapbot/ui_tracker.py#L1257-L1268)):
  add `"grantaccess": 'ui_components.tracker.button_grant_access'` (secondary style).
- `build_tracker_item_view()` ([ui_tracker.py:933](qapbot/ui_tracker.py#L933)): add the new
  `TrackerItemButton("grantaccess", item_number)` to the row, alongside edit/addfiles/status/testcases.
- `TrackerItemButton.callback()` dispatch ([ui_tracker.py:1297](qapbot/ui_tracker.py#L1297)):
  add the `grantaccess` branch calling a new `_handle_grant_access()`.

### 2. `_handle_grant_access(interaction, item)` — grant

Mirrors `_handle_status()`'s admin gate ([ui_tracker.py:1359](qapbot/ui_tracker.py#L1359)):

- Deny (ephemeral `grant_access_denied`) unless `check_bot_admin_only(interaction, CONFIG.server_admin)`
  — this is a staff action, same bar as changing status, not "reporter or admin" like edit.
- `reporter_id = item["reporter_id"]`; if not `.isdigit()` (agent-filed items use `agent:<label>`,
  per [ui_tracker.py:862-864](qapbot/ui_tracker.py#L862-L864)) → ephemeral `grant_access_no_user`, return.
- Resolve the member via `interaction.guild.get_member(int(reporter_id))`, falling back to
  `await interaction.guild.fetch_member(...)` inside a `try/except discord.NotFound` →
  ephemeral `grant_access_member_not_found`.
- Build `discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages_in_threads=True)`
  — view + read everywhere in the channel, but only reply inside threads (their own ticket's
  thread), not post new top-level messages in the working channel. Leaving `send_messages` unset
  means it keeps inheriting from the `@everyone`/role deny that already exists on this channel.
- `await interaction.channel.set_permissions(member, overwrite=overwrite, reason=f"Tracker #{item_number}: grant requestor access to reply")`
  wrapped in `try/except (discord.Forbidden, discord.HTTPException)` → ephemeral `grant_access_failed`
  + `logging.warning(...)`.
- On success: ephemeral `grant_access_granted` (mentions the member) to the admin who clicked.

### 3. Revoke on archive

New helper, called only from the terminal-status branch of `apply_status_change()`
([ui_tracker.py:1138-1154](qapbot/ui_tracker.py#L1138-L1154)):

```python
async def _revoke_requestor_access(item: Dict[str, Any], channel_id: str) -> None:
    """Undo _handle_grant_access()'s overwrite once an item archives, unless the same
    reporter still has another open item sitting in the same working channel."""
```

- Skip (return) if `reporter_id` isn't `.isdigit()`.
- `others = await CACHE.db_manager.list_tracker_items(reporter_id=reporter_id, guild_id=item.get("guild_id"))`
  (existing method, [db_manager.py:2873](qapbot/db_manager.py#L2873)) — skip revoke if any other
  item (`item_number` different) has `channel_id == channel_id` and `status not in ("done", "rejected", "duplicate")`.
- Otherwise resolve the channel/guild/member via `QBcore.bot.get_channel`/`fetch_channel` and
  `QBcore.bot.get_guild(...)`/`guild.get_member(...)`, then
  `await channel.set_permissions(member, overwrite=None, reason=f"Tracker #{item_number} closed: revoke requestor access")`.
- All lookups tolerate `None`/failure by just returning (log at `warning`), matching this module's
  existing "best-effort Discord side-effect, never raise" convention (e.g. `_move_item_to_implemented_channel`,
  `_post_tracker_item`).

Wire-up in `apply_status_change()`: capture `original_channel_id = item.get("channel_id")` *before*
calling `_move_item_to_implemented_channel(item)` (that call mutates the DB row's `channel_id`), and
only call `_revoke_requestor_access(item, original_channel_id)` **when `moved` is `True`** — i.e.
revoke happens specifically when the item actually leaves the working channel for the Implemented
channel, matching "when the ticket is closed and moved to the archive". If no Implemented channel
is configured (`moved == False`), the item stays put and access is left alone.

### 4. Translations (`en.json` / `de.json`)

Add alongside the existing `button_*`/`*_denied` keys ([en.json:414-446](qapbot/translations/en.json#L414-L446)):
`button_grant_access`, `grant_access_denied`, `grant_access_no_user`,
`grant_access_member_not_found`, `grant_access_granted`, `grant_access_failed` — same keys in
both locale files, mirroring the existing 1:1 structure.

### 5. Tests (`tests/discord/test_ui_tracker_items.py`)

Reuse existing fixtures/helpers (`db`, `mock_interaction`, `_wire_bot`/`_wire_bot_multi`, `_make_item`).
`mock_interaction.guild` needs `.get_member`/`.fetch_member` configured per test (it's a bare
`MagicMock` today, per [conftest.py:97-99](tests/conftest.py#L97-L99)). New cases:

- Grant denied for a non-admin clicker.
- Grant succeeds for the admin: asserts `interaction.channel.set_permissions` called with the
  resolved member and the expected overwrite flags.
- Grant on an agent-filed item (`reporter_id="agent:foo"`) → `grant_access_no_user`, no Discord call.
- `apply_status_change(..., "done")` with a configured Implemented channel revokes access
  (`set_permissions(member, overwrite=None)`) on the **original** channel once moved.
- Same, but the reporter has a second still-open item in that channel → revoke skipped.
- Same, but no Implemented channel configured (`moved == False`) → revoke skipped, access untouched.

### 6. Docs

`qapbot/docs/BUG_FEATURE_TRACKER.md`:
- Extend the button list at [line 149-150](qapbot/docs/BUG_FEATURE_TRACKER.md#L149-L150)
  ("Edit/Add files/Status/Test cases") to include "Grant access".
- Add a short paragraph near the "Move-on-terminal-status" section
  ([lines 129-142](qapbot/docs/BUG_FEATURE_TRACKER.md#L129-L142)) documenting the grant/revoke
  mechanism, the accepted trade-off (channel-wide, not thread-scoped — a deliberate choice over
  Discord's private-thread `add_user()` primitive), and the multi-open-item guard.

### 7. Changelog

Add a new top entry to `changelog.txt` dated 2026-08-22, numbered one past the current top entry
`(21)` → `(22)`, describing the feature, motivating ticket (#21), and files touched.

## Verification

- `.\run_tests.ps1 -k grant_access` for the new cases, then a full `.\run_tests.ps1` run.
- Manual: on a live tracker item, click "Reply to requestor" as admin → confirm the reporter (a
  non-staff test account) can now see the channel/thread and reply inside the thread; close the
  item → confirm their channel access is gone (`Manage Channel → Permissions` no longer lists
  them) unless they still have another open item there.
