# Auto-archive done tracker items into two new channels

## Context

`/bug` and `/feature` items currently stay posted in their single "reports" channel
(`tracker_bug_channel_id`) forever, through every status including `done`/`rejected`. Same
for the test-case sign-off message, which stays in the "test" channel
(`tracker_test_channel_id`) with live Pass/Fail buttons even after the item is fully closed.
The user finds this makes the reports channel too long and mixes closed work with open work,
making it hard to see what's still outstanding.

Clarified requirement (via AskUserQuestion): bugs/features never move to a "done testing"
channel — they move from the reports channel to a new **Implemented** channel the moment
their status becomes `done` (not `implemented`; the name is the user's choice, the trigger is
the `done` transition). Separately, the *test-case message* moves from the test channel to a
new **Done Testing** channel the moment it's fully signed off (👍, which is exactly what
drives the same `done` transition) — and its Pass/Fail buttons must be stripped when it lands
there, since there's nothing left to sign off.

Both new channels are configured through `/admin` → *Bot Setup*, alongside the existing two.

## Design

### New bot_settings keys (`qapbot/ui_tracker.py` top-of-file constants)

```python
TRACKER_SETTING_IMPLEMENTED_CHANNEL = "tracker_implemented_channel_id"
TRACKER_SETTING_DONE_TESTING_CHANNEL = "tracker_done_testing_channel_id"
```

### `BotSetupView` (`qapbot/ui_tracker.py`)

Extended `_SLOTS` from 2 to 4 entries: `bug`, `test`, `implemented`, `done_testing`. Each is a
fully generic `(slot_key, setting_key, label_key)` tuple consumed by
`_add_channel_select`/`_on_save`/`format_header`'s `channel_text()` closure — no per-slot
special-casing needed.

Row budget: 4 `ChannelSelect`s take rows 0-3, the Save/Toggle/Close button row moves to row 4
(`_add_buttons(row=len(self._SLOTS))`) — exactly Discord's 5-row cap, no headroom left.

`format_header()` gained two more `channel_text(...)` calls passed into the `bot_setup.header`
i18n string as `implemented_channel`/`done_testing_channel`.

### i18n (`qapbot/translations/en.json` + `de.json`, `ui_components.bot_setup` block)

- Extended `header` with two more lines.
- Added `implemented_label` / `done_testing_label`.
- Added `ui_components.tracker.discussion_thread_field` = "Discussion thread" /
  "Diskussions-Thread" — used as an embed field name (see below).

### Moving the item post on `done` (`apply_status_change`)

`apply_status_change` previously called `_refresh_item_message(item)` unconditionally after
the DB write. Now, when `new_status == "done"`:

```python
moved = await _move_item_to_implemented_channel(item)
if not moved:
    await _refresh_item_message(item)
await _move_test_message_to_done_testing_channel(item)
item = await db.get_tracker_item(item_number)  # pick up new channel_id/message_id/test_*
```

This is the single choke point (per the module's design note) so it covers every path that
can set `done`: the admin status dropdown, the bridge/MCP `tracker_set_status` tool, *and*
`mark_environment_passed_and_refresh()`'s automatic transition when the last environment gets
its 👍 — since that function already calls `apply_status_change(item_number, "done", ...)`,
no change was needed there.

`_move_item_to_implemented_channel(item) -> bool` (near `_post_tracker_item`):

- No-op (`False`) if `TRACKER_SETTING_IMPLEMENTED_CHANNEL` isn't configured, or the item is
  already posted there.
- Resolves the target channel with the same `get_channel`/`fetch_channel` fallback as
  `_post_tracker_item`; logs + returns `False` on failure, never raises.
- Reuses `build_tracker_embed()`, `build_tracker_item_view()`, `_build_discord_files()`
  unchanged.
- If `item.get("thread_id")`, adds a "Discussion thread" embed field with a jump link
  (`https://discord.com/channels/{guild}/{thread_id}`) on the local embed copy only — Discord
  threads can't move channels, so once the old message is deleted the thread becomes an
  orphan with no visible parent; this keeps it reachable.
- Posts the new message, best-effort deletes the old one (catches
  `NotFound`/`Forbidden`/`HTTPException`, logs a warning, never blocks the already-succeeded
  move).
- Persists via `db.update_tracker_item(item_number, channel_id=..., message_id=...)` —
  deliberately *not* `set_tracker_item_message()`, which always overwrites `thread_id`
  (defaulting to `None`) and would wipe the thread reference the jump link depends on.

`_move_test_message_to_done_testing_channel(item) -> bool` (near `post_test_cases`): same
shape, but reposts `_format_testcase_message(item, testcases)` with **no `view=`** (that's how
the Pass/Fail buttons get stripped), and persists `test_channel_id`/`test_message_id`.

No other code needed to change: the "Test cases" jump-link button, `_refresh_testcase_message`,
and `mark_testing_failed` all already resolve by `test_channel_id`/`test_message_id` read
fresh from the DB each time, so they automatically follow the moved location.

### Docs

Updated `qapbot/docs/BUG_FEATURE_TRACKER.md`'s "Discord surface" (move-on-`done` behavior) and
"Runbook" (mentions configuring all four channels, both new ones optional) sections.

### Tests

- `tests/discord/test_ui_bot_setup.py`: prefill/slot-id coverage for the 2 new slots, a save
  test asserting both new bot_settings keys persist, header rendering with all four channels.
- `tests/discord/test_ui_tracker_items.py`: `apply_status_change` transitioning to `done` —
  moves the item to Implemented (embed+view+files, old message deleted, `thread_id`
  preserved) and the test-case message to Done Testing (no `view=`, old message deleted);
  falls back to in-place refresh when Implemented isn't configured; skips cleanly when already
  at the target channel or when no test-case message exists yet.

## Verification

- `.\run_tests.ps1` — full suite: 2320 passed, 4 deselected (no regressions).
- `.\run_tests.ps1 -k "tracker_items or bot_setup"` — 48 passed (the new/modified tests).
- No local end-to-end Discord test possible: `CONFIG.tracker_enabled` is hard-coded off on
  DEV, so `/bug`/`/feature` never register there — same constraint as the rest of this
  tracker, verified via the unit/discord suites only.

## Status

Implemented, tests passing. Not yet committed (only commit when the user asks) — `git mv` this
file into `plans/implemented/` in the same turn as that commit.
