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
  is allowed to touch (never build a raw SQL identifier from caller input).
- `tracker_attachments` — junction, `ON DELETE CASCADE`. `local_path` is the on-disk copy
  under `CONFIG.tracker_data_dir` (agent-readable, immune to Discord's expiring CDN URLs);
  `discord_url` is the bot's own re-upload.
- `tracker_testcases` — junction, `ON DELETE CASCADE`. `seq` orders rows within one
  `(item_number, environment)` pair.

Status lifecycle:

```
open ──▶ triaged ──▶ in_progress ──▶ implemented ──▶ testing ──▶ done
  └──────────────────────────────▶ rejected
  └──────────────────────────────▶ duplicate
```

`implemented → testing` happens automatically the moment `post_test_cases()` runs (Discord
button has no "compose test cases" UI — only the bridge/MCP `tracker_add_testcases` does that;
the Discord-side "Test cases" button is a jump link once one exists). `testing → done` happens
automatically once every environment that has at least one test case is fully passed — an
item with only DEV cases never waits on PROD. `❌ Failed` reverts `testing → in_progress`,
keeping already-passed environments' sign-off (never resets them).

## Discord surface (`qapbot/ui_tracker.py`)

- `/bug`, `/feature` (`QBdiscordcmds.py`) — thin wrappers around `start_tracker_item()`.
  Registered only when `CONFIG.tracker_enabled`; DM-invokable (no `guild_only()`).
- `TrackerItemModal` → `TrackerDraftView` (Edit/Add attachments/Submit/Discard, ephemeral,
  session-scoped) → posted item (embed + persistent `TrackerItemButton` row: Edit/Add
  files/Status/Test cases).
- Attachments: `attachment1..3` command params are pre-downloaded as a background task
  **immediately after** `send_modal()` — the signed CDN URL on the user's own attachment
  expires while they're still filling in the modal. `Add attachments`/`Add files` instead open
  a 5-minute **upload window** (`_upload_windows`, consumed by `handle_tracker_upload_message()`,
  wired into `QapBot.py`'s existing `on_message` handler *before* its DM-fallback branch) —
  the only way to accept a pasted screenshot, since Discord modals can't contain file inputs.
- `TrackerItemButton` / `TrackerTestPassButton` / `TrackerTestFailButton` are restart-safe
  `discord.ui.DynamicItem`s (custom_id scheme: `tracker:<action>:<item_number>`,
  `tracker:test:pass:<item_number>:<DEV|PROD>`, `tracker:test:fail:<item_number>`), registered
  once via `QBcore.bot.add_dynamic_items(...)` in `QapBot.py`'s `_setup_hook()`. Each
  implements `interaction_check()` gating on `QBcore.bot.fully_initialized` (Pitfall 20).
- The 👍-reaction sign-off shortcut is a **new** `on_raw_reaction_add` listener in `QapBot.py`
  (the bot had none before) — bot-admin only, raw (not cached) so it survives restarts.

## Agent integration

### Bridge (`qapbot/web_bridge.py`, `/api/tracker/*`)

Reuses the existing `X-Bridge-Secret` (no second secret — accepted trade-off for a
single-admin setup: holding it grants tracker-admin *and* CWL-endpoint access).
`X-Tracker-Admin` is attribution-only (shows up as `last_edited_by`/comment author), never
authentication — it's self-asserted.

```
GET  /api/tracker/items?status=&type=&limit=
GET  /api/tracker/items/{n}
GET  /api/tracker/items/{n}/attachments/{aid}
POST /api/tracker/items/{n}/status     {status, note}
POST /api/tracker/items/{n}/comment    {text}
POST /api/tracker/items/{n}/testcases  {cases: [{environment, description}]}
```

All handlers delegate the actual DB+Discord work to `qapbot/ui_tracker.py`
(`apply_status_change()` / `post_comment()` / `post_test_cases()`) — there is exactly one
place each of those things happens, whether triggered from Discord or from an agent.

### MCP server (`qapbot/mcp/tracker_mcp.py`)

Stdio server, launched as `python -m qapbot.mcp.tracker_mcp` by both `.mcp.json` (Claude Code)
and `.vscode/mcp.json` (VS Code Copilot Chat). Hand-rolled JSON-RPC 2.0 — the `mcp` PyPI
package is **not** a project dependency; the wire surface needed (`initialize`, `tools/list`,
`tools/call`) is small enough that adding a new dependency wasn't worth it.

Five tools: `tracker_list_items`, `tracker_get_item`, `tracker_set_status`, `tracker_comment`,
`tracker_add_testcases`. Only the last three write anything — no filesystem/shell/git/deploy
tool is exposed here (plan §6.6).

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
  PROD to configure the three channels (bug reports, feature requests, manual test cases).
- **Disable at runtime without restarting**: `/admin` → *Bot Setup* → "Disable tracker" — this
  flips `bot_settings.tracker_enabled` (the *runtime* switch); `/bug`/`/feature` reply
  ephemerally that the tracker is off. There is no way to disable command *registration* itself
  short of running that instance in DEV mode.
- **MCP setup**: set `TRACKER_BRIDGE_URL`/`TRACKER_BRIDGE_SECRET`/`TRACKER_ADMIN_ID` in your
  shell/`.env` before launching VS Code or Claude Code — `.mcp.json`/`.vscode/mcp.json`
  reference them via `${env:...}`, never as literals (Cardinal Rule 16).
- **Attachments on disk**: `CONFIG.tracker_data_dir` (default `data/tracker`), one subdirectory
  per item (`0001/`, `0002/`, ...). Kept indefinitely — no automatic purge (plan §8.3).

## See also

- `BUG_FEATURE_TRACKER_PLAN.md` — the full design doc (UX walkthrough, alternatives considered,
  every decision's rationale).
- `qapbot/docs/DATABASE_ARCHITECTURE.md` § Bug/Feature Tracker — table definitions.
- `qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md` — the expiring-CDN-attachment-URL pitfall.
