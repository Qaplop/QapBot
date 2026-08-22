# Bug/Feature Tracker

Implements `BUG_FEATURE_TRACKER_PLAN.md` (the full design doc — read it for the "why" behind
every decision below; this doc is the day-to-day reference for working on the shipped code).

## Architecture at a glance

```
/bug, /feature (QBdiscordcmds.py)
        │
        ▼
qapbot/ui_tracker.py        ── all Discord UI + item lifecycle logic
        │
        ▼
CACHE.db_manager (db_manager.py)   ── bot_settings / tracker_items / tracker_attachments /
        │                              tracker_testcases (WarHistoryDB._create_tracker_schema())
        │
        ▼
qapbot/web_bridge.py  /api/tracker/*   ── HTTP surface for the agent side, X-Bridge-Secret gated
        │
        ▼
qapbot/mcp/tracker_mcp.py   ── stdio MCP server, 5 tools, used by Copilot Chat / Claude Code
```

Only PROD ever owns the tracker: `CONFIG.tracker_enabled` is hard-coded to `not is_dev_mode`
in `qapbot/config.py`'s `load_config()` — there is no env var, and DEV can never register
`/bug`/`/feature` in any configuration. This is deliberate: PROD's DB (including
`bot_settings`' tracker channel IDs) is routinely copied to DEV for realistic-data testing, so
an independently-configurable toggle would let DEV post real-looking items into PROD's actual
channels. There is currently no way to live-test the Discord-side tracker commands on DEV —
use the unit/discord/integration test suites instead.

**This invariant has to be enforced at every entry point individually — slash-command
registration alone doesn't cover everything** (2026-08-22 live bug report: DEV reacted to a 👍
on a PROD-authored test-case message, marked environments passed in its own PROD-backup-seeded
copy of that tracker item, then failed with a 403 editing a message it doesn't own, and still
posted its own "mark item done too?" prompt into the thread). The reason slash-command gating
alone isn't enough: a raw gateway event like `on_raw_reaction_add` fires for **every bot present
in a channel**, regardless of which bot's message was reacted to — unlike a component
interaction (button click), which Discord delivers only to the application that owns the
message. So "DEV's copy of a DynamicItem button is safe because DEV never receives the
interaction" does NOT generalize to reaction listeners, where DEV very much does receive the
event and, since its DB is a PROD copy, finds a real matching row to act on.
`handle_tracker_test_reaction()` now checks `CONFIG.tracker_enabled` explicitly as its first
line for exactly this reason — any *future* raw event listener (reactions, message edits, etc.)
touching tracker state needs the same explicit check; do not rely on "DEV won't have a matching
row" or "DEV won't receive this event" without verifying which kind of Discord event it actually
is.

## Data model

Four tables, all created in `WarHistoryDB._create_tracker_schema()` (called from
`initialize_database()`), hot-only (no `history` mirror — Cardinal Rule 1 parity doesn't
apply, but Rule 14 named-column access still does):

- `bot_settings` — bot-wide key/value config (`tracker_*_channel_id`, `tracker_guild_id`,
  `tracker_enabled`). `PRIMARY KEY (guild_id, key)`, `guild_id=''` = global (the only scope
  used today). Cached on `CACHE.tracker_settings`, write-through via `CACHE.set_tracker_setting()`.
- `tracker_items` — one row per filed bug/feature. `item_number` (AUTOINCREMENT) is a single
  shared `#NNNN` pool across both `item_type` values. See `db_manager.py`'s
  `_TRACKER_ITEM_UPDATABLE_COLUMNS` whitelist for which columns `update_tracker_item(**fields)`
  is allowed to touch (never build a raw SQL identifier from caller input). `priority`
  (HIGH/MEDIUM/LOW, default MEDIUM) is set by the reporter in the modal.
- `tracker_attachments` — junction, `ON DELETE CASCADE`. `local_path` is the on-disk copy
  under `CONFIG.tracker_data_dir` (agent-readable, immune to Discord's expiring CDN URLs);
  `discord_url` is the bot's own re-upload.
- `tracker_testcases` — junction, `ON DELETE CASCADE`. `seq` orders rows within one
  `(item_number, environment)` pair. `priority` (HIGH/MEDIUM/LOW, default MEDIUM) is set
  per-row by whoever calls `tracker_add_testcases` (there is no Discord modal for composing
  test cases — see below) and shown next to each case in the posted test-case message.

There is no cap on how many open items one reporter can have — `/bug`/`/feature` always opens
the modal (removed 2026-08-22 per the project owner's request; see `BUG_FEATURE_TRACKER_PLAN.md`
for the original rationale).

Status lifecycle:

```
open ──▶ triaged ──▶ in_progress ──▶ implemented ──▶ testing ──▶ done
  └──────────────────────────────▶ rejected
  └──────────────────────────────▶ duplicate
```

`implemented → testing` happens automatically the moment `post_test_cases()` runs (Discord
button has no "compose test cases" UI — only the bridge/MCP `tracker_add_testcases` does that;
the Discord-side "Test cases" button is a jump link once one exists). `❌ Failed` reverts
`testing → in_progress`, keeping already-passed environments' sign-off (never resets them).

**`testing → done` is NOT automatic any more (decoupled 2026-08-22, tracker item #0015
follow-up).** Completing every environment's test cases used to force the item straight to
`done` as a side effect — by design now, the item and its test-case message are two
independent objects that only ever move on their OWN trigger:

- **The item** only reaches `done` via an explicit `apply_status_change(item_number, "done", ...)`
  call — the status dropdown, `tracker_set_status`, or someone accepting the "mark done too?"
  prompt described below. Reaching `done` never touches the test-case message.
- **The test-case message** only archives to the Done Testing channel via
  `move_testcases_to_done_channel()`/`finalize_testcases_move()` — triggered by its own
  completion (every environment with cases fully passed, detected as an edge-trigger in
  `mark_environment_passed_and_refresh()`/the 👍-reaction shortcut so it only fires once, not on
  every call against an already-fully-passed item) or by the manual `[ 📦 Move to Done ]`
  button/`tracker_move_testcases_done` MCP tool. The manual path works even with cases still
  unchecked — it asks for confirmation first (`ConfirmForceMoveView` / the bridge's
  `needs_confirmation`/`unchecked_count` response, force-able with `{"force": true}`) — and
  never silently marks the remaining cases passed; they stay visibly unchecked in the archived
  message. `_refresh_testcase_message()` (any later in-place refresh of that same message) checks
  whether `test_channel_id` already equals the configured Done Testing channel and, if so, passes
  `view=None` instead of rebuilding the interactive Pass/Fail/Move-to-Done view — mirroring
  `_refresh_item_message()`'s own `archived` handling for the item embed. Without this a later
  refresh (e.g. a redundant 👍 on an already-fully-passed message) would resurrect the buttons on
  a message that was deliberately stripped (2026-08-22 live bug report).
- Whenever a test-case set just finished (naturally or via the forced manual move),
  `get_linked_item_if_eligible_for_done()` checks whether the linked item is still open (not
  already `done`/`rejected`/`duplicate`) and, if so, offers a "mark item done too?" prompt:
  `ConfirmItemDoneView` as an ephemeral followup (Pass button, Move-to-Done button) or, for the
  👍-reaction shortcut (no interaction to attach an ephemeral to), a message with the same Yes/No
  buttons posted to the item's discussion thread, falling back to a DM to the reactor if there's
  no thread. The bridge/MCP surface never blocks on this — `tracker_mark_testcase_passed`/
  `tracker_move_testcases_done`'s response just names the linked item and status and suggests
  calling `tracker_set_status(..., "done")` if the agent wants to.
- The passive (thread/DM) prompt opens with an `<@actor_id>` mention (`item_done_confirm_prompt_
  passive`, 2026-08-22 live bug report: item #0015's prompt sat unnoticed in the thread because a
  plain, unmentioned message doesn't highlight or notify anyone in Discord). The ephemeral variant
  (`item_done_confirm_prompt`) has no mention — it's already private to the person who just
  clicked the button. There is no Discord API for a bot to move a user's client view to a
  channel/thread directly; a mention is the closest equivalent, since its notification is
  clickable and jumps straight to that exact message.

**Move-on-terminal-status (2026-08-22, item side only since the follow-up above; widened same
day to `rejected`/`duplicate`)**: the moment any path sets `status` to `done`, `rejected`, or
`duplicate` — the status dropdown, the bridge/MCP `tracker_set_status` tool, or accepting the
"mark done too?" prompt — `apply_status_change()` reposts the item's embed into the configured
**Implemented** channel, with its Edit/Add files/Status/Test cases buttons stripped (no `view=`
on the repost — nothing left to do on a closed item), and deletes the old copy from the reports
channel. All three are equally terminal: `rejected`/`duplicate` originally fell into the plain
in-place-refresh branch and lingered in the working reports channel indefinitely (live bug found
via tracker #0010's own closure) — an item that will never move again needs to leave the
unfinished-workload channel regardless of *why* it closed. The Implemented channel is optional —
unconfigured means the move is skipped and the item stays where it is. Discord threads can't
move channels, so the reposted item embed adds a "Discussion thread" jump-link field when one
exists (the old thread otherwise becomes unreachable once its parent message is deleted). See
`_move_item_to_implemented_channel()`.

**Grant/revoke requestor access (2026-08-22, ticket #0021)**: reporters normally can't see the
reports channel their item was posted in (it isn't open to `@everyone`), so a staff `@mention`
reply gets no push notification and the reporter can't read the thread. The **"Reply to
requestor"** button on `TrackerItemButton` (`grantaccess` action, admin-only) gives the reporter
a member-specific Discord permission overwrite on that channel
(`view_channel`/`read_message_history`/`send_messages_in_threads`, `_handle_grant_access()`), then
replies (ephemeral, to the admin who clicked) with a jump link to the item's discussion thread
(or the item message itself if it has no thread) — the bot never posts the reply/mention itself,
this only gets the admin's own client to that spot ready to type one, same "no client-navigation
API, a link is the closest thing" reasoning as the passive-prompt mention above.
This is deliberately **channel-wide**, not scoped to just their own message/thread — Discord's
narrower primitive for that would be a standalone **private thread** with `thread.add_user()`
(membership on a private thread grants access independent of the parent channel's own view
permission), but that would mean creating the discussion thread separately from the item message
instead of via `message.create_thread()`. The chosen trade-off accepts that a reporter granted
access can also see other tickets filed in the same channel, in exchange for keeping the existing
public-thread-off-the-message flow unchanged. `apply_status_change()` calls
`_revoke_requestor_access()` to undo the overwrite once the item actually moves to the
Implemented channel on a terminal status (not merely reaches one — an unconfigured Implemented
channel leaves the item, and the reporter's access, right where they are) — unless the same
reporter still has another open item sitting in that same working channel, checked via
`list_tracker_items(reporter_id=..., guild_id=...)`, in which case the overwrite is left in place
so it doesn't cut them off from that other item too.

## Discord surface (`qapbot/ui_tracker.py`)

- `/bug`, `/feature` (`QBdiscordcmds.py`) — thin wrappers around `start_tracker_item()`.
  Registered only when `CONFIG.tracker_enabled`; DM-invokable (no `guild_only()`).
- `TrackerItemModal` → `TrackerDraftView` (Edit/Add attachments/Submit/Discard, ephemeral,
  session-scoped) → posted item (embed + persistent `TrackerItemButton` row: Edit/Add
  files/Status/Test cases/Grant access).
- Attachments: `attachment1..3` command params are pre-downloaded as a background task
  **immediately after** `send_modal()` — the signed CDN URL on the user's own attachment
  expires while they're still filling in the modal. `Add attachments`/`Add files` instead open
  a 5-minute **upload window** (`_upload_windows`, consumed by `handle_tracker_upload_message()`,
  wired into `QapBot.py`'s existing `on_message` handler *before* its DM-fallback branch) —
  the only way to accept a pasted screenshot, since Discord modals can't contain file inputs.
- `TrackerItemButton` / `TrackerTestPassButton` / `TrackerTestFailButton` / `TrackerTestMoveDoneButton`
  are restart-safe `discord.ui.DynamicItem`s (custom_id scheme: `tracker:<action>:<item_number>`,
  `tracker:test:pass:<item_number>:<DEV|PROD>`, `tracker:test:fail:<item_number>`,
  `tracker:test:movedone:<item_number>`), registered once via `QBcore.bot.add_dynamic_items(...)`
  in `QapBot.py`'s `_setup_hook()`. Each implements `interaction_check()` gating on
  `QBcore.bot.fully_initialized` (Pitfall 20). `ConfirmItemDoneView`/`ConfirmForceMoveView` are
  short-lived, session-scoped Yes/No prompts (not restart-safe by design, same convention as
  `TrackerDraftView`) used by the decoupled done-linkage flow above.
- The 👍-reaction sign-off shortcut is a **new** `on_raw_reaction_add` listener in `QapBot.py`
  (the bot had none before) — bot-admin only, raw (not cached) so it survives restarts. A
  redundant reaction with nothing left pending is a true no-op (returns before touching the
  message at all — see `_refresh_testcase_message()`'s archived handling above).

## Agent integration

### Bridge (`qapbot/web_bridge.py`, `/api/tracker/*`)

Reuses the existing `X-Bridge-Secret` (no second secret — accepted trade-off for a
single-admin setup: holding it grants tracker-admin *and* CWL-endpoint access).
`X-Tracker-Admin` is attribution-only (shows up as `last_edited_by`/comment author), never
authentication — it's self-asserted.

```
GET  /api/tracker/items?status=&type=&limit=
POST /api/tracker/items                {item_type, title, description, details?, environment?, priority?}
GET  /api/tracker/items/{n}
GET  /api/tracker/items/{n}/attachments/{aid}
GET  /api/tracker/items/{n}/thread?limit=
POST /api/tracker/items/{n}/status     {status, note}
POST /api/tracker/items/{n}/comment    {text}
POST /api/tracker/items/{n}/testcases  {cases: [{environment, description, priority?}]}
POST /api/tracker/items/{n}/testcases/pass  {environment}
POST /api/tracker/items/{n}/testcases/fail  {}
POST /api/tracker/items/{n}/testcases/move-done  {force?}
```

All handlers delegate the actual DB+Discord work to `qapbot/ui_tracker.py`
(`create_tracker_item_for_agent()` / `apply_status_change()` / `post_comment()` /
`get_thread_messages()` / `post_test_cases()` / `mark_environment_passed_and_refresh()` /
`mark_testing_failed()` / `finalize_testcases_move()`) — there is exactly one place each of those
things happens, whether triggered from Discord or from an agent.

### MCP server (`qapbot/mcp/tracker_mcp.py`)

Stdio server, launched as `python -m qapbot.mcp.tracker_mcp` by both `.mcp.json` (Claude Code)
and `.vscode/mcp.json` (VS Code Copilot Chat). Hand-rolled JSON-RPC 2.0 — the `mcp` PyPI
package is **not** a project dependency; the wire surface needed (`initialize`, `tools/list`,
`tools/call`) is small enough that adding a new dependency wasn't worth it.

Ten tools: `tracker_list_items`, `tracker_get_item`, `tracker_get_thread`, `tracker_create_item`,
`tracker_set_status`, `tracker_comment`, `tracker_add_testcases`, `tracker_mark_testcase_passed`,
`tracker_mark_testcase_failed`, `tracker_move_testcases_done`. Only the first three are read-only
— no filesystem/shell/git/deploy tool is exposed here (plan §6.6).

`tracker_get_thread` (2026-08-22) closes a one-way gap: `tracker_comment` could only ever WRITE
into an item's Discord discussion thread — there was no way for an agent to read a human's
replies, clarifications, or the automated notes (test-fail notes, other comments) already posted
there. Backed by a new `get_thread_messages()` in `ui_tracker.py`, which resolves the thread the
same way `post_comment()` already does and calls `Thread.history()`, reversed into chronological
(oldest-first) order. Returns `[]` rather than raising when the item simply has no thread yet
(best-effort thread creation in `_post_tracker_item()` can fail) — only an unknown `item_number`
raises. Since `_post_tracker_item()` seeds the thread with the item's own untruncated
title/description/details as its first message(s) (the embed-overflow strategy, §2.3), this tool
doubles as a way to read a long report that `tracker_get_item`'s embed truncated. Thread content
goes through the same `sanitize_field()`/`wrap_untrusted()` treatment as every other
reporter-supplied field — arbitrary Discord-user chat is exactly as untrusted as a report's
description/details field, just free-form rather than one structured value, so the whole
transcript is wrapped as a single block instead of per-message.

`tracker_create_item` (tracker item #0015, 2026-08-22) lets an agent file a new bug/feature
directly instead of asking a human to run `/bug`/`/feature` — it posts to the same reports
channel with the same embed/buttons a human-filed item gets. Its `reporter_id` is
`agent:<X-Tracker-Admin label>` (e.g. `agent:claude`), deliberately non-numeric so it can never
collide with a real Discord snowflake; this means the reporter-DM-on-status-change step silently
no-ops (nobody to DM) and only a bot admin — not "the reporter" — can Edit it from Discord. Both
are acceptable: there is no Discord user to notify or to grant reporter-only edit rights to.

`tracker_mark_testcase_passed`/`tracker_mark_testcase_failed` (2026-08-22) are the agent-drivable
equivalent of the Discord "✅ passed"/"❌ Failed" test-case buttons — previously the only way to
sign off a testing round. `mark_environment_passed_and_refresh()`/`mark_testing_failed()` never
actually depended on a Discord interaction (only on `QBcore.bot` for posting/refreshing
messages), so this was pure wiring: no new tracker-state logic, just a bridge
endpoint + MCP tool pointing at the same functions Discord's buttons already call. Marking an
environment passed no longer touches the item's status at all (decoupled in a follow-up the same
day, tracker item #0015 follow-up) — see the status-lifecycle section above for what happens
instead once every environment with cases is fully passed. Marking failed still reverts the item
to `in_progress`, keeping any already-passed environments' sign-off (that direction of coupling
was deliberately left alone).

`tracker_move_testcases_done` (2026-08-22 follow-up, point 4) is the agent-drivable equivalent of
the new `[ 📦 Move to Done ]` Discord button — archives the test-case message regardless of
whether every case is checked off. Without `force: true`, an incomplete set isn't moved; the
response's `needs_confirmation`/`unchecked_count` fields tell the caller how many cases remain so
it can decide whether to finish them first or retry with `force: true`. Never auto-marks the
remaining cases passed — they stay visibly unchecked in the archived message either way.

**Untrusted input (`qapbot/mcp/tracker_envelope.py`)**: bug/feature reports are arbitrary text
from arbitrary Discord users, fed straight into an agent's context — a textbook
prompt-injection surface. Every reporter-supplied field returned by `tracker_get_item` is
`sanitize_field()`'d (control chars stripped, ` ``` ` fences neutralized, length capped) then
`wrap_untrusted()`'d (`<user_report id="NNNN" field="..." trust="untrusted">…</user_report>`)
before it reaches the tool result.

**Local cache**: `tracker_get_item` downloads attachments and writes a rendered
`.tracker-cache/NNNN/item.md` under the workspace root (`.gitignore`d — never commit
reporter-uploaded content). `cache_path_for_item()` validates every write stays inside that
directory.

## Runbook

- **Enable the tracker**: nothing to set — `/bug`/`/feature` are registered automatically on
  PROD startup (`CONFIG.tracker_enabled = not is_dev_mode`). Just run `/admin` → *Bot Setup* on
  PROD to configure the channels: bug & feature reports, manual test cases, and (optional) the
  Implemented/Done Testing move-on-`done` archive channels above. `/bug` and `/feature` both
  post to the same reports channel (tracker item #0006, 2026-08-21 — they used to have separate
  channels; `item_type` still distinguishes them in the embed/emoji, just not in routing any
  more).
- **Disable at runtime without restarting**: `/admin` → *Bot Setup* → "Disable tracker" — this
  flips `bot_settings.tracker_enabled` (the *runtime* switch); `/bug`/`/feature` reply
  ephemerally that the tracker is off. There is no way to disable command *registration* itself
  short of running that instance in DEV mode.
- **MCP setup**: set `TRACKER_BRIDGE_URL`/`TRACKER_BRIDGE_SECRET`/`TRACKER_ADMIN_ID` in `.env`.
  `tracker_mcp.py` calls `load_dotenv(override=False)` itself (same convention as
  `qapbot/config.py`), so `.env` alone is sufficient — never put these as literals in
  `.mcp.json`/`.vscode/mcp.json` (Cardinal Rule 16). `.vscode/mcp.json` still passes them via
  `${env:...}`/`${workspaceFolder}`, which VS Code resolves against its own environment;
  `.mcp.json` (Claude Code CLI) does *not* reliably expand `${env:...}`/`${workspaceFolder}` —
  it uses a literal `command`/`cwd` path and relies on `.env` alone for the secrets.
- **After editing `.mcp.json`**: Claude Code only re-reads MCP server config at CLI startup —
  restart the session (`claude`) and approve the server (it shows "⏸ Pending approval" until
  then) before the tracker tools become available.
- **Attachments on disk**: `CONFIG.tracker_data_dir` (default `tracker`, project root — like
  `investigate_dir`, deliberately NOT under `data_dir`/`PROD_DATA_DIR`'s SSD, since attachment
  storage isn't performance-critical and benefits from the HDD's larger free space; override
  with `TRACKER_DATA_DIR`), one subdirectory per item (`0001/`, `0002/`, ...). Kept indefinitely
  — no automatic purge (plan §8.3).
- **Claude Code finishing a tracker-linked item**: never move an item to `implemented` without
  also posting at least one test case (`tracker_add_testcases` MCP tool, or the bridge's
  `POST /api/tracker/items/{n}/testcases` with `{"cases": [{"environment": "PROD"|"DEV"|"BOTH",
  "description": "...", "priority": "HIGH"|"MEDIUM"|"LOW"}]}` — priority is optional per case
  (defaults to MEDIUM), pick it based on how critical that specific case is to verify — when MCP
  isn't wired up in the session) — see
  `.github/copilot-instructions.md` Cardinal Rule 15. Posting the test case is what actually
  drives `post_test_cases()`'s automatic `implemented → testing` transition; skipping it leaves
  the item in a state the tracker's own lifecycle doesn't expect anyone to leave it in.

## See also

- `BUG_FEATURE_TRACKER_PLAN.md` — the full design doc (UX walkthrough, alternatives considered,
  every decision's rationale).
- `qapbot/docs/DATABASE_ARCHITECTURE.md` § Bug/Feature Tracker — table definitions.
- `qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md` — the expiring-CDN-attachment-URL pitfall.
