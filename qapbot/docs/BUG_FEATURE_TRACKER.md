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
the Discord-side "Test cases" button is a jump link once one exists). `testing → done` happens
automatically once every environment that has at least one test case is fully passed — an
item with only DEV cases never waits on PROD. `❌ Failed` reverts `testing → in_progress`,
keeping already-passed environments' sign-off (never resets them).

**Move-on-`done` (2026-08-22)**: the moment any path sets `status = done` — the status
dropdown, the bridge/MCP `tracker_set_status` tool, or the automatic transition above —
`apply_status_change()` reposts the item's embed into the configured **Implemented** channel,
with its Edit/Add files/Status/Test cases buttons stripped (no `view=` on the repost — nothing
left to do on a closed item), and deletes the old copy from the reports channel. If a test-case
message exists, it's likewise reposted into the configured **Done Testing** channel with its
Pass/Fail buttons stripped and the old copy deleted from the test channel. Both channels are
optional — unconfigured means the move is skipped and the item/test-message stay where they
are, so a setup that never configures them behaves exactly as before this feature existed.
Discord threads can't move channels, so the reposted item embed adds a "Discussion thread"
jump-link field when one exists (the old thread otherwise becomes unreachable once its parent
message is deleted). See `_move_item_to_implemented_channel()` /
`_move_test_message_to_done_testing_channel()`.

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
POST /api/tracker/items                {item_type, title, description, details?, environment?, priority?}
GET  /api/tracker/items/{n}
GET  /api/tracker/items/{n}/attachments/{aid}
POST /api/tracker/items/{n}/status     {status, note}
POST /api/tracker/items/{n}/comment    {text}
POST /api/tracker/items/{n}/testcases  {cases: [{environment, description, priority?}]}
```

All handlers delegate the actual DB+Discord work to `qapbot/ui_tracker.py`
(`create_tracker_item_for_agent()` / `apply_status_change()` / `post_comment()` /
`post_test_cases()`) — there is exactly one place each of those things happens, whether
triggered from Discord or from an agent.

### MCP server (`qapbot/mcp/tracker_mcp.py`)

Stdio server, launched as `python -m qapbot.mcp.tracker_mcp` by both `.mcp.json` (Claude Code)
and `.vscode/mcp.json` (VS Code Copilot Chat). Hand-rolled JSON-RPC 2.0 — the `mcp` PyPI
package is **not** a project dependency; the wire surface needed (`initialize`, `tools/list`,
`tools/call`) is small enough that adding a new dependency wasn't worth it.

Six tools: `tracker_list_items`, `tracker_get_item`, `tracker_create_item`, `tracker_set_status`,
`tracker_comment`, `tracker_add_testcases`. All but the first two write something — no
filesystem/shell/git/deploy tool is exposed here (plan §6.6).

`tracker_create_item` (tracker item #0015, 2026-08-22) lets an agent file a new bug/feature
directly instead of asking a human to run `/bug`/`/feature` — it posts to the same reports
channel with the same embed/buttons a human-filed item gets. Its `reporter_id` is
`agent:<X-Tracker-Admin label>` (e.g. `agent:claude`), deliberately non-numeric so it can never
collide with a real Discord snowflake; this means the reporter-DM-on-status-change step silently
no-ops (nobody to DM) and only a bot admin — not "the reporter" — can Edit it from Discord. Both
are acceptable: there is no Discord user to notify or to grant reporter-only edit rights to.

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
