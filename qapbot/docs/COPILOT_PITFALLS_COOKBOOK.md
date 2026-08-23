# Copilot Pitfalls Cookbook (QapBot)

Purpose: keep short, copy-pasteable snippets for common mistakes.
This is referenced by ../.github/copilot-instructions.md to keep the playbook short.

Note on doc links: use plain text paths only (no markdown links).

---

## Pitfall 1: Select dropdown selection disappears after edit

Symptom: after `edit_message()`, the select resets to the placeholder.

Fix: rebuild the select and mark the chosen option with `default=True`.

```python
options = [
    discord.SelectOption(label=name, value=value, default=(value == selected_value))
    for name, value in items
]

self.remove_item(self.my_select)
self._build_select(selected_value=selected_value)
await interaction.response.edit_message(view=self)
```

See also: ../qapbot/docs/CODE_STRUCTURE.md § Select Dropdown Persistence

---

## Pitfall 2: Multi-step workflows create message clutter

Symptom: users get multiple ephemeral messages for one workflow.

Fix: prefer in-place updates via `edit_message()`; if you must send a new message, delete the old one best-effort.

```python
# Preferred: single-message flow, work finishes within Discord's 3s response window
await interaction.response.edit_message(content="Step 2", view=StepTwoView())

# Work takes longer than 3s: defer, THEN edit the original message via the webhook token —
# NOT interaction.followup.send(), which creates a brand-new message and leaves this one
# (buttons/select and all) behind, stale and still clickable (live bug report, 2026-08-23: a
# Yes/No confirm dialog and a status-select dropdown in qapbot/ui_tracker.py both did exactly
# this — see BUG_FEATURE_TRACKER.md's "Single-use ephemeral prompts must edit themselves" note).
await interaction.response.defer(thinking=False)
# ... slow work ...
await interaction.edit_original_response(content="Step 2", view=None)

# If you genuinely need a NEW message (the message being clicked is a permanent/persistent one
# that should stay, e.g. a toast layered on top of an item card), clean up any PRIOR throwaway
# response first when one exists:
try:
    await interaction.delete_original_response()
except Exception:
    pass
await interaction.response.send_message("Step 2", ephemeral=True)
```

See also: ../qapbot/docs/CODE_STRUCTURE.md § Discord.py Patterns

---

## Pitfall 3: Hardcoded English breaks i18n

Rule: ALL Discord-visible text must use `t()`.

Ephemeral/DM: pass BOTH `user_id` and `guild_id` (fallback chain matters).

```python
from qapbot.i18n import t

user_id = str(interaction.user.id)
guild_id = interaction.guild.id if interaction.guild else None

await interaction.followup.send(
    t("some.key", user_id=user_id, guild_id=guild_id),
    ephemeral=True,
)
```

Public channel: pass only `guild_id`.

```python
await channel.send(t("some.key", guild_id=guild_id))
```

See also: ../qapbot/docs/CODE_STRUCTURE.md § Discord.py Patterns

---

## Pitfall 4: “Interaction already responded” errors

Modal submit pattern:

```python
async def on_submit(self, interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    # ... work ...
    await interaction.followup.send("OK", ephemeral=True)
```

Button/select callbacks:

```python
if interaction.response.is_done():
    await interaction.followup.send("OK", ephemeral=True)
else:
    await interaction.response.send_message("OK", ephemeral=True)
```

See also: ../qapbot/docs/CODE_STRUCTURE.md § Discord.py Patterns (Single-Message Flow, Double-Click Guard)

---

## Pitfall 5: Creating duplicate helpers

Rule: search first.

- Use exact-string/symbol search (grep/ripgrep) to find existing usages of a name.
- Use semantic/fuzzy search (or just browse related modules) to answer "do we already do
  something like this?" when you don't know the exact name.

If a helper is close but missing one parameter, prefer extending it with an optional parameter rather than creating a near-duplicate.

---

## Pitfall 6: Not passing essential context through call stacks

Common misses:
- `guild_id` / `user_id` needed for i18n.
- `admin_override` flags for protected flows.
- `CONFIG.server_admin` (not a bare constant).

Quick check: when adding a new rule/flag, make sure it is threaded through all functions that need it.

---

## Pitfall 7: Overwriting unknown keys in CACHE dicts

Symptom: new feature fields “randomly disappear” after an update.

Fix: modify in-place, or if you must rebuild a dict, preserve unknown keys.

```python
user_data = CACHE.user_accounts.get(user_id, {})

# Preferred: in-place update
user_data.setdefault("notification_settings", {})["war_reminders"] = True

# If rebuilding, preserve unknown keys
ordered = {
    "display_name": user_data.get("display_name", "Unknown"),
    "notification_settings": user_data.get("notification_settings", {}),
    "players": user_data.get("players", []),
}
for k, v in user_data.items():
    if k not in ordered:
        ordered[k] = v
CACHE.user_accounts[user_id] = ordered
```

See also: ../qapbot/docs/CODE_STRUCTURE.md § Cache Management

---

## Pitfall 8: Thinking indicator without followup

Symptom: you `defer(thinking=True)` but only edit an existing message, so the user sees a “loading” indicator with no final response.

Fix:
- Use `thinking=False` if you only edit.
- Use `thinking=True` only if you will `followup.send()`.

```python
await interaction.response.defer(thinking=False, ephemeral=True)
await self.sent_message.edit(view=self)
```

---

## Pitfall 9: Direct DB access outside db_manager (runtime)

Rule: runtime modules must not import `sqlite3` directly.

Correct pattern:

```python
from qapbot.cache_manager import CACHE
rows = CACHE.db_manager.get_clan_attack_history_sync(clan_tag, month, year)
```

One-off scripts under `qapbot/scripts/` may use direct sqlite3 for offline validation/repair.

See also: ../qapbot/docs/DATABASE_ARCHITECTURE.md

---

## Pitfall 10: New DB column goes stale for up to 12 h after ALTER TABLE

Symptom: you add a column via `ALTER TABLE clans ADD COLUMN ...` but runtime code reads
`None`/empty for existing rows until the next scheduled API refresh (up to 12 h away).

Fix: add the field name to `_ESSENTIAL_CLAN_FIELDS` in `fetch_clan_war_data()`
(`QBhelperfunctions.py`). The essential-field bypass then forces an immediate API
backfill fetch for any clan_name_cache entry that is missing that field, without
waiting for the TTL cycle.

```python
# QBhelperfunctions.py — fetch_clan_war_data(), one place to change:
_ESSENTIAL_CLAN_FIELDS = ('war_league', 'your_new_field')  # <-- add here
```

---

## Pitfall 11: CommandTree.interaction_check used as a decorator

Symptom: global command guard (startup/maintenance) never fires; commands run regardless.

Root cause: `@bot.tree.interaction_check` CALLS `CommandTree.interaction_check(your_func)`
which returns a coroutine (truthy) and silently discards `your_func`. The check is never
registered.

Fix: use **direct attribute assignment**.

```python
# WRONG — func is silently discarded:
@bot.tree.interaction_check
async def my_check(interaction): ...

# CORRECT — check is actually registered:
async def my_check(interaction: discord.Interaction) -> bool:
    ...
bot.tree.interaction_check = my_check
```

See also: QBcore.py `_maintenance_interaction_check`

---

## Pitfall 12: Windows BSOD from excessive glob.glob() calls at startup (WdFilter.sys overload)

Symptom: Fatal system error / immediate reboot with no Python traceback during bot startup,
specifically during `load_all_temp_war_stats()`. Multiple different BSOD codes across
sessions (0x00000050, 0x00000139) — varying codes = memory corruption hitting random kernel
structures depending on timing. No Python traceback because crash is at kernel level.

Root cause: `load_all_temp_war_stats()` called `glob.glob("data/temp/*_war_data.json")`
once per tracked clan (1569 clans × same directory with 341 files) → ~535,000 filesystem
callbacks into `WdFilter.sys` (Windows Defender kernel minifilter) in a tight synchronous
loop → kernel data structure corruption → BSOD.

Why different BSOD codes: WdFilter.sys hits different kernel structures each run depending
on timing — classic memory corruption signature. This is NOT a NIC or network issue.

Why it appeared with CWL expansion: clan count grew from ~1385 to 1569, crossing the
threshold at which Defender's minifilter driver can no longer keep up with the callback rate.

The pattern to avoid:
```python
# WRONG — O(n_clans × n_files): calls glob.glob() once per clan on the same directory
for clan_tag in all_clan_tags:
    matches = glob.glob(f"data/temp/*{clan_tag}*_war_data.json")  # 1569 calls!

# CORRECT — O(1): single scan, then dict-lookup per clan
all_files = glob.glob("data/temp/*_war_data.json")  # 1 call
file_map = {}
for f in all_files:
    filename = os.path.basename(f)
    parts = filename.split("_")
    clan_tag = parts[0] if parts else None
    if clan_tag:
        file_map[clan_tag] = f
# Then per clan: filepath = file_map.get(clan_tag)
```

Diagnostic commands (elevated PowerShell):
```powershell
# Check for kernel crash events (Event ID 41 = unexpected reboot, 1001 = WER crash dump)
Get-WinEvent -LogName System -MaxEvents 100 | Where-Object { $_.Id -in @(41, 1001) } |
    Select-Object TimeCreated, Id, Message | Format-List

# Check which driver is in the crash dump (if minidump exists)
# %SystemRoot%\Minidump\ — open with WinDbg and run: !analyze -v
```

Fix (in code — already applied):
Single-scan pattern in `load_all_temp_war_stats()` (qapbot/cache_manager.py):
- Build `{clan_tag: filepath}` dict from one `glob.glob()` call
- Pass `json_file_path=filepath` directly to `_load_war_data_from_json()`
- Result: 1569 glob calls → 1 glob call (~1500× reduction in WdFilter callbacks)

Secondary hardening (host machine — run once in elevated PowerShell):
```powershell
# Add Defender exclusions to prevent future filesystem callback storms
Add-MpPreference -ExclusionPath "C:\python\QapBot\data"
Add-MpPreference -ExclusionPath "C:\python"
Add-MpPreference -ExclusionProcess "python.exe"
```

Code defences also in place in QapBot.py (independent of the M×N glob fix):
- asyncio.WindowsSelectorEventLoopPolicy() replaces ProactorEventLoop on Windows (general stability)
- asyncio.Semaphore(20) caps concurrent Phase-1 CoC API fetches (prevents API overload)
- coc_retry reconnect callback in coc_health.py handles unexpected session-close gracefully

Note: Intel I225-V NIC driver update and LSO v2 change were investigated as a hypothesis
during 2026-03-06 but were NOT the root cause — NIC changes did not stop the BSODs.
The M×N glob.glob() fix above was the real solution.

---

## Pitfall 13: sys.exit() inside an asyncio task hangs the process

Symptom: `sys.exit(42)` called inside `periodic_main()` task; bot hangs, Ctrl+C stops
working, `bot.run()` never returns.

Root cause: `sys.exit()` raises `SystemExit` which only terminates the *current task*.
The asyncio event loop keeps running; `bot.run()` blocks forever.

Fix: use `os._exit(code)` to terminate the entire process unconditionally.

```python
import os as _os
logging.info(f"Forcing process exit with code {code}")
_os._exit(code)  # bypasses asyncio entirely
```

Note: `os._exit()` skips `atexit` handlers and `finally` blocks — call it only AFTER
all cleanup (DB close, bot.close) has been awaited.

See also: QapBot.py `periodic_main()` exit-code block

See also: ../qapbot/docs/DATABASE_ARCHITECTURE.md § Migration Principles (Principle 4)

---

## Pitfall 14: Adding Phase-1 skip logic — check BOTH `track_war_updates` AND `is_deleted`

Symptom: A newly added "skip this clan" condition only checks `track_war_updates=False`,
missing the `is_deleted=True` gate, so deleted clans are still polled and generate errors.

Root cause: The Phase-1 build loop in `QapBot.py` has TWO separate early-continue guards:
1. `if not clan_data.get('track_war_updates'): continue`
2. `if clan_data.get('is_deleted'): continue`

They are independent. A clan can have `track_war_updates=True` AND `is_deleted=True`
(e.g. subscribed clans are always track=True, but can still be deleted by Supercell).

Fix: whenever you add or modify Phase-1 loop filtering, preserve both guards in order:
```python
# 1. Skip passive clans (below M3, no subscriptions)
if not clan_data.get('track_war_updates'):
    continue
# 2. Skip Supercell-deleted clans (detected via 404 NotFound; auto-restored on any success)
if clan_data.get('is_deleted'):
    continue
```

The `is_deleted` flag is set by `_mark_clan_deleted()` in QBhelperfunctions.py and cleared
automatically by `_update_clan_metadata()` in qapbot/coc_cache.py on any successful
`GET /clans/{tag}`. Do NOT add `is_deleted` to `_ESSENTIAL_CLAN_FIELDS`.

See also: ../qapbot/docs/CLAN_WAR_TRACKING.md § Clan Deletion Detection

## Pitfall 15: `bot.get_channel()` returning `None` does NOT mean the channel was deleted

Symptom: Code treats `bot.get_channel(channel_id) is None` as "channel was deleted" and purges
related CACHE entries (e.g. tracked message IDs). This can fire on a transient gateway/cache gap
even though the channel still exists, silently dropping the tracked message ID — the bot then
posts a brand-new message next time instead of recognizing the old one is still there, producing
duplicate messages (e.g. registration messages appearing a 2nd time). Root-caused in
`handle_cleanup_messages_all()` in qapbot/QBdiscocmdshelper_admin_command.py (fixed 2026-08-03).

Root cause: `Client.get_channel()` is a **local cache lookup only** — it never calls the Discord
API. `None` can mean the channel was deleted, but can equally mean a cache gap (bot restart race,
gateway hiccup, or in dev mode: a prod-guild channel the dev bot was never in).

Fix: never treat a `get_channel()` miss as confirmed deletion. Fall back to an actual API call and
gate on the specific exception:
```python
channel = bot.get_channel(channel_id)
if not channel:
    try:
        channel = await bot.fetch_channel(channel_id)
    except discord.NotFound:
        # Discord API confirms the channel no longer exists — safe to treat as deleted.
        ...purge cache entries...
    except Exception:
        # Forbidden (no access), HTTPException (API error/rate limit), network errors, etc.
        # None of these confirm deletion — leave the cache untouched and retry next run.
        ...skip, log, don't purge...
```
Only `discord.NotFound` (HTTP 404, "Invalid Channel ID") confirms deletion. `discord.Forbidden`
(HTTP 403, e.g. bot not in that guild — relevant for the dev/prod split) and any other
`HTTPException`/network error are inconclusive and must not trigger a purge.

Sibling pattern — "delete old message, then unconditionally post new one": the same duplicate
risk shows up without `get_channel()` at all. `repost_playerregistration_messages()` in QapBot.py
(runs every main loop cycle) deleted the tracked message inside a blanket `except Exception: pass`
and then *always* proceeded to post a replacement, regardless of whether the delete actually
happened. A transient failure (rate limit, network blip, permissions) left the old message alive
while a new one was created on top of it. Same fix shape: only treat the delete as done on
`discord.NotFound` (or a confirmed-gone channel); any other exception must skip posting/clearing
tracking this cycle rather than risk a duplicate. Fixed 2026-08-03.

See also: qapbot/QBdiscocmdshelper_admin_command.py `handle_cleanup_messages_all()`,
QapBot.py `repost_playerregistration_messages()` / `cleanup_stale_ui_messages()`

---

## Pitfall 16: `asyncio.to_thread()` does NOT make an atomic C call non-blocking

Symptom: a slow C-level call (`gc.collect()`, hashlib, most C-accelerated stdlib) is wrapped in
`asyncio.to_thread()` expecting it to stop blocking the event loop, but the bot still freezes for
its full duration.

Root cause: CPython only releases the GIL / checks the "eval breaker" at Python bytecode-execution
boundaries. A single atomic C-level call does not hit such a boundary mid-call, so running it on a
worker thread only avoids blocking *other* code that happens to run on other worker threads (rare
in this bot) — it does not free the main event loop.

Fix: reduce the actual scope/cost of the C call instead of wrapping it. Example (2026-07-18):
`_post_cycle_cleanup()`'s full `gc.collect()` (gen-2, ~1s+) blocked the whole bot despite running
via `asyncio.to_thread()`; fixed with `gc.collect(1)` (gen-0+1 only, skips the huge long-lived
`CACHE` object graph in gen-2).

By contrast, a plain **Python-level loop** (dict iteration, string ops) DOES get preempted at
bytecode boundaries, so for that case both `await asyncio.sleep(0)` every N iterations, or
`asyncio.to_thread()`-wrapping the whole loop, are valid fixes. Example: the clan-categorization
loop in `main()` (QapBot.py) iterating `CACHE.clan_name_cache` (~380K entries in PROD) cost
~4.1-4.2s of frozen event-loop time every cycle; fixed with `await asyncio.sleep(0)` every 2000
iterations (simpler than `to_thread()`-wrapping the whole loop).

Second example, the *inverse* mistake (2026-08-17, PROD): `db_manager.py`'s
`_backfill_player_name_search_if_needed()` ran three sequential Python-level list comprehensions
over `player_name_index` (6.6M rows on PROD) directly inline in its `async def` body — no
`to_thread()`, no `sleep(0)`, nothing — including one that called `hashlib.blake2b()` once per
row. Unlike the `gc.collect()` case above, this WAS ordinary Python bytecode (dict/tuple
construction, a hash call per iteration), so it would have been fully fixed by
`asyncio.to_thread()`-wrapping the whole thing — the bug was simply never wrapping it at all, not
a to_thread-doesn't-help case. Missed because this code runs once, at startup/rare schema
migrations, a code path with much less scrutiny than the hot per-cycle loops this pitfall is
usually hunted in — froze the event loop long enough (multiple minutes at 6.6M rows) to miss a
Discord gateway heartbeat and force a reconnect. Fix: moved the whole row-fetch +
transform + bulk-write to a sync method run via `asyncio.to_thread()` on the sync connection,
matching the pattern already used by `flush_pending_war_writes` elsewhere in the same file.
Lesson: this pitfall applies just as much to one-time startup/migration code as to per-cycle
hot paths — "runs once" is not the same as "runs fast," and at PROD's row counts it wasn't.

Follow-up (still 2026-08-17): the `to_thread()` fix stopped the event-loop freeze, but the
`hashlib.blake2b()` call itself was still the wrong design — 6.6M individual Python-level hash
calls measured 355.25s even off the event loop, and PROD's much weaker CPU never finished it at
all inside a 30-minute startup safety timeout (see Pitfall 29). Replaced the per-row hash with a
single bulk `SELECT ... rowid FROM player_name_search` — 82.42s for the same dataset, 4.3x
faster. Lesson on top of the lesson: `asyncio.to_thread()` fixes the *event-loop-blocking*
problem, but moving slow work off the loop doesn't make it fast — if a per-row operation inside
a `to_thread()`-wrapped loop can instead be one bulk SQL query, prefer the query.

Diagnostic tool: `qapbot/scripts/log_time_gaps.py --log data/logs/qapbot.log --top N` finds the
biggest gaps between consecutive timestamped log lines — the fastest way to find blocking/slow
segments in the update cycle. On Windows, pipe through
`$env:PYTHONIOENCODING="utf-8"; ... | Out-File -Encoding utf8` (emoji in log lines crash the
default cp1252 console encoding otherwise). Exclude "Sleeping for" and nightly-maintenance
(DB-MAINT/REINDEX/VACUUM) lines as expected noise. A large `--top` (300-400) that shows the SAME
gap recurring on every cycle is the signal of a systemic blocking issue, vs. a one-off external
`[COC-API-SLOW]` response (not our bug).

---

## Pitfall 17: `_ensure_clan_exists()` / similar FK-integrity helpers called on a non-FK `clan_tag` column

Symptom: a `/whois player` (or similar) report silently inserts a bogus placeholder row
(`name='Unknown'`) into `clans` for a **player** tag, because some column named `clan_tag` by
convention doesn't actually hold a clan tag in that row.

Root cause: `leaderboard_messages.clan_tag` has no FK constraint — it's intentionally reused to
store clan tags, family tags, or (for `mode == "whois_player"`) a player tag. Calling
`_ensure_clan_exists(clan_tag)` unconditionally whenever the column is truthy can't tell which
kind of tag it holds.

Fix: before calling `_ensure_clan_exists()`/similar FK-integrity helpers on any `clan_tag`-named
column, check the `CREATE TABLE` for an actual `FOREIGN KEY ... REFERENCES clans`. Several columns
are named `clan_tag` by convention but deliberately hold non-clan tags too (`subscriptions`,
`leaderboard_messages` both have "Note: No FK constraint" schema comments).

Self-healing companion (added alongside the 2026-07-18 fix): `db_manager.is_clan_tag_referenced()`
/ `delete_clan_if_unreferenced()` (+ `CACHE.purge_clan_if_orphaned()` wrapper) hard-delete a
`clans` row once `_mark_clan_deleted()` confirms it's gone via CoC API 404, but only if no other
table still references it. Since 2026-08-08 the referencing tables live in one module-level
registry, `CLAN_TAG_REFERENCING_TABLES` in db_manager.py — **any new table with a real `clans`
FK (or a non-FK column that conceptually depends on a clan being real) must be added there**,
or the orphan purge can cascade-delete rows that table still needs.
tests/unit/test_clan_tag_reference_registry.py asserts every `REFERENCES clans` clause in the
live DDL has a registry entry, so forgetting one is a test failure, not silent data loss.
(The registry conversion itself caught a real instance: `guild_welcome_clans` has a
`REFERENCES clans ... ON DELETE CASCADE` FK but was missing from the old hand-enumerated list.)

Related same-day gotcha: `is_clan_tag_referenced()` initially checked `leaderboard_messages.clan_tag`
unconditionally — the exact overloaded-column trap above — which circularly "protected" the bogus
`whois_player` rows the check should have deleted (2 of 9 PROD candidates wrongly kept). Fix:
exclude `mode = 'whois_player'` rows (`AND mode != 'whois_player'`). General lesson: when a column
is deliberately overloaded to store different tag *kinds* behind a `mode`/`type` discriminator,
any "is this tag referenced" check MUST filter by that discriminator, not just match the tag value.

One-off cleanup script for existing bad PROD rows: `qapbot/scripts/cleanup_orphaned_clans.py`
(dry-run by default; candidates = `name='Unknown' AND last_checked_via_api IS NULL`).

---

## Pitfall 18: `normalize_clan_tag()` over-matches plain player names as "valid tags"

Symptom: a user types a plain player name (no leading `#`) into a search field, and it gets
misinterpreted as a raw clan/player tag, producing a bogus "tag not found" error instead of
running a name search.

Root cause: `normalize_clan_tag()`'s regex (5-10 alphanumeric chars after `#`,
QBdiscocmdshelper.py) can't distinguish a real CoC tag typed without `#` from a short all-letter
player name (e.g. "Killer" → 6 letters → passes the regex).

Fix pattern (2026-07-25): only prioritize the direct-tag interpretation when the user input has an
explicit leading `#` (unambiguous intent). Otherwise try the name search first and only fall back
to raw-tag interpretation if that search finds nothing. Confirmed instance:
`PlayerSubstringModal.on_submit()` (qapbot/ui_registration.py). Check for this same ordering bug
anywhere else `normalize_clan_tag()` is used as a first-resort classifier on freeform user text.

---

## Pitfall 19: Auditing `member_clans`/`member_families` coverage checks for family-blind bugs

Symptom: a guild configured with ONLY a clan family (no individual clans listed) silently loses
coverage for some feature — e.g. role syncs never trigger for its members.

Root cause: guild config stores clan coverage as TWO separate lists: `member_clans` (individual
tags) and `member_families` (family IDs, each expanding to a `clans` list via
`CACHE.clan_families`). Any "is this clan covered by this guild" check that only inspects
`member_clans` misses family-only guilds entirely.

Fix: grep for `.get("member_clans"` / `.get("member_families")` after any change in this area —
every coverage check MUST expand `member_families` too. Confirmed-correct reference
implementations: `get_guild_clans_including_member_config()`, `is_player_in_member_clans()`
(QBdiscocmdshelper.py), `guild_role_manager.sync_roles_for_user()`, `cache_manager._is_clan_tracked()`.

---

## Pitfall 20: `bot.add_view()` dispatch is live before `CACHE`/DB finish loading — false "no data" reports AND real data loss

Symptom: a button click on a persistent view immediately after a bot restart reports "you have no
X" (no linked accounts, no registered players, etc.) for a user/guild whose data was intact in the
DB before the click. Worse (confirmed in prod 2026-08-08): the click itself can DESTROY that data —
the reporter's 5 linked accounts were hard-deleted from `user_players` by nothing more than
clicking "My Accounts" during the startup window.

Root cause — two stacked problems:
1. **Dispatch-before-load**: `bot.add_view(view)` (called in `_setup_hook()`, which runs BEFORE
   the gateway connects) registers a view's `custom_id`s for interaction dispatch immediately —
   independent of `on_ready()`, which is where `CACHE` actually gets populated from the DB and
   `QBcore.bot.fully_initialized` is finally set `True` (Step 9, near the end). Component
   (button/select) interactions are NOT covered by the `CommandTree.interaction_check` guard that
   blocks slash commands until `fully_initialized` — that only fires for slash commands/context
   menus. So a click in the gap reads an empty, not-yet-loaded `CACHE`.
2. **Read-path click triggers a cache-blind write**: every registration button starts with
   `update_user_metadata_from_interaction()` → `CACHE.update_user_metadata()`, which (pre-guard)
   would CREATE a skeleton entry `{"display_name": ..., "players": []}` for any user missing from
   the cache and write-through persist it. `save_user()`/`_save_user_impl()` implements persistence
   as `DELETE FROM user_players WHERE discord_id = ?` + reinsert-from-payload — so persisting that
   fabricated `players=[]` skeleton hard-deleted every real `user_players` row for that user. The
   "no linked accounts" message wasn't just a stale read; it was the after-image of the wipe.

Fixes (all three layers required, defense in depth):
1. **View-level gate**: any `discord.ui.View` registered via `bot.add_view()` must override
   `interaction_check()` to check `QBcore.bot.fully_initialized` first, responding with
   `t('commands.errors.startup_in_progress', ...)` (ephemeral) and returning `False` if not ready —
   then delegate to `await super().interaction_check(interaction)` to preserve the maintenance-mode
   guard (monkey-patched onto `discord.ui.View` in QBcore.py). Reference:
   `RegistrationView.interaction_check()` (qapbot/ui_registration.py). NOT automatic for future
   `add_view()`-registered views (e.g. planned CWL hubs) — each needs the override.
2. **Cache-level gate** (`CacheManager.users_loaded`, qapbot/cache_manager.py): `False` until
   `load_user_accounts()` succeeds. While `False`: `update_user_metadata()` skips entirely (logs
   `[STARTUP-GUARD]`, never fabricates a skeleton), and `set_user_account()` / `persist_user()` /
   `delete_user_account()` raise `RuntimeError` instead of writing — no user write-through can
   ever run against an unloaded cache, regardless of which view/command forgot layer 1. Class-level
   default is `False` so even `__new__`-created instances fail safe.
3. **DB-level forensics**: `_save_user_impl()` logs a WARNING whenever a save replaces N>0 existing
   `user_players` rows with an EMPTY players list (legitimate only for an explicit last-account
   unlink) — so any future wipe is visible in the log at the moment it happens.

Test coverage: tests/discord/test_ui_registration.py (view gate),
tests/integration/test_cache_manager.py `TestUsersLoadedDataLossGuard` (cache gate, including a
reproduction of the exact incident path asserting the DB rows survive).
Found-and-fixed instance (2026-07-25): `coc_cache.py`'s role-sync trigger only checked
`member_clans`, so family-only guilds never got CoC-role/clan-role syncs triggered on
clan-member cache updates.

---

## Pitfall 21: Automatic (implicit) `gc` gen-2 sweeps — Pitfall 16's fix only covers the *explicit* call

Symptom (2026-08-08): an admin's `/admin` slash command got "The application did not respond" —
Discord's interaction token expired before the bot's `defer()` call reached it. `qapbot_PROD.log`
showed the command's `[CMD]` line landing *after* the 3s window, and — more tellingly — a burst of
~20 concurrently in-flight `[COC-API-SLOW] get_clan(...)` log lines all reporting **the same**
elapsed time (7.8–8.7s) within milliseconds of each other, immediately preceded by a 7.8s stretch
of the log with **zero output at all** (no new `[COC-API-CALL]` dispatches, no `[WAR-LEAGUE-UPDATE]`
lines — nothing). Independent concurrent HTTPS calls to 20 different endpoints do not finish in
near-perfect sync; a shared atomic pause that blocks the entire process and releases everything at
once when it ends does. Running `qapbot/scripts/log_time_gaps.py --top 400` over the full day
confirmed this is systemic, not one-off: dozens of multi-second gaps recur throughout the day at
*different* points in the cycle each time (inside Phase-1 fetch bursts, inside the
`[NOTIFY-TIMING]` war-notification loop, right after `[CATEGORIZE-TIMING]`), which is the signature
of an allocation-threshold-triggered pause, not a fixed code path.

Root cause: Pitfall 16's fix (`_post_cycle_cleanup()` using `gc.collect(1)` instead of a full
`gc.collect()`) only scopes the bot's own **explicit** end-of-cycle call. It deliberately leaves
CPython's **automatic** generational collector enabled at default thresholds — see the comment
above `_post_cycle_cleanup()` in QapBot.py, which already flagged this as an accepted gap: "Python's
automatic generational GC ... continues to run real gen-2 sweeps on its own schedule". An automatic
gen-2 sweep is triggered by allocation counters, not by any point in our code, so it can fire in the
middle of Phase-1's concurrent API-response object churn (thousands of `coc.Clan`/`coc.War` objects
created and freed). Being a single atomic C call (Pitfall 16), it holds the GIL for its whole
duration — freezing every thread, including the event loop and the Discord gateway heartbeat — and
because it's a *full* sweep it walks the entire long-lived `CACHE` graph (in PROD: ~420K
`clan_name_cache` entries, ~6.4M `player_name_index` entries, subscriptions, user accounts, ...),
which is what stretches it to multiple seconds.

Fix (both applied near the top of QapBot.py / in the Step-3 cache-load block):
1. **Make future pauses visible**: register a `gc.callbacks` start/stop logger at import time that
   logs `[GC-AUTO] Automatic gen-N collection paused the process for X.XXXs` for any automatic
   collection ≥0.5s. Without this, the failure mode is invisible — it masquerades as CoC API
   latency, exactly as it did in this incident, and gets misdiagnosed as "not our bug" per the
   Pitfall-16 diagnostic note above.
2. **Shrink what gets swept**: after `CACHE.load_all()` completes at startup, run one full
   `gc.collect()` (clean up startup-only garbage) then `gc.freeze()` — this moves everything
   currently tracked into gc's permanent generation, which automatic collections never scan. Future
   automatic sweeps then only walk genuinely new (post-startup) object churn — a small, bounded
   working set — instead of the entire multi-million-object CACHE baseline. Both are atomic C calls
   themselves, so they're `asyncio.to_thread()`-wrapped purely to keep the Discord heartbeat task
   schedulable while they run (Pitfall 16: this does NOT make them non-blocking, it's a one-time
   startup cost before real traffic/Phase-1 begins).

Caveat: `gc.freeze()` permanently excludes the frozen objects from cyclic collection. If a genuine
reference cycle later forms between a frozen (baseline) object and a new object, that cycle will
never be collected. Acceptable here because `CACHE`'s frozen contents are plain dicts/lists/strings
(no `__del__`, no custom classes prone to cycles) — the classic reference-cycle risk (e.g. `discord`
objects, `aiohttp` responses) is all *post-freeze* churn, still tracked and collected normally.

Diagnostic tool: same as Pitfall 16 — `qapbot/scripts/log_time_gaps.py --top 300-400`, looking for
`[COC-API-SLOW]`/`[NOTIFY-TIMING]`/`[CATEGORIZE-TIMING]` gaps that recur at *varying* positions
across cycles (vs. a fixed line, which would point to a specific blocking call instead).

**Follow-up 1 (2026-08-08, "Issue 3" — same day, undocumented until now)**: the startup freeze
alone wasn't enough — PROD's automatic `[GC-AUTO]` pauses kept recurring over the following days.
Every update cycle promotes newly-created long-lived `CACHE` growth (new clans, new war metadata —
substantial during a CWL season, e.g. 7000+ active wars) into gen-2, which the one-time startup
freeze never covers, so the swept-but-unfrozen working set grows daily. Fix: `run_nightly_
maintenance_routine()` (`QapBot.py`) now ends with `gc.unfreeze()` + a real full `gc.collect()`
(catches genuine reference cycles the per-cycle `gc.collect(1)` always skips — those would become
permanent floating garbage once re-frozen otherwise) + `gc.freeze()` again, folding each day's
legitimate growth back into the permanent generation before tomorrow's automatic sweeps have to
walk it. Runs during the maintenance window (`db_maintenance_mode=True`, Discord commands already
blocked) since a real full collect costs the same multi-second price the per-cycle scoping exists
to avoid during live cycles — logged as `[NIGHTLY-MAINTENANCE] GC refresh: ...`. Never re-freezes
mid-cycle or per-cycle — only nightly, after a full collect, per the caveat above (a cycle formed
between a frozen baseline object and new post-freeze churn would never collect otherwise, so
re-freezing has to follow a real sweep, not run on its own schedule).

**Follow-up 2 (2026-08-17, CWL_PROD_PERFORMANCE_FIX_PLAN.md P2 Step 10)**: raised the gen-2
threshold multiplier from the default 10 to 20 — `gc.set_threshold(700, 10, 20)`, set once at
startup right before the freeze above (gen-0/gen-1 left at their defaults; only gen-2's own
allocation-threshold-triggered frequency changes). Halves how often an automatic gen-2 sweep fires
at all, on top of (not instead of) the freeze shrinking what each sweep has to walk when it does
fire — pure mitigation, doesn't address any allocation source (the 2026-08-16 PROD meltdown's
actual source — an unbounded CWL guest-search scan — was removed at the source by P0 of that same
plan).

---

## Pitfall 22: `QapBot.py`'s top-level code runs TWICE in one process — `QBdiscordcmds.py` imports back from it

Symptom (2026-08-08): every `[GC-AUTO]` log line appeared as an exact near-duplicate pair —
identical `collected=`/`uncollectable=` values, timestamps a few milliseconds apart, durations
differing only in the 3rd decimal. Looked at first like the automatic collector itself was
double-firing; it wasn't — the *logger* was registered twice.

Root cause: `QBdiscordcmds.py` has a module-level `from QapBot import GLOBAL_GUILD_ID,
run_nightly_maintenance_routine, is_monthly_migration_due`. `QapBot.py` is run as `python
QapBot.py`, so Python loads it into `sys.modules['__main__']` — there is no `sys.modules['QapBot']`
entry. When execution reaches (directly or transitively) an import of `QBdiscordcmds`, that
module's `from QapBot import ...` line doesn't find a `'QapBot'` key in `sys.modules`, so Python
does NOT recognize the running script as already loaded — it opens `QapBot.py` again and executes
**every top-level statement in the file a second time**, this time under the module name
`"QapBot"`, completely independent of the `__main__` execution already in progress.

Why this doesn't start a second bot: the actual `bot.run()` call is correctly gated behind `if
__name__ == "__main__":` at the bottom of the file, and the second execution's `__name__` is
`"QapBot"`, not `"__main__"`, so that block is skipped there. `@QBcore.bot.event` decorators
(`on_ready`, `on_disconnect`, etc.) are also safe by accident: `discord.Client.event()` just does
`setattr(bot, coro.__name__, coro)`, so re-running the decorator on the second execution simply
overwrites the attribute with an identical redefinition — not a second registration.

What ISN'T safe: any module-level statement that **mutates a shared, already-existing collection**
instead of plain `def`/`class`/`import`/assignment-to-a-fresh-name. `gc.callbacks.append(fn)` is
exactly that — each execution creates a *new* closure object and appends it, so `gc.callbacks` ends
up with two separate entries that both fire on every real collection event. (`logging.basicConfig()`
happens to be safe too, but only because it's documented to no-op if the root logger already has
handlers — not because module-level code is inherently safe to duplicate.)

Fix applied: a name-based (not identity-based) dedup guard before the `append()` — identity
(`is`/`in` on the function object) can't catch this, since the second execution's function object
is a different object with the same `__name__`:
```python
if not any(getattr(cb, "__name__", None) == "_log_slow_gc" for cb in gc.callbacks):
    gc.callbacks.append(_log_slow_gc)
```

Not fixed (deliberately, out of scope for the immediate bug): the circular import itself.
`GLOBAL_GUILD_ID`/`run_nightly_maintenance_routine`/`is_monthly_migration_due` would need to move
to a neutral module (e.g. `QBcore.py` or a new small module) both files can import from, breaking
the `QapBot.py` → `QBdiscordcmds.py` → `QapBot.py` cycle — a real fix, but a bigger one that needs
its own review of every consumer.

Rule going forward: any new module-level code added near the top of `QapBot.py` that does more than
`def`/`class`/`import`/plain assignment (registers a callback, starts a task, opens a resource,
appends to a shared list) needs a dedup guard exactly like the one above, or it will silently
double up the same way. Check with `python -c "import ast; ..."` or simply grep
`^[A-Za-z_].*\.(append|add|register)\(` at column 0 in `QapBot.py` to audit for more of these.

## Pitfall 23: RTL (Arabic/Hebrew) names in a Discord embed line scramble the WHOLE line, not just the name

Symptom (2026-08-08): `/analyse league_group` embed rows mixing an Arabic clan/player name with other
fields on the same line (`rank · player · clan → ⭐ total (atk info)`) displayed with the name's
relative position swapped with the clan name, AND the trailing `→ ⭐ total` segment visually jumped to
*before* the names — while the row's raw/logical text (confirmed via copy-paste out of Discord) was
always byte-for-byte correct. Only the visual rendering was wrong.

Root cause: Discord's client merges an RTL name with every following character up to the next
unambiguous Latin-script text into ONE contiguous bidi run, and mirrors that whole span. In a line like
`rank+TH  player · clan  → ⭐ 15  (5 atk...)`, everything from the RTL name through `→ ⭐ 15` gets pulled
into that run — the mirror only stops at `(5 atk...)` because "atk" is unambiguous Latin text.

Two plausible-looking fixes were tried and BOTH had zero effect, live-tested on Discord desktop and iOS:
- **Unicode bidi isolates** (FSI `U+2068` / PDI `U+2069`) wrapped around the name. Theoretically the
  textbook-correct fix (isolates make a run opaque to the surrounding context) — but Discord's client
  does not appear to implement/respect isolates at all. (Independent corroborating signal: even the
  `python-bidi` reference library throws `AssertionError: FSI not allowed here` — isolates are a
  newer, less universally-supported bidi feature.)
- **A single leading LRM** (`U+200E`) at the very start of the line. Anchors the START of the line to
  LTR, but does nothing to stop content *after* the RTL name from being absorbed into its run — the
  scramble was unaffected.

Fix that actually worked: **plain LRM, applied twice per name** — once bracketing the name itself
(already done by `normalize_player_name()` in `qapbot/formatting.py`: `f"{LRM}{name}{LRM}"`), AND
again as a bare, non-bracketed LRM placed directly in the line's literal separator text at EVERY
transition out of the name back into the rest of the line (before ` · `, before ` → `, etc.) — not
just at the line's start. This matches an already-proven-working pattern elsewhere in this codebase,
the CWL round-lines "vs. opponent" builder in `QBhelperfunctions.py`:
```python
f"vs. [{LRM}{opp_name}{LRM}]({opp_url})  {LRM}`{opp_tag}`{opp_league_str}"
```
Applied identically to the CWL group analyse row builder (also `QBhelperfunctions.py`):
```python
f"{LRM}{_rank_label(rank)}{_th_emoji(th)} {player_link}"
f"{LRM} · {clan_link}"
f"{LRM} → ⭐ **{total_stars_val}** *({total_atk} atk · {pos_note})*"
```

Rule going forward: any new Discord text (embed description, message content) that mixes a
user-supplied RTL name with OTHER fields on the same line needs a bare LRM at every field-transition
boundary after the name, not just marks wrapped around the name. Isolates (FSI/PDI) do not work on
Discord — don't reach for them here even though they're the "more correct" Unicode mechanism in a
spec-compliant renderer. When in doubt, replicate the two reference implementations above exactly.

## Pitfall 24: `generate_cwl_group_analysis_embeds()` (`/analyse league_group`) silently dropped wars fetched live from the CoC API

Symptom (2026-08-09): running `/analyse league_group` back-to-back on DEV and PROD, for the exact same
CWL group and season, on databases confirmed byte-identical (a fresh PROD→DEV backup had just been
taken), gave *consistently* different leaderboards — PROD reported 24 of 28 group wars had attack data,
DEV consistently reported only 20, every time, across multiple restarts. Not a one-off flake: repeated
runs on each side kept landing on their own number.

Root cause (function bypasses the DB — see below — so "identical DB" was a red herring): the function
fetches the CWL group and every one of its round wars **live** from the Clash of Clans API on every
invocation (`CACHE.get_league_group()` + `CACHE.get_league_war()` per war tag), and any per-war fetch
that came back without attack data was silently dropped:
```python
wars = [w for w in war_futures if not isinstance(w, Exception) and w is not None]
```
No logging (`coc.NotFound` wasn't logged at any level — see `coc_retry()` in `qapbot/coc_health.py`),
no retry, no indication in the output that data was incomplete. DEV and PROD log in via *separate*
Clash of Clans developer accounts (`COC_API_EMAIL_DEV` vs `COC_API_EMAIL`, see `qapbot/config.py`).

Fix applied: `generate_cwl_group_analysis_embeds()` now (1) logs every failed/empty per-war fetch, (2)
retries any war that came back without attacks exactly once, forcing a real cache bypass via
`CACHE.get_league_war(wt, force_refresh=True)` — needed because `get_league_war()` normally treats a
`warEnded` result as immutable and caches it permanently for the TTL, so a plain retry would just be
served the same incomplete snapshot again, and (3) for any war still missing attack data after the
retry, falls back to this bot's own locally-recorded `war_attacks`/`war_summary` rows for that specific
war tag (`_load_cwl_wars_from_db_sync()`, keyed by `war_tag` rather than by clan+season like the
whole-command DB fallback) — this also recovers *partial* data for a war that's still in progress,
since the periodic tracker keeps those rows updated throughout the war, not just after it ends. If a
war is still unrecovered after all three steps, its count is surfaced in the embed itself
(`⚠️ N war(s) could not be verified live or in local records`) instead of just silently under-counting.

After this fix, DEV and PROD converged on the *correct* number (24/28 — this group was mid-round-6-of-7,
so round 7 legitimately had no data yet on either side; the embed's warning about those 4 wars is
expected behaviour, not a bug, when a season is still in progress). One real DEV-only gap remained
though: 4 already-*played* war tags occasionally came back as `coc.PrivateWarLog: accessDenied (403)`
on DEV, while PROD fetched the exact same tags cleanly every time.

`coc.PrivateWarLog` is a misnomer for this call path — verified directly in the installed `coc.py`
source, not just inferred. `Client.get_league_war()` does:
```python
# coc/client.py:1134-1137
try:
    data = await self.http.get_cwl_wars(war_tag, **{**self._defaults, **kwargs})
except Forbidden as exception:
    raise PrivateWarLog(exception.response, exception.reason) from exception
```
— it blindly relabels **any** 403 from that endpoint as `PrivateWarLog`, with no check on the actual
reason and no clan-privacy lookup anywhere in the path. The name is inherited from a *different* method
(`get_current_war()`, which genuinely does gate on a clan's `isWarLogPublic` flag) — for
`get_league_war()` it just means "got a 403 from `/clanwarleagues/wars/{warTag}`", nothing more. CWL
league-war-by-tag lookups are not supposed to be gated by clan warlog privacy at all, and that setting
is clan-level game data — identical regardless of which account queries it — so this was never a real
privacy difference between DEV and PROD.

That also rules out the tidier "stale API key registered under a different IP" theory that seemed
promising at first: `coc.py`'s low-level `request()` already special-cases exactly that scenario and
self-heals it *before* it can surface as an exception —
```python
# coc/http.py:362-369
if response.status == 403:
    if data.get("reason") == "accessDenied.invalidIp" and self.email and self.password:
        await self.initialise_keys()          # re-provision keys...
        return await self.request(route, **kwargs)   # ...then silently retry
    raise Forbidden(response, data)
```
The exception we actually logged was `PrivateWarLog: accessDenied (status code: 403)` — reason is bare
`"accessDenied"`, not `"accessDenied.invalidIp"`. Had it been the IP-mismatch case, the library would
have reprovisioned and retried internally and we'd never have seen an exception at all. So whatever
Supercell is rejecting this specific account's request for is a different condition than a mismatched
key IP.

**Root cause, confirmed**: `raise PrivateWarLog(exception.response, exception.reason) from exception`
drops the original `Forbidden`'s `.message` field (only `.reason` survives the re-wrap) — but the
original `Forbidden` is still reachable as `__cause__` (preserved by that `from exception`), and it
still has `.message` intact. Added temporary diagnostic logging in `coc_retry()`
(`qapbot/coc_health.py`, scoped to `operation_name.startswith("get_league_war")` so it doesn't add noise
to the routine, genuinely-expected `PrivateWarLog` hits on regular `get_current_war()` polling elsewhere)
to surface it. First DEV run with that logging live:
```
cause_message='Invalid authorization'
```
That's decisive: the bearer token itself was rejected by Supercell — a broken API key, not a privacy
setting, not IP mismatch, not eventual consistency. `HTTPClient.initialise_keys()` (`coc/http.py:551-561`)
only validates a candidate key's **name + registered CIDR** against the `/apikey/list` response before
reusing it — it never test-calls the key. So if one of a developer account's registered keys is broken
on Supercell's auth side while still listing normally in the developer portal, `initialise_keys()` will
silently keep including that same dead key in the rotation on every restart — and since
`BatchThrottler` cycles through the key list in a stable order, and `all_war_tags` is dispatched in a
stable order too (from `league_group.rounds`), the same position in every request batch keeps landing
on the same broken key. That's exactly why the identical set of war tags failed identically across five
independent DEV restarts spanning over an hour (07:49, 07:54, 08:02, 08:24, 08:58) — not a timing
coincidence, a structurally-guaranteed repeat. PROD's log showed zero `PrivateWarLog` hits the entire
time, so only DEV's account has a bad key.

**Fix was account hygiene, not code**: deleted the keys named `"Created with coc.py Client"`
(`coc.py`'s default — nothing in this repo overrides it, so every script under `qapbot/scripts/` that
logs into the same account shares this key name) from the DEV account (`COC_API_EMAIL_DEV`) on
developer.clashofclans.com. On the next DEV login, `initialise_keys()` found 0 matching keys and
created a fresh set of `key_count=10`, all valid — confirmed clean afterward.

**Status: resolved (2026-08-09).** Follow-up hardening once the cause was confirmed:
- **Retry no longer wastes a call on this case**: `generate_cwl_group_analysis_embeds()`
  (`QBhelperfunctions.py`) now separates `coc.PrivateWarLog` failures out of the general retry pool —
  a broken/revoked key is an auth-level rejection that a cache-bypass retry within the same session
  cannot fix, so those tags skip straight to the local-DB-fallback step instead of burning a second,
  guaranteed-to-fail API call.
- **Proactive detection at startup**: new `_validate_coc_api_keys()` (`QapBot.py`), fired as a
  fire-and-forget background task right after `coc_client.login()`, test-calls each of the `key_count`
  provisioned keys directly (bypassing the round-robin) against the cheap `/locations` endpoint. Any
  key that fails logs `[COC-KEY-SANITY] CRITICAL` with its 1-indexed position, **and DMs the configured
  `SERVER_ADMIN`** via `CACHE.send_user_dm()` (a log line alone had already gone unnoticed for over an
  hour across 5 DEV restarts before this incident was traced) — so a future dead key is caught before
  it ever reaches a user-facing command, on both DEV and PROD. Doesn't auto-fix anything; the fix is
  still manual account cleanup as above.
- **Temporary diagnostics reverted**: the verbose `[COC-PRIVATEWARLOG-DIAG]` block in `coc_retry()`
  (`qapbot/coc_health.py`) and the DEV-only `coc.http` logger `INFO` override (`QapBot.py`) are both
  back to their original state — the startup guard above is the permanent replacement for "notice a
  dead key," so the reactive per-call diagnostic logging that did its job here is no longer needed.

Rule going forward: any code path that fans out `asyncio.gather(..., return_exceptions=True)` over
several independent live API calls and then filters `if not isinstance(w, Exception)` needs to also
log what got filtered out and decide a recovery strategy — silently discarding a subset of a batch
fetch is very easy to miss in testing (a single run looks fine; it's only "the same query, run twice,
gives two different answers" that exposes it). Don't trust an exception's *name* over what its raise
site actually checks: `coc.PrivateWarLog` reads like a clan-privacy issue, and IS one for
`get_current_war()` — but for `get_league_war()` it's raised unconditionally for any 403, so treating it
as "expected, no big deal" without reading the raise site first would have masked a real broken API key
here. When a library's `raise X(...) from original_exception` re-wrap looks like it dropped useful
detail, check `__cause__` before assuming the detail is gone — it's very often still there. And once a
class of failure is proven non-transient (an auth rejection, not a data-freshness race), don't keep
retrying it just because the surrounding code already has a retry loop — check *why* the retry existed
before reusing it for a new failure mode. One more from this same incident: `coc_retry()` is a shared
handler called from dozens of unrelated call sites — elevating `coc.NotFound`'s log level from silent to
`WARNING` (done here specifically to see CWL war-tag 404s) also caught `get_league_group()`'s routine
"clan not currently in CWL" 404, fired for every actively-tracked clan every cycle, and flooded PROD's
log at fleet scale. Before raising a shared handler's log level for one investigation, check what *else*
routes through that same branch — a change scoped to "this one exception" can still be far too broad if
the exception type itself is reused for routine conditions elsewhere. Reverted to debug.

**Follow-up (same day), two more refinements once the dust settled:**
- **DB-first, not live-first.** Prompted by "why do we hit the live API at all when this bot's own
  periodic tracking already has the data" — a fair question, since the whole DB-fallback machinery
  above only exists *because* the data is already there. `generate_cwl_group_analysis_embeds()` now
  checks `war_attacks`/`war_summary` for every war tag in the group **before** any live call
  (`_load_cwl_wars_from_db_sync(all_war_tags)`, one bulk query), and only fetches live for whatever the
  DB doesn't have yet. In the common case (a group made entirely of clans this bot's Discord server
  tracks — the normal case for a "family" server) that's zero live calls, not up to 28: faster, and
  structurally immune to the whole class of live-fetch flakiness this pitfall exists to document.
- **Stop warning about rounds that haven't been played yet.** The "N war(s) could not be verified"
  footer was firing even when the *correct* answer was "round 7 of 7 hasn't started" (e.g. 24/28 wars
  — genuinely complete for round 6). Fixed by grouping unrecovered tags by which round they belong to
  (from `league_group.rounds`) and only keeping a tag in the warning if **some other war in the same
  round has data** — a round with zero data anywhere is indistinguishable from "not started" and isn't
  worth alarming over; a round with partial data but one specific war missing is a genuine gap.

## Pitfall 25: Enabling Discord Activities breaks the next global command sync — `discord.py` has no model of the auto-created Entry Point command

Symptom (2026-08-09, `CWL_CLAN_CONFIG_ACTIVITY_PLAN.md` Phase D): restarting the PROD bot right
after enabling Activities in the Developer Portal for the PROD application (a routine Phase D
step, needed to launch the CWL Clan-Config web Activity) crashed the whole process on startup:

```
[SETUP_HOOK] Registering commands globally (global mode)
global_command_sync failed: 400 400 Bad Request (error code: 50240): You cannot remove this
app's Entry Point command in a bulk update operation. Please include the Entry Point command
in your update request or delete it separately. (not retrying — client error)
```

`discord_retry()` correctly treats a 4xx as permanent and re-raises; that exception propagated
out of `setup_hook`, uncaught, and the top-level handler ran `async_cleanup()` and exited —
PROD was down until fixed.

Root cause: enabling Activities auto-creates a **global** `PRIMARY_ENTRY_POINT` command (type
4, Discord's auto-managed `/launch` command — see Phase A of the plan doc) that `discord.py`
2.7.1 has zero awareness of at all; its `AppCommandType` enum doesn't even define the value.
`CommandTree.sync(guild=None)` always does a full bulk overwrite built only from commands
registered in the tree (`_get_all_commands()` → `bulk_upsert_global_commands()`), so it
necessarily omits a command type the tree can't represent. Discord used to just silently delete
whatever a bulk overwrite omitted; for Entry Point commands specifically it now refuses the
entire request instead, per its own error message. `CommandTree.clear_commands(guild=None)`
followed by `sync(guild=None)` hits the exact same wall (an empty payload still "omits" it) —
this silently broke `_clear_global_commands_after_ready()`'s DEV-app cleanup path too, already
wrapped in try/except there so non-fatal, but quietly failing to actually clear anything, since
the DEV app has had an Entry Point command since Phase A.

Fix: never call `tree.sync(guild=None)` or `tree.clear_commands(guild=None)` + `sync()`
directly once an app might have Activities enabled. Use `bulk_sync_global_commands()`
(`qapbot/discord_health.py`) instead — it fetches the app's current global commands via a raw
`bot.http.get_global_commands()` call, filters for any command with `type == 4`, and always
splices it back into the bulk-upsert payload (`tree_payload + entry_points`) before calling
`bot.http.bulk_upsert_global_commands()` directly. Passing `tree_payload=[]` (the "clear"
case) still correctly preserves the Entry Point command while wiping everything else. Guild-scoped
syncs are never affected — Entry Point commands are inherently global-only, so
`tree.sync(guild=some_id)` was never in danger. This is a standing fix, not a one-off
workaround: every future global sync on either the DEV or PROD app (both Activities-enabled
now) would otherwise hit this exact wall again. 3 tests added in
`tests/discord/test_discord_health.py`.

---

## Pitfall 26: A sync DB *write* called directly inside an `async def` interaction callback can freeze the whole bot, not just that one interaction

Symptom (2026-08-16, PROD): clicking "Add New Season" in the CWL Management Hub while the
periodic update cycle was mid-write produced a `database is locked` error from
`create_cwl_event_sync`, immediately followed by unrelated `[COC-API-SLOW]` log lines for
*other guilds'* clans all completing ~5.1-5.2s later than they should have (bunched at the exact
same wall-clock moment), and the interaction itself died with `discord.errors.NotFound: 404
Unknown interaction`.

Root cause: `qapbot/ui_cwl_roster.py`'s `_make_cwl_management_add_season_callback` called
`db.create_cwl_event_sync(...)` **directly** — not `asyncio.to_thread()`-wrapped — inside its
`async def callback`. That method (`db_manager.py`) does `with self._sync_write_lock:` (a plain
`threading.Lock` shared by every sync writer in the process) around a SQLite write with
`PRAGMA busy_timeout=5000`. Called this way, the lock-wait and the busy-timeout wait both happen
**on the event loop thread itself** — so while they block, `asyncio` cannot schedule ANY other
coroutine on that thread, not just this one. Every concurrent `await` elsewhere in the bot
(unrelated guilds' CoC API calls, other interactions, the gateway heartbeat) stalls in lockstep
until the lock is released or `busy_timeout` expires. Separately, by the time this callback
finally called `interaction.response.send_message()`, more than Discord's ~3s ack window had
already elapsed, so Discord had invalidated the interaction — hence the `404 Unknown interaction`
crash on top of the freeze.

This is a distinct case from Pitfall 16's `gc.collect()`/hashlib gotcha, not a repeat of it:
`threading.Lock.acquire()` and CPython's stdlib `sqlite3` C module both release the GIL while
blocked waiting (they're genuine I/O-like blocking calls, not one atomic non-yielding C call), so
`asyncio.to_thread()` **does** fix the whole-bot-freeze half of this bug — moving the call off the
event loop thread means the lock/busy-timeout wait happens on a worker thread, leaving the event
loop free to keep processing everything else concurrently. It does NOT, by itself, guarantee
*this* interaction's own response beats Discord's 3s deadline if the wait is genuinely that long —
that part still needs graceful handling (below).

Fix, two parts, both required:
1. **Every `db.*_sync()` call with a write verb (`create_`, `set_`, `update_`, `delete_`,
   `upsert_`, `insert_`, `save_`, `remove_`, `evict_`, `mark_`, `add_`) made from inside an
   `async def` Discord interaction callback (button/select/modal) must go through
   `await asyncio.to_thread(db.method_name, *args)`, never called bare. Read-only `get_*_sync`/
   `list_*_sync` calls are lower risk (SQLite WAL mode lets reads proceed concurrently with a
   writer in the common case) but wrapping them too is harmless and matches the convention used
   elsewhere (e.g. `QapBot.py`'s startup `check_hot_history_schema_parity_sync()` call).
2. **Any response sent after one of these calls must tolerate the interaction having already
   expired** — wrap `interaction.response.send_message()` in `try/except discord.errors.NotFound:
   logging.warning(...)` (see `_respond_or_log()` in `ui_cwl_roster.py`) instead of letting it
   raise unhandled. One caller (`_launch_cwl_activity`, the Discord Activity LAUNCH_ACTIVITY
   callback) has the opposite constraint — it has no deferred/followup form at all, so it must
   remain the interaction's literal first response and cannot call `defer()` first; that path
   already degrades to a plain text fallback (and swallows the final exception) if the interaction
   died before it got there — a template worth reusing for other "must-be-first-response" flows.

Diagnostic tool for finding what the periodic cycle itself was doing at the time:
`qapbot/scripts/log_time_gaps.py` (see Pitfall 16) against the live PROD log around the
incident's timestamp — not run for this specific incident since the local dev copy of
`data/logs/qapbot.log` predates the PROD restart and doesn't cover it, but it's the right tool
for correlating a `database is locked` timestamp with what the update cycle's own sync writes
were doing at that exact moment.

**Full-sweep follow-up, same day**: an audit of every `qapbot/ui_*.py`, `QBdiscocmdshelper*.py`,
and `qapbot/web_bridge.py` write path found the anti-pattern was not a one-off — the entire CWL
shared-clan/roster subsystem had **zero** `asyncio.to_thread()` usage anywhere (`QBdiscocmdshelper_cwl.py`,
`web_bridge.py`), across season create/delete/carry-over, cross-guild sharing/detach/evict,
signup/assignment upserts, and Start Enrollment. `web_bridge.py` matters exactly as much as the
Discord-side files here even though it has no Discord interaction of its own — its module
docstring is explicit that its aiohttp handlers run "IN-PROCESS with the bot (same asyncio loop,
same CACHE/db_manager)", so a bare sync write in an HTTP handler freezes the whole bot exactly
like one in a button callback would.

Fixed by two concrete sub-patterns, chosen per function based on whether it mixes real `await`s
with its sync DB work:
- **Pattern A — bundle, don't scatter.** A function whose body is *already* 100% synchronous
  today (an `async def` with zero `await` inside, or a plain sync `def`) gets ONE
  `asyncio.to_thread()` hop wrapping the whole thing (via a small private `_*_sync` helper),
  never one hop per DB call. This isn't just an efficiency choice: today, with zero yield points,
  nothing else can run between that function's own reads and writes — a second concurrent call
  for the same clan/event can't interleave mid-sequence. Splitting one hop into several would
  introduce new interleaving windows that don't exist today, which is a correctness regression,
  not a style one. Examples: `ensure_cwl_clan_sharing`'s two branches
  (`_attach_guild_to_existing_shared_clan_sync`/`_create_new_shared_clan_sync`),
  `auto_assign_prior_cwl_members`, `evict_guild_from_shared_clan`,
  `handle_post_cwl_enrollment_assign`/`_guest` and `handle_post_clan_config` in `web_bridge.py`
  (`_prepare_and_save_clan_config_sync`).
- **Pattern B — wrap call-by-call.** A function that already has a real `await` in the middle
  (e.g. `prune_or_detach_shared_clans_before_deletion`/`detach_guild_from_shared_clan_on_deactivation`,
  both of which `await resolve_cwl_clan_owner()` mid-sequence) was never atomic across that
  `await` to begin with — another coroutine could already interleave there before this fix. For
  these, each sync DB call around the existing `await` is wrapped individually; no new
  interleaving risk is introduced since one already existed.

`assign_cwl_player_sync` (the one general "place a player in a pool" write path, already a plain
sync `def` used as an atomic unit by design) needed no changes itself — only its 3 call sites
(`web_bridge.py`'s drag-and-drop handler, `auto_assign_prior_cwl_members`, `start_cwl_enrollment`'s
seed loop) needed `to_thread()`-wrapping, and two of those already got it for free once their
containing function became Pattern A.

**2026-08-17 follow-up, PROD meltdown**: this rule covers **READ** paths too, not just writes —
proven the hard way. `web_bridge.py`'s `_search_cwl_guests()` (an uncapped scan over
`CACHE.clan_name_cache`/`CACHE.player_name_index`, millions of entries), `_build_enrollment_payload()`,
`_build_clan_config_payload()`, and `handle_get_cwl_player_stats`'s `get_recent_cwl_player_stats()`
call (a 3-month main+history SQL aggregation, run once per tile hover) were all still bare `async
def`/direct calls running full DB-query sequences and O(n) cache scans on the event loop — the
"lower risk, WAL lets reads proceed concurrently" note above is about a read racing a WRITER's
lock, not about a slow read blocking the loop itself, which is the actual failure mode: escalating
multi-second gen-2 GC pauses and 3x Discord gateway heartbeat misses (`CWL_PROD_PERFORMANCE_FIX_PLAN.md`).
Fixed with the same Pattern A treatment (`_search_cwl_guests_sync`/`_build_enrollment_payload_sync`/
`_build_clan_config_payload_sync`, one bundled `to_thread()` hop per handler) plus, for the guest
search specifically, a single-flight/newest-wins guard per `(guild_id, discord_user_id)` so a burst
of debounced keystrokes can't pile up concurrent scans for the same admin. Do **not** wrap pure
`CACHE` dict lookups that are O(1) (e.g. `handle_get_cwl_screen`, `handle_get_cwl_clan_names`) —
the hop is for DB-query sequences and O(n) scans, not every read.

## Pitfall 27: Adding `overflow-x: auto` to a wrapper around a `position: sticky` element can silently relocate that element's sticky containing block, hiding content behind it

Symptom (2026-08-16, PROD): the CWL clan-config table's `(34)` fix (adding a `.table-scroll`
wrapper with `overflow-x: auto` around `<table>`, to make an over-wide table scroll sideways
instead of letting cells wrap and break row alignment) shipped clean in isolation, but broke the
very screen it fixed — the column header row rendered, then every clan row underneath it was
gone, at ANY window width or scroll position (not just the narrow-window case (34) targeted).

Root cause: `activity/client/src/clanConfigTable.ts` already had `thead th { position: sticky;
top: <JS-computed pixel value> }`, set to the height of a sibling `.board-topbar` element so the
column headers stick just below it as the page scrolls. Before (34), nothing between `th` and the
page had non-`visible` overflow, so `th`'s sticky *containing block* was the page/viewport, and
the JS-computed offset was correct relative to that. (34)'s new `.table-scroll { overflow-x: auto
}` wrapper changed that silently: per the CSS Overflow spec, when one axis's overflow is
`visible` and the other isn't, the browser forces the `visible` axis's *used* value to `auto` too
— this is real, current, unconditional Chromium/Firefox/Safari behavior, not a bug in the app's
own CSS, and it cannot be worked around by setting `overflow-y: hidden` explicitly (non-`visible`
still counts, regardless of which non-`visible` value it is). That silently made `.table-scroll`
the new nearest-non-`visible`-overflow ancestor — i.e. the new sticky containing block for the
`th`s inside it — even though its `height` was left `auto` (sized exactly to its own content), so
it never actually has anything to scroll internally. A sticky element whose containing block never
scrolls has nothing to react to: it just renders at a permanent, fixed offset from that
container's top, forever — which is exactly what happened, except the "fixed offset" left over
from before (34) was `<topbar's height>px`, pushing the header down on top of the first couple of
rows and hiding them behind its own opaque background (`thead th`'s `background: #313338`, needed
so scrolled-under rows don't show through it).

**The generalizable trap**: adding `overflow-x` (or `-y`) to *any* element has a real chance of
silently promoting it to a CSS "scroll container" on BOTH axes, which changes the sticky
containing block for every `position: sticky` descendant inside it — even ones that look
unrelated to the specific overflow you meant to fix, and even when that element never actually
develops a scrollbar. This is invisible in the diff (nothing about `position: sticky` changed) and
in a type-checker (no TypeScript/CSS syntax is wrong), so it only shows up as a live rendering bug.
Before adding `overflow-x`/`overflow-y` to any element, grep its descendants for
`position: sticky` — if any exist, either (a) verify their `top`/`left`/etc. offsets are still
correct relative to the NEW containing block, not the old one, or (b) if genuine page-level
sticky tracking still matters, give the container a real, JS-computed bounded height (`max-height`
or `height`) so it becomes an actual scroll container rather than an inert one — see
`enrollmentBoard.ts`'s `resizeBoard()`/`.board` pattern (which solved this identical
containing-block problem first, before clan-config table existed) and
`resizeTableScroll()`/`.table-scroll` in `clanConfigTable.ts` (this fix) for two working examples
of the JS-computed-bounded-height approach.

Confirmed both the root cause and the fix empirically, not just by reading the CSS spec — this
class of bug is layout/paint behavior a DOM inspection or a `grep` cannot show; a throwaway
Playwright harness rendering the real `renderClanConfigTable()` against mock data (outside the
git-tracked tree) was used to screenshot the broken state, inspect the sticky element's
`getBoundingClientRect()` and `.table-scroll`'s computed `overflow-y`, then verify the candidate
fix restored both correct row visibility AND correct sticky-tracking-during-scroll before it was
applied to the real source.

## Pitfall 28: A local `from module import NAME` inside one branch of a function poisons `NAME` for the WHOLE function, even in sibling branches that never run it

Symptom (2026-08-17, found live-testing): opening the DEV VS Code "Problems" panel for
`QBdiscordcmds.py` showed 35 Pylance errors — `"CACHE" is unbound` plus a cascade of `Cannot
access attribute "X" for class "Unbound"` at unrelated-looking lines scattered from ~1476 to
~2580. Every one of those lines is inside `admin()`, a single ~1400-line function implementing a
dropdown-driven admin-action router (`if action_norm == "X": ... return`, repeated for a couple
dozen actions).

Root cause: `CACHE` is imported once at module level (`from qapbot.cache_manager import CACHE`,
near the top of the file) — that should make it available everywhere. But three of `admin()`'s
individual action branches ALSO had their own `from qapbot.cache_manager import CACHE` line,
added independently (likely out of habit/caution, not because any of them actually needed a
fresh import — `CACHE` was already the same module-level object). Python's compiler determines a
name's scope for an ENTIRE function body statically, before any code runs: `from X import Y`
inside a function is compiled exactly like `Y = ...` — if it appears ANYWHERE in the function
(any branch, any nesting depth), `Y` is treated as a LOCAL variable for the WHOLE function, not
just the branch it's textually in. Since only one `if action_norm == "X": ... return` branch
ever executes per call, invoking `admin()` with an action whose branch references `CACHE` BEFORE
(in line-number terms) one of the three re-import branches — mutually exclusive branches, so
that re-import line never actually runs for this call — raises `UnboundLocalError: cannot access
local variable 'CACHE' where it is not associated with a value` the instant `CACHE` is touched.
Confirmed with a 10-line repro (`def outer(branch): if branch=="A": print(X) \n if branch=="B":
from os import getcwd as X`) — calling `outer("A")` alone crashes, even though branch B (the one
with the local import) never runs.

This is NOT a Pylance false positive — Pylance's `reportUnboundVariable`/`Cannot access
attribute ... for class "Unbound"` here is correctly predicting a REAL runtime crash for
whichever admin action's branch happens to reference `CACHE` earlier in the function than the
first branch (in source order) that re-imports it. At least one live admin action
(`BACKFILL_CWL_GROUPS`) was broken this way — every invocation would have raised
`UnboundLocalError` the moment its `on_submit` handler touched `CACHE`.

Fix: delete the redundant local re-imports (`CACHE`, and a same-function `CONFIG` one found by
the same audit) — both names are already module-level globals, so the local imports were pure
liability with zero benefit. After deletion, every reference in the function correctly resolves
to the module-level global for the function's entire body, since nothing shadows it anymore.

**Rule going forward — this codebase deliberately favors LOCAL imports inside functions (see
this cookbook's own convention throughout, and Pitfall 26's `to_thread` sync-core pattern), so
this exact landmine is a real, recurring risk, not a one-off**: before adding `from module import
NAME` inside ANY function (or nested branch/class/closure within one), check whether `NAME` is
already imported at module level in that file. If it is, the local import is unnecessary —
delete it, don't add it. If a name genuinely needs a fresh/deferred import inside a specific
branch (e.g. to dodge a circular import), give it a distinct local alias
(`from x import Y as _Y_deferred`) instead of reusing a name that's also a module-level global,
so a sibling branch can never have its scope silently poisoned. A quick audit technique: `grep
-n "^from \|^import " <file> | head -50` to list module-level imports, then grep the same names
indented deeper in the file — any hit is a candidate for this exact bug (verify by checking
whether the name is used anywhere else in the SAME enclosing function before that local import's
own line, in source order, across ALL sibling branches, not just the one containing the import).

Separately, `AVAILABLE_COMMANDS` (a local, ALL-CAPS variable in the `/help` command, legitimately
reassigned once for the DM-filtered case) tripped Pylance's `reportConstantRedefinition` — a
naming-convention lint, not a real bug (Python has no true constants; reassigning a local is
always legal). Renamed to lowercase `available_commands` since it's genuinely not a constant —
cheaper than fighting the lint, and more honest about what the variable actually is.

---

## Pitfall 29: `asyncio.wait_for(timeout=...)` around an `asyncio.to_thread()` call cancels the *wait*, not the underlying thread

Symptom (2026-08-17, PROD): `QapBot.py`'s startup DB initialization is wrapped in
`asyncio.wait_for(db_manager.initialize(...), timeout=1800.0)` as a safety net against a
genuinely stuck migration. A one-time backfill inside that call (`_backfill_player_name_search_
if_needed()`, itself correctly `asyncio.to_thread()`-wrapped per Pitfall 16) took longer than 30
minutes on PROD's weak hardware. The timeout fired as designed, logged `"Database initialization
timed out - bot cannot start"`, and the caller treated startup as failed.

Root cause / the part that's easy to miss: `asyncio.to_thread()` (and `run_in_executor()` under
it) hands the synchronous function to a `ThreadPoolExecutor` worker thread and returns a
`Future`. When `wait_for()`'s timeout expires, it cancels the *awaiting* task — which, at the
`await` point suspended on that `Future`, raises `CancelledError` back into the coroutine. It
does **not**, and cannot, stop the worker thread itself: CPython has no supported mechanism to
forcibly terminate a running thread. So the actual synchronous work (in this case, mid-way
through a `DELETE`+`INSERT` bulk write against SQLite) keeps executing in the background, on its
own schedule, entirely independent of whatever the rest of the process does with the
`TimeoutError`/`RuntimeError` that resulted from the cancellation.

Why this mattered in practice here: since the backfill's only `conn.commit()` sits at the very
end of its transaction, an in-progress (never-committed) run rolling back cleanly on the next
SQLite connection open was the actual safety net — not the `wait_for()` timeout, despite that
being what fired. In other cases (e.g. a background write that reaches its own commit *after*
the surrounding code has already decided the operation "failed" and moved on) this pattern can
let a nominally-cancelled operation still take effect later, unobserved, out of sequence with
whatever ran after the timeout — a much subtler bug than a straightforward failure would be.

Fix / mitigation, situational (no single universal fix — depends what the wrapped work is):
- If the wrapped work is a single atomic SQL transaction with one commit at the end (as here),
  a `wait_for()` timeout is *safe* even though it doesn't stop the thread — SQLite's own
  transaction atomicity means "cancelled before commit" and "still running in the background"
  both resolve to the same safe outcome (rolled back / eventually committed with fully-valid
  data) rather than a torn write.
- If the wrapped work has multiple independent side effects (several separate commits, or
  non-DB side effects like a Discord API call) a `wait_for()` timeout is NOT a safe way to bound
  it — the timeout only stops YOUR code from waiting, not the effects from happening. Reach for
  actual cooperative cancellation (checking a cancellation flag between steps) or accept that a
  timeout here can only be a monitoring signal, not a hard stop.
- Making the wrapped work itself faster (see Pitfall 16's follow-up in this same file, same day
  — replacing 6.6M per-row `hashlib` calls with one bulk SQL query cut this specific backfill
  from 355s to 82s) is often more valuable than trying to make the timeout "safer," since it
  avoids ever needing the timeout to fire for a legitimate run in the first place.

---

## Pitfall 30: A new `/api/cwl/...` bridge endpoint needs a route in TWO places, not one

Symptom (2026-08-18, live-tested in DEV): three new CWL enrollment endpoints
(`POST /api/cwl/enrollment/guest-clan/remove`, `GET /api/cwl/enrollment/guest-players`,
`POST /api/cwl/enrollment/guest-players/remove`) were added to `qapbot/web_bridge.py` — routes
registered, handlers written, unit-tested against the bridge directly, all green. In the actual
DEV Activity, every one of them 404'd unconditionally: `{"error":"not found","path":"/cwl/
enrollment/guest-players","method":"GET"}` — regardless of whether the underlying data existed,
which read at first like an empty-state case rather than a routing bug.

Root cause: the CWL clan-config Discord Activity has TWO server-side layers, not one —
`activity/server/src/index.ts` (a separate Cloudflare Worker, Hono-based) sits in front of
`qapbot/web_bridge.py` (the actual Python bridge) and does two jobs neither side can do alone:
verifies the Activity's Discord user via `verifiedDiscordUserId()` (the bridge itself only
checks a shared secret, never the caller's real identity) and injects the server-verified
`discord_user_id` into the forwarded request (so a client can never spoof someone else's ID).
Every single bridge endpoint therefore needs an explicit matching route in `index.ts`'s Hono
`api` router — there is no wildcard/passthrough. `web_bridge.py` getting a new route says
nothing about whether `index.ts` has one; the bridge's own tests (which hit it directly, bypassing
the Worker entirely) can't catch a missing proxy route either — this is exactly why it went
unnoticed until a live click in the actual Activity iframe.

Fix: add the new endpoint to BOTH files, mirroring the exact verify-identity-then-proxy shape
every existing route in `index.ts` already uses (see any `api.get(...)`/`api.post(...)` block
there — query-string forwarding for GET, `{ ...body, discord_user_id: discordUserId }` for
POST). `npm run typecheck` in `activity/server` catches a malformed route but NOT a missing one
— only an actual live request (or manually diffing the two files' endpoint lists) surfaces that.

---

## Pitfall 31: `user_players` only has rows for clans with `has_active_subscriptions` — a CWL guest clan has none

Symptom (2026-08-19, live-tested in DEV): a guest clan added to a CWL season showed a board column
with **zero** players, forever. `Save` succeeded, the clan row persisted, the auto-assign/pool seed
ran without error, and every unit test of the seed passed. Nothing in the log looked wrong.

Root cause: everything in the CWL enrollment feature that asks "who is in this clan right now"
goes through `db_manager.get_current_clan_members_sync()`, which reads
`user_players.current_clan_tag`. That table is populated by exactly one writer —
`coc_cache.py`'s `update_player_info_in_user_accounts()`, including the UNASSIGNED-pool rows it
creates for never-linked players. Since the 2026-08-14 scope-bug incident, that call is gated on
`clans.has_active_subscriptions`:

```python
if clan_data.get("has_active_subscriptions"):
    await self.update_player_info_in_user_accounts(clan_obj, self.cache_manager)
```

and `has_active_subscriptions` is recomputed in `cache_manager.update_all_clan_subscription_statuses()`
from **channel subscriptions + guild `member_clans` + guild `member_families`** — nothing else.
A CWL **guest** clan is in none of those sets, so it has zero `user_players` rows and every seed
reads an empty member list.

Why it hid for so long: a guest clan that happens to be some *other* guild's own member clan is
tracked for unrelated reasons and works fine. Only a genuinely foreign clan — one no guild on the
bot subscribes to — exposes the gap. Every guest clan used in testing until this report was of the
former kind.

Two general lessons beyond CWL:

1. **`user_players` is not a general "who's in clan X" table.** It is a projection of the clans
   this bot is *subscribed* to. Before building any feature on `get_current_clan_members_sync()`
   (or `current_clan_tag` directly), ask whether the clans involved are guaranteed to be in
   `tracked_tags`. If a user can name an arbitrary clan, they are not.
2. **Diagnose with the live DB, not the code.** Two queries settled "did our recent fixes break
   this?" instantly:
   ```
   sqlite3 -readonly data/qapbot.db "SELECT clan_tag, has_active_subscriptions FROM clans WHERE clan_tag IN (...)"
   sqlite3 -readonly data/qapbot.db "SELECT current_clan_tag, COUNT(*) FROM user_players GROUP BY 1"
   ```
   `0`/`0 rows` for the broken clan vs `1`/`47 rows` for the working one named the root cause
   directly, where reading the seed code had produced several plausible-but-wrong theories.

Fix pattern (`QBdiscocmdshelper_cwl.ensure_cwl_clan_membership_tracked`): for a clan a user has
explicitly put on a roster and which has **no** tracked members at all, fetch it once via
`CACHE.coc_clan_cache.get_clan()` and run `update_player_info_in_user_accounts()` on the result —
the same population path, just on demand. Do **not** add such clans to `tracked_tags` to fix this:
`_calculate_track_war_updates()` is a one-way ratchet, so that permanently enables war polling for
a clan that is only relevant for one season.

## Pitfall 32: an individually-invited guest PLAYER has none of the cross-guild conflict protection a guest CLAN gets

Symptom (2026-08-20, live bug report, project owner): a player already deliberately placed in
`StayMad`'s CWL roster (guild "STAY Family") was added as an individual guest player to a
completely different guild ("The QCrew")'s own CWL pool via the Guests search's "Add" button. The
add succeeded silently and the player showed up as plain Unassigned in the new guild — no warning,
no "Assigned to other Guild" badge, nothing indicating they already had a real home elsewhere.
Dragging them into a column there would have given them a second, fully independent placement in
a second guild's roster for the same season — something real CWL rules never allow, and something
neither guild's admin would have any way to notice until CWL day.

Root cause: this feature has TWO structurally different ways to add a guest, and only one of them
is cross-guild-aware. A guest **clan** (`_search_cwl_guests_sync` → `cwl_event_clans`) becomes a
real `cwl_shared_clans` entry the moment a second guild also has it, and from then on every
placement into it — auto-assign seed or deliberate drag — flows through
`assign_cwl_player_sync`'s elaborate conflict-purge/preservation machinery (eviction on a
deliberate drag, defer-and-mirror-into-"Assigned to other Guild" on an automatic guess). A guest
**player** (`handle_post_cwl_enrollment_guest` → a plain `cwl_signups` row, `source='guest_invite'`)
never touches any of that: it writes straight into the inviting guild's own local tables with zero
awareness that the same `player_tag` might already have a private (non-shared) `cwl_assignments`
row sitting in a totally different guild's own event for the same season.

The two "already in the pool" guards this endpoint already had (`_search_cwl_guests_sync`
excluding already-invited tags, the guest-clan-membership check in
`handle_post_cwl_enrollment_guest`) only look at **this guild's own** tables — neither one queries
across guilds, so neither one could ever have caught this.

Fix: `db_manager.find_cwl_player_private_placement_in_other_guilds_sync(player_tag, season,
exclude_guild_id)` — a `cwl_assignments` row, by construction, only ever represents a PRIVATE
(non-shared) placement (a shared target always routes through `cwl_shared_clan_players` instead,
per that table's own CREATE TABLE comment), so any hit here is a genuine, independent placement
this guild has no visibility or authority over. Wired into two places: (1)
`handle_post_cwl_enrollment_guest` refuses the invite itself (409, same pattern as the existing
guest-clan-conflict check); (2) `assign_cwl_player_sync` refuses a **deliberate** placement
(returns an error string instead of `None`, surfaced by `handle_post_cwl_enrollment_assign` as a
409) as defense-in-depth for a signup that already slipped through before this fix, or any other
future write path. A **non-deliberate** (auto-assign seed) placement just quietly skips instead —
matching the existing "an automatic guess never creates a real conflict" philosophy for shared-clan
conflicts, but deliberately NOT attempting that branch's "mirror into an orphaned local assignment"
treatment: doing so would also require teaching `_build_enrollment_payload`'s purge step
(`web_bridge.py`) to preserve a private (non-shared) orphaned assignment, which was left out of
scope as a follow-up rather than a correctness gap — the player simply stays plain Unassigned
there instead of being double-booked.

General lesson: when a feature has two on-ramps for conceptually the same thing (a guest CLAN vs.
a guest PLAYER; more generally, a bulk/structured path vs. an individual/ad-hoc one), audit
whether a safety mechanism built for one on-ramp — here, `cwl_shared_clan_players`'s whole
cross-guild machinery — was ever actually wired into the other, rather than assuming "guests are
handled" covers both.

**Follow-up (2026-08-20, same day, live bug report, project owner):** the deferred display gap
above turned out to matter sooner than expected, and through a THIRD on-ramp neither this pitfall's
original fix nor Pitfall 33 touched: `cwl_enrollment_include_all_linked_accounts` (account-wide
expansion — a Discord account that qualifies via one participating-clan player also pools its
OTHER linked players, wherever they currently play). Reproduced live: player Killer, already
deliberately placed in "The Marines" guild's own private CWL roster this season, got pooled into
"The QCrew" guild too via this expansion and showed up as plain Unassigned there — no hint they
already have a real home elsewhere, exactly the deferred gap's predicted symptom. Closed by
extending `find_cwl_player_private_placement_in_other_guilds_sync` into a proper batched sibling,
`find_cwl_players_private_placements_in_other_guilds_sync(player_tags, season, exclude_guild_id)`
(the single-tag version is now a thin wrapper around it — **one query implementation now backs
all three use sites**: the guest-invite guard, the assign-time guard, and this new read-time
mirror in `_build_enrollment_payload`), which mirrors a still-unresolved private placement into
`assigned_clan_tag` for any pooled player who has no local assignment of their own — the exact
same "Assigned to other Guild" pseudo-column a cross-guild SHARED clan placement already gets via
the merge just above it, now working identically for the private case. Confirms the general
lesson above generalizes past "two on-ramps": **any** path that can populate a guild's pool
(explicit invite, drag, auto-assign, OR bulk/account-wide expansion) needs to either write through
the one general placement guard (`assign_cwl_player_sync`) or be covered by the display-time
mirror — auditing only the write paths that looked like "assignment" missed the read-time gap
until a THIRD, bulk seeding path exposed it.

## Pitfall 33: a "current clan beats stale history" override, meant for a GUEST clan, also fired for the family's OWN other clan

Symptom (2026-08-20, live bug report, project owner, PROD — "The Marines" family, 2 clans): right
after Start Enrollment ran, player Killer — a genuine current member of "The Marines" — landed in
"The Marines" column despite having played CWL for "The Marines II" (the *same family's other
clan*) just last season. Verified against the real data (`get_last_real_cwl_attack_clan_sync`
called directly): Killer's last real CWL attack (`2026-08-09`, `is_cwl=1`) genuinely was for The
Marines II — several *regular* (non-CWL) war attacks for The Marines afterward, correctly excluded
by the query's own `is_cwl=1` filter, confirmed they really did transfer clans after CWL ended.
The auto-assign function itself (`get_last_real_cwl_attack_clan_sync`) was working exactly as
designed — the bug was one layer up.

Root cause: `start_cwl_enrollment`'s own post-`resolve_prior_cwl_assignments` override (the
2026-08-19 fix for "theqcrew members get auto assigned to staycalm" — see Pitfall 32's sibling
incidents and `test_current_family_clan_membership_beats_stale_history_for_a_guest_clan`)
redirects a player's auto-assign target to their live current clan whenever that current clan is
itself participating this season — with **no check on what the history it's overriding actually
points at**. The original bug's own docstring already named the intended scope in passing
("some earlier season's history for a totally different participating clan (**most commonly a
guest clan**)") but the code never encoded that distinction — it fired for ANY participating
current clan, including, as here, another one of the SAME multi-clan family's own clans. A player
switching between a family's own clans between seasons (Marines II → Marines) is completely
normal churn and produces perfectly valid, one-season-old, in-family history — nothing like the
original bug's out-of-family guest-clan mixup — but the override couldn't tell the two apart and
discarded the correct, fresher signal (real CWL history) in favor of the wrong one (current clan)
every time.

Fix: added a `prior_assignments[tag] not in family_clan_tags` guard (`family_clan_tags =
resolve_guild_member_clan_tags(guild_id)`, already computed nearby in the same function) — the
override now only redirects when the history it's about to discard points OUTSIDE the guild's own
clan family. History pointing at another of the family's own clans is left alone, matching
`resolve_prior_cwl_assignments`' own original design ("assign to wherever they last actually
played, not wherever they're currently rostered"). Verified against all three existing override
tests (still pass — none of them involve a family clan on both sides) plus a new one,
`test_current_clan_does_not_beat_history_pointing_at_another_family_clan`
(`tests/integration/test_cwl_start_enrollment.py`), locking in the Marines/Marines II scenario.

General lesson: when a fix's own code comment says "most commonly a guest clan" (or any other
"usually X" qualifier) but the actual `if` condition doesn't test for X at all, that gap is a live
bug waiting for the "not X" case to occur — encode the qualifier explicitly rather than relying on
it being true in practice. A live-testing fix aimed at one narrow failure mode (foreign/guest-clan
history overriding a real current clan) had a checkable, specific distinguishing signal available
the whole time (family membership) and simply never used it, so it silently regressed every
adjacent case (in-family clan transfers) it was never actually meant to touch.

## Pitfall 34: a Discord attachment's CDN URL is signed and expires — download it the moment you receive it, not when you're ready to use it

Design-time trap identified while building the bug/feature tracker (`BUG_FEATURE_TRACKER_PLAN.md`
§3.3, §2.2), not (yet) a confirmed live incident — documented here because it's exactly the kind
of thing that silently breaks weeks after shipping, the first time someone leaves a Discord modal
open for a few minutes with attachments already selected.

The trap: `discord.Attachment.url` on any attachment (a slash-command attachment parameter, a
plain message's `message.attachments`, anything) is a **signed, time-limited CDN URL** — it works
fine right after the message/interaction arrives, then starts returning HTTP 403 once the
signature expires. Code that stores the URL string and defers the actual download to "whenever
this item eventually gets processed" (a modal the user might sit in for minutes, a queued
background job, a value merely passed through and read later) will intermittently 403 depending on
timing that has nothing to do with the code's own correctness — it looks like a flaky Discord API,
not a design bug, which is what makes it slow to diagnose.

Fix pattern used here (`qapbot/ui_tracker.py`, `start_tracker_item()`): call
`await attachment.read()` (which streams the bytes over HTTP right then, not just the URL) as a
background `asyncio.create_task()` **immediately after** `interaction.response.send_modal()` —
never after the modal's `on_submit()` fires, since discord.py's Modal contract requires
`send_modal()` to be the interaction's first response, so the download can't happen first, only in
parallel. `on_submit()` then `await`s that already-in-flight task instead of starting a fresh
download against a URL that may already be dead.

General lesson: anywhere an `Attachment`/its `.url` is captured for later use — not just
modals — download the bytes (`await attachment.read()`) as close to receipt as possible, and pass
the bytes (or a path to bytes already written to disk) onward, never the bare attachment object or
its URL, if there's any gap between "received" and "processed" larger than the current event-loop
tick. The bot's own re-upload into a posted item (`_build_discord_files()`) exists for exactly this
reason on the *durable-storage* side too: a bot-owned copy never expires, but the user's original
CDN link always eventually does.

## Pitfall 35: two independent callers of the same "detect-then-persist" function racing on a shared in-memory list can poison it forever, not just once

Symptom (2026-08-21, confirmed live on PROD): right after a CWL "Start Enrollment" DM blast (120
players, Stay family) — the first time the `cwl_dm_restrict_to_admin` toggle was ever off on
PROD (tracker item #0007) — the log filled with repeating `[DB-WRITE] Transaction failed for
save_user(UNASSIGNED): UNIQUE constraint failed: user_players.discord_id, user_players.player_tag`,
plus unrelated players' self-service "Unlink" confirmations throwing
`discord.errors.NotFound: 404 (10062) Unknown interaction`. The two looked like separate problems
reported together; only one was.

Root cause (the data-integrity half): `coc_cache.py`'s `update_player_info_in_user_accounts()` —
the function that discovers "never-tracked" players in a clan's live roster and adds them to the
shared `UNASSIGNED` pseudo-account — is called from **two independent places**: the normal
periodic Phase-1 poll loop, and (on demand) `QBdiscocmdshelper_cwl.py`'s
`ensure_cwl_clan_membership_tracked()`, itself called from `start_cwl_enrollment()` — i.e. exactly
the flow this DM blast ran. Its "who already owns this player_tag" check is a **DB read**
(`get_player_owners_for_tags_sync()`), while the actual mutation is an **in-memory list append**
followed by a persist at the very end of the same call. If the SAME clan is processed by both
call sites while it currently has zero tracked members (true here — Stay's clan had just hit that
state, which is exactly `ensure_cwl_clan_membership_tracked()`'s trigger condition), both read the
DB before either has written anything, both see the same player as unowned, and both append the
identical player dict to the SAME shared `CACHE.user_accounts["UNASSIGNED"]["players"]` list
object. The second `persist_user("UNASSIGNED")` — a DELETE-then-bulk-INSERT of that whole list —
then hits SQLite's `UNIQUE(discord_id, player_tag)` constraint.

**The part that turned one race into a two-hour incident**: nothing ever removed the duplicate
from the in-memory list after the failed write. `_save_user_impl` correctly rolls back its own
transaction on failure (the DB itself was never corrupted — confirmed by reading the code, not
just log-guessing), but the poisoned Python list stayed poisoned. Every *subsequent*,
completely unrelated `persist_user("UNASSIGNED")` call — including other players' legitimate
self-service unlinks, which route through the same pseudo-account — kept re-attempting to save
the same duplicate-laden list and kept failing identically. Confirmed directly in the log: three
different unlink attempts by different users, minutes apart, all died on the exact same
constraint. One race, indefinitely repeating collateral damage, until a process restart reloads
`CACHE.user_accounts` clean from the (still-correct) DB.

Secondary finding (the "Unknown interaction" half, a separate bug that just became visible under
the same load spike): `UnlinkConfirmView._on_confirm` (`ui_registration.py`) and
`ClanManagementUnlinkPlayerConfirmView._on_confirm` (`ui_clan_management.py`) both went straight
into slow work (`unlink_player()` + `sync_roles_for_user()`) and called
`interaction.response.edit_message()` as their *first* response — no `defer()`. Under the DM
blast's load (`get_current_war`/`get_league_war` calls were logged taking 15–25s each), that
routinely blew Discord's 3-second ack window. This was always latent, just never exposed by normal
load before.

Fix, three parts:
1. **Serialize the racing function per clan_tag.** `CoCClanCache.__init__` gained
   `self._update_locks: Dict[str, asyncio.Lock]`; `update_player_info_in_user_accounts()` is now a
   thin wrapper that acquires `self._update_locks.setdefault(clan_tag, asyncio.Lock())` before
   calling the (otherwise unchanged) renamed `_update_player_info_in_user_accounts_locked()`. Two
   different clan_tags still run fully concurrently — only same-clan_tag calls now queue.
2. **Defense-in-depth de-dup at the write boundary**, independent of whether every race is ever
   found: `db_manager.py`'s `_replace_user_players_rows()` now drops (and logs) any repeated
   `player_tag` before building the INSERT, keeping the first occurrence — turns "hard crash that
   poisons every future call" into "silently corrected, logged, still works."
3. **`defer()` immediately** as the first line of both `_on_confirm` handlers, switching their
   response calls from `interaction.response.edit_message()` to `interaction.edit_original_response()`.

General lesson: a "detect whether X already exists, then mutate a shared collection, then persist
it" function is only safe under a single caller unless it's explicitly serialized — and a second
call site can arrive from a direction you didn't design the first one around (here: an on-demand
CWL helper nobody thought of as "a second entry point into the periodic poll's own logic," because
it calls the exact same shared function rather than duplicating it). Separately: when a write
fails, ask not just "is the on-disk data still correct" (it was — the transaction rolled back) but
"is the in-memory state I'm about to reuse next time still correct" — an object mutated *before*
the failing write, then never repaired after, silently becomes permanent poison for every future
caller that shares it, which is a much larger blast radius than the single failed call suggests.

---

## Pitfall 36: `user_players.added_at` is "last persisted", NOT "when this account was linked"

**Symptom (2026-08-22 investigation):** a forensic query grouped `user_players` by `added_at` and
found all 82 of one Discord user's accounts sharing the exact same second
(`2026-08-21 22:41:49`), several of them tags that a `cwl_signups` snapshot showed belonging to
three *other* Discord users days earlier. That reads unmistakably as a bulk ownership hijack, and a
whole hypothesis was built on it. It was nothing of the sort.

**Why:** `save_user()` → `_replace_user_players_rows()` (`db_manager.py`) persists a user by

```sql
DELETE FROM user_players WHERE discord_id = ?
```

followed by a full re-insert of every player from the in-memory list. `added_at` has
`DEFAULT (datetime('now'))` and is never carried over, so **every** persist of a user rewrites the
timestamp on **all** of that user's rows. The routine clan-poll cycle persists users constantly
(`_update_player_info_in_user_accounts_locked` → `persist_user(uid)` for every user whose TH, name,
role or current clan changed), so a shared `added_at` across a user's whole account list means only
"this user was last written at that moment" — the single most common state for an active user.

**How to apply:**
- Never use `added_at` to date a link, order links, or infer that ownership moved. It cannot
  distinguish "linked just now" from "linked in February and touched by a poll just now."
- The real link/unlink audit trail is the **log**, not the DB:
  `USER ACTION: <user> skipped verification for player <name> (<tag>)`, the `/link` and unlink
  paths' own log lines, and `[USER-ACCOUNTS-UPDATE] <tag>: newly tracked from clan <clan>`.
  Grep the rotated `data/logs/qapbot.log*` for the player tag.
- Watch the timezone when correlating the two: log lines are **local** (UTC+2 on PROD) while
  `added_at` / `datetime('now')` are **UTC**. A DB timestamp of `2026-08-21 22:41:49` is
  `00:41:49` on 2026-08-22 in the log — a different file once the log has rotated.

General lesson: a column whose value is a side effect of *how* a row is persisted carries no
information about the fact the row represents. Before treating any timestamp as domain evidence,
check whether the write path is an UPDATE of changed fields or a DELETE + full re-insert — the
latter resets every defaulted column on every save, for every row, whether or not anything about
that row actually changed.

---

## Pitfall 38: a persistent Discord button is a FOREIGN KEY into the DB — the row must outlive the message

> **Status: fixed 2026-08-22** (`plans/implemented/tracker-0016-dead-signup-dm-buttons.md`).

**Symptom (tracker #0016, live):** users clicking the Confirm/Opt-Out buttons on their CWL
sign-up DM got *"⚠️ This sign-up is no longer valid (the season may have been deleted)"* — for
most of their accounts, while a few worked. No season had been deleted. 27 delivered DMs across
4 Discord users were affected, and all 27 were still `status='pending'` — nobody had responded
because nobody *could*.

**Why:** `CwlSignupResponseButton` is a `DynamicItem` whose `custom_id` encodes only
`(action, event_id, player_tag)`. The callback re-reads the row at click time — correctly, since
the message outlives any in-memory view — and bails when it finds nothing:

```python
signup = await asyncio.to_thread(db.get_cwl_signup_sync, self.event_id, self.player_tag)
if event is None or signup is None:
    ... t('cwl.template.no_longer_valid'); return
```

So the button is effectively a foreign key into `cwl_signups`. Two paths sent that DM, and only
one of them created the row:

| path | seeds `cwl_signups`? | sends DM? |
|---|---|---|
| `start_cwl_enrollment` | yes (`bulk_create_cwl_signups_sync`) | yes |
| `notify_new_cwl_pool_members` ("Notify New Pool Members") | **no** | yes |

They shared the *pool resolution* (`resolve_cwl_pool_dm_targets_sync`) and the *sending*
(`_send_cwl_enrollment_dm_batch`) — which is exactly why nobody noticed the seeding wasn't
shared too. Worse, the notify button's purpose is to reach members added *after* Start
Enrollment, i.e. precisely the players with no row: it was ~100% broken for its intended
population, not an edge case.

**How to apply:**
- **Before sending any message with a persistent button, make sure the row that button resolves
  already exists** — and put that guarantee next to the *send*, not in one caller. It now lives
  in `_send_cwl_enrollment_dm_batch`, so any future caller inherits it.
- **Seed only for recipients who actually get the message.** A target skipped by a dedup or guard
  must not gain a DB row (and hence a board entry) for a message it never received.
- **A delivered message can be repaired in place.** Because the callback resolves at click time,
  back-filling the missing row revives buttons already sitting in users' inboxes — no re-send, no
  new message. `_repair_cwl_signups_for_sent_dms()` does this idempotently at startup, scoped to
  events still `signup_open` (the only status where the button is actionable, and so the exact
  broken population — while leaving a finalized event's historical board untouched).
- **Watch for the misleading error text.** `no_longer_valid` says "the season may have been
  deleted", which sent the first look at this straight down the wrong path. When a persistent
  component reports a missing parent, check whether the parent was ever *created* before assuming
  it was deleted.

General lesson: when two code paths share "who to contact" but not "what state to write", they
will drift, and the symptom shows up much later at the point of interaction. If a message
outlives the process that sent it, everything its buttons dereference is now a persistence
contract — enumerate every sender and make the write an invariant of sending.

---

## Pitfall 37: `cwl_signups` is an enrollment-time SNAPSHOT — re-resolve its `discord_id` on read

> **Status: fixed 2026-08-22** (`plans/implemented/cwl-board-stale-link-and-whois-timeout.md`,
> then `plans/implemented/cwl-dmed-discord-id-rename.md`). All four read paths resolve the live
> owner from `user_players`, the carry-forward writers resolve at the write boundary, and the
> column itself is now named **`dmed_discord_id`** on all three snapshot tables — so it can only
> be read as what it is (who we DMed), never mistaken for ownership again. The "Why" below
> describes the original defect; the "How to apply" is the convention to keep following.

**Symptom (2026-08-22, live report):** a player (`B.A.B.A`, `#2RPLRVUG9`) rendered grey
"Not Linked" on the CWL enrollment board while `/whois` showed it correctly linked. Sixteen other
players on the same board had the same problem, and eight more showed a *different* owner than
`user_players` held.

**Why:** `cwl_signups` rows are written once, by Start Enrollment, and never refreshed.
`_build_enrollment_payload` (`web_bridge.py`) seeds `players_by_tag` from `cwl_signups` **first**
and then skips any tag it already has when merging the live `get_current_clan_members_sync()`
source — so the snapshot's `discord_id` wins permanently. Any account linked *after* Start
Enrollment ran keeps the snapshot's `NULL` forever; any account re-linked to a different user keeps
the old owner forever.

The same staleness reaches DM routing: `resolve_cwl_pool_dm_targets_sync()`
(`QBdiscocmdshelper_cwl.py`) merges its four sources with first-non-`None`-wins
(`entry["discord_id"] = entry["discord_id"] or discord_id`), and the live `get_player_links_sync()`
source runs **last** — so it can add a missing link but can never correct a stale one. A pooled
player with no `current_clan_tag` (not returned by the clan-scoped live source) therefore gets DMed
at whatever Discord account owned them at snapshot time.

**How to apply:**
- Treat `cwl_signups` (and `cwl_shared_clan_players`) as a historical record of what was true at
  Start Enrollment. `user_players` is the only authority for "who owns this account *now*."
- Any field on those tables that can change afterwards — `discord_id` above all, also
  `player_name`, `cwl_permanent_optout` — must be re-resolved from `user_players` on read.
  `get_player_links_sync(tags)` is the batched, chunked helper for exactly this; it already applies
  the verified-wins + UNASSIGNED tiebreak and maps `'UNASSIGNED'` → `None`.
- `_build_enrollment_payload` already does this correctly for `th_level` (three-step fallback
  chain), `current_clan_tag` (player-scoped fallback) and `is_guest` (overridden outright whenever a
  live current clan is known). Copy that pattern; `discord_id` was simply never given it.
- In a first-non-`None`-wins merge, source order **is** the precedence rule. A live-data source
  added at the end to "fill gaps" silently cannot fix wrong values — if it is the authority, it must
  assign, not `or`.

**Also fix the WRITE boundary, not just the readers.** Defending every reader stops the
user-visible symptom, but the paths that *copy one snapshot into another* — a clan becoming
shared (`_migrate_local_clan_roster_to_shared`), a drag-and-drop placement (`_resolve_identity`),
the cross-guild mirror — were still laundering an outdated owner into `cwl_shared_clan_players`,
where the next feature to read that column would naturally trust it. Those now resolve through
`_live_owners_or_sync()` (`QBdiscocmdshelper_cwl.py`), which batches the lookup and falls back to
the recorded value only for a tag with no `user_players` row at all. Note the asymmetry, which is
deliberate: a *reader* treats a live None as authoritative (nobody owns this, don't DM anyone),
while a *carry-forward writer* falls back instead — so it can only ever correct an owner, never
erase the record of who was originally DMed.

General lesson: when a snapshot table and a live table both carry the same field, every read path
has to make an explicit, documented choice about which wins — and "whichever source the merge loop
happened to visit first" is not that choice. Grep for *all* readers of the snapshot field before
concluding a fix is complete: this one had four (board payload, DM targeting, the DM button's
ownership guard, and the upsert that wrote the stale value straight back). Then grep the *writers*
too — a defended reader still leaves the bad data spreading, and the next person to add a reader
starts from a column that looks trustworthy and isn't.

## Pitfall 38: `asyncio.StreamReaderProtocol` + `loop.connect_read_pipe(..., sys.stdin)` crashes instantly on Windows for any host that gives the child a non-overlapped stdin pipe

**Symptom (2026-08-22, live report):** the tracker MCP server (`qapbot/mcp/tracker_mcp.py`,
`.vscode/mcp.json`) never produced any tools in VS Code Copilot Chat on Windows — no error
visible from the chat side, it simply looked like the tools didn't exist. Spawning the server
directly and feeding it an `initialize` request reproduced it: the process died before writing
any response, with `OSError: [WinError 6] Invalid handle` inside
`ProactorEventLoop._register_with_iocp` → `CreateIoCompletionPort`.

**Why:** `run_stdio_server()` used `loop.connect_read_pipe(lambda: protocol, sys.stdin)` to get
an async line reader. On Windows, `asyncio.run()`'s default loop is `ProactorEventLoop`, and
`connect_read_pipe` registers the pipe handle with an I/O Completion Port — which requires the
handle to have been created with `FILE_FLAG_OVERLAPPED`. An anonymous pipe a parent process
hands a child as stdin (Node's `child_process.spawn` on Windows, Python's own `subprocess.Popen`
via `_winapi.CreatePipe`) is **not** overlapped by default, so the registration fails outright —
every time, not intermittently. This has nothing to do with the tracker/bridge logic itself
(`handle_request()` was already correct and fully unit-tested); it's purely a transport-layer
bug that only manifests on Windows hosts, which is why it could ship unnoticed if the server was
only ever tested from a non-Windows Claude Code CLI session.

**Fix:** don't use `connect_read_pipe` for stdin at all. Read lines with a plain blocking
`sys.stdin.readline()` executed in a thread-pool executor each loop iteration
(`await loop.run_in_executor(None, sys.stdin.readline)`) — this works identically on every
platform and every kind of stdin handle (overlapped or not, console or pipe or redirected file),
at the cost of one extra thread instead of native proactor I/O, which is irrelevant for a
line-at-a-time JSON-RPC server. `_write_message()`'s plain synchronous `sys.stdout.write()` needed no change *here* — only the
*read* side goes through the proactor pipe API. (It turned out to have its own, separate bug —
unconfigured encoding, not transport — fixed later; see Pitfall 40 below.)

**How to apply:** any hand-rolled stdio-transport MCP/JSON-RPC server in this repo must avoid
`loop.connect_read_pipe`/`loop.connect_write_pipe` on `sys.stdin`/`sys.stdout` — use the
executor-thread `readline()` pattern above instead. If you're debugging "an MCP server produces
no tools in some client but the tool-call logic unit-tests fine," reproduce it by spawning the
server directly (`subprocess.Popen([...], stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True)`) and
sending it a raw `initialize` line by hand — a transport-layer crash shows up immediately in
`stderr`, whereas the calling host (VS Code, Claude Code) usually just silently shows zero tools
with no visible error.

## Pitfall 39: a raw Discord gateway event fires for every bot in the channel, not just the one whose message it's about — "DEV never sees a matching row" doesn't generalize from button clicks to reactions

**Symptom (2026-08-22, live report):** DEV reacted to an admin's 👍 on a test-case message that
PROD had posted, in a channel/guild both bots share for testing. DEV proceeded to mark that
tracker item's environments passed in its own DB, then logged `[TRACKER] Failed to refresh
test-case message for item #11: 403 Forbidden (error code: 50005): Cannot edit a message
authored by another user`, and still posted its own "mark item done too?" prompt into the
thread — visibly, since the mention fix landed in the same deploy (see the changelog).

**Why:** `qapbot/ui_tracker.py`'s bug/feature tracker is meant to be PROD-only —
`CONFIG.tracker_enabled` is hard-coded `not is_dev_mode` specifically so DEV never registers
`/bug`/`/feature` (see `BUG_FEATURE_TRACKER.md`'s "Architecture at a glance"). The 👍-reaction
sign-off shortcut is wired as `on_raw_reaction_add` in `QapBot.py`, registered unconditionally
under the assumption that it'd be a harmless no-op on DEV — reasoning copied from the adjacent
`DynamicItem` button registration, which really is safe on DEV: a component **interaction**
(button click) is delivered by Discord only to the bot application that owns the message, so
DEV's own copy of a `TrackerTestPassButton` etc. is simply never invoked for a PROD-authored
message. A **raw reaction add event is not scoped that way** — it fires for every bot present in
the channel/guild regardless of which bot's message got the reaction, because it's a statement
about the channel's state, not an interaction routed to a specific application. Combined with
DEV's tracker DB being a routine copy of a PROD backup (a project-standard workflow, not a
misconfiguration), DEV's `get_tracker_item_by_test_message_id()` lookup found a real row and
proceeded to act on it — the "no tracker message ever exists there" assumption was true for
`/bug`/`/feature`-created items but false the moment ANY raw gateway event touches tracker state.

**Fix:** `handle_tracker_test_reaction()` now checks `CONFIG.tracker_enabled` as its very first
line, before even checking the emoji — the gate belongs in the one function actually reachable
from the listener, not only (or instead of) the registration site, so it holds regardless of
future callers.

**How to apply:** before assuming a Discord listener is "safe" on DEV because an analogous one
is, check WHICH KIND of event it is. Component interactions (buttons, select menus, modals) and
slash commands are scoped by Discord to the owning bot application — safe by construction.
Anything based on raw gateway events over shared channel state — `on_raw_reaction_add`/`_remove`,
`on_message`, `on_raw_message_edit`/`_delete`, presence/typing events — fires for every bot
present, regardless of which bot the content belongs to, and must carry its own explicit
`CONFIG.tracker_enabled` (or equivalent PROD-only) check if it touches tracker (or any other
PROD-only) state. Don't reuse "DEV never receives this" reasoning across event types without
verifying it actually applies to the specific event you're adding.

## Pitfall 40: an unconfigured `sys.stdin`/`sys.stdout` on Windows silently mangles non-ASCII MCP traffic, and can crash the server outright on the write side

**Symptom (2026-08-22, live report):** test cases posted through `tracker_add_testcases` on item
#0014 came out as mojibake in the DB — an em-dash stored as `Ã¢â‚¬"`, a `▸` as `Ã¢â€“Â¸`. The
bridge and DB were innocent; the text was already corrupt by the time it left the MCP server
(`qapbot/mcp/tracker_mcp.py`).

**Why:** `run_stdio_server()` reads with a bare `sys.stdin.readline()` (Pitfall 38's fix) and
`_write_message()` writes with a bare `sys.stdout.write()`. Neither call sets an encoding, so on
Windows Python falls back to `locale.getpreferredencoding(False)` — cp1252 on a typical
dev/PROD host. The MCP client always sends UTF-8. Decoding UTF-8 bytes as cp1252 reproduces the
mojibake exactly (`"—"` → UTF-8 `e2 80 94` → read as cp1252 → `"Ã¢â‚¬"`). Worse on the write
side: cp1252 cannot *encode* most of these characters at all, so a tool response containing one
raises `UnicodeEncodeError` inside `sys.stdout.write()` instead of producing garbage — taking the
whole server down mid-session. The calling chat client just sees the tools stop working, the same
silent-failure mode as Pitfall 38.

**Fix:** force UTF-8 on both streams at process startup, independent of locale —
`sys.stdin.reconfigure(encoding="utf-8", errors="replace")` /
`sys.stdout.reconfigure(encoding="utf-8")` in `main()`, before `asyncio.run(run_stdio_server())`.
`errors="replace"` on the read side means a genuinely malformed byte degrades to `�` instead of
killing the read loop. Setting `PYTHONUTF8=1` in the launch config would also work but is easy to
lose when the server is spawned from a different host/config, so pinning it in code is safer.

**How to apply:** any hand-rolled stdio-transport server in this repo (MCP or otherwise) must
explicitly set UTF-8 on `sys.stdin`/`sys.stdout` at startup rather than trusting the platform
default — Windows' locale-codepage fallback is not UTF-8 by default and this class of bug is
invisible on Linux/macOS dev machines where the locale usually already is UTF-8, so it can ship
unnoticed until a Windows host or a non-ASCII payload hits it.

## Pitfall 41: a Discord View callback that `defer()`s (or otherwise awaits) before performing its side effect is re-entrant — a second click landing before the first finishes re-runs the action

**Symptom (2026-08-23, two live reports):** tracker item #0026 — rapid double-clicking a
"Report an item" draft's Submit button created two identical tracker items. tracker item #0036 —
double-clicking "Yes, mark done" on the "mark linked item done too?" prompt (`ui_tracker.py`'s
`ConfirmItemDoneView`) sent the reporter five identical "passed verification and is now done!"
DMs — one per click that landed before the view's buttons visually disappeared.

**Why:** every affected callback followed the same shape: check permission (sync, no await) →
`await interaction.response.defer(...)` → perform the state-mutating action (create a DB row,
call `apply_status_change()`, etc.) → edit the message to show buttons gone. The `defer()` call
is itself an `await` — it yields control back to the event loop — and discord.py dispatches each
incoming interaction as its own task, so a second click's callback can start running (and pass
the same permission check) before the first click's `defer()` even returns, let alone before the
first click's action or its final "buttons removed" edit completes. The visible "buttons are
still there" window during that first `await` is exactly the window a rage-click lands in.
`apply_status_change()` (and most of the tracker's action functions) aren't written to be
idempotent against being called twice for the same logical action — they unconditionally re-DM,
re-insert, etc. — so nothing downstream catches the duplicate either.

**Fix:** for a short-lived, session-scoped `discord.ui.View` (held in memory across its own
clicks until `.stop()` — draft previews, Yes/No confirms, single-use selects), guard with a flag
set as the literal first synchronous statement of the handler, *before* any `await` — checking
and setting it is then atomic against a second click's callback starting mid-way through the
first, since nothing yields the event loop in between. Respond to the interaction by disabling
every child and calling `interaction.response.edit_message(view=view)` directly (not
`defer()`-then-edit-separately) — this is both the guard's synchronous side (buttons visibly gone
in the same response) and the fastest way to make a rage-click's later hits land on a
already-disabled control. `ui_tracker.py`'s `_consume_once(view, interaction)` helper implements
exactly this and is shared by `ConfirmItemDoneView`, `ConfirmForceMoveView`, and
`TrackerStatusSelectView`; `TrackerDraftView._on_submit` predates the helper and inlines the same
pattern under its own `self.submitted` flag name.

For a **persistent** button (a `discord.ui.DynamicItem`, reconstructed as a fresh Python object
from its `custom_id` on every single click — Edit/Add files/Status/Test cases/Grant access on a
posted tracker item, the Pass/Fail/Move-to-Done buttons) there is no `self` state that survives
between clicks, so the flag-before-await trick doesn't apply. Guard these instead with a
persisted-state idempotency check — re-fetch the relevant DB state before acting and short-circuit
if a previous click (even one still in flight) already recorded it, the way
`TrackerItemButton._invite_requestor` checks `item["access_grant_pending"]` before creating
another one-time invite. This narrows the race to the (much smaller, rarely hit in practice) window
where two clicks' DB reads both land before either click's write — good enough for the realistic
"user re-clicked because nothing visibly happened yet" case #0026/#0036 both were, as opposed to
requiring a real lock for genuinely simultaneous concurrent clicks.

**How to apply:** any new tracker (or other Discord UI) View callback that performs a
side-effecting action — anything that writes to the DB, sends a DM, or otherwise isn't safe to
run twice for one logical user action — needs one of these two guards depending on whether its
component is session-scoped (`self`-state flag via `_consume_once()`) or a persistent
`DynamicItem` (persisted-state re-check). A bare `check permission → defer() → act` shape with
neither is the exact pattern that shipped both #0026 and #0036.

## Pitfall 42: `interaction.followup.send(view=...)` rejects an explicit `None` — only `edit_message()`/`edit_original_response()` treat `None` as "no view"

**Symptom (2026-08-23, live report, found while re-testing #0036's fix):** clicking "Move to
Done" on a test-case set whose linked tracker item was already `done` crashed with
`TypeError: expected view parameter to be of type View or LayoutView, not NoneType` inside
`discord/webhook/async_.py`'s `send()`, surfacing as "Ignoring exception in dynamic item
callback for TrackerTestMoveDoneButton" in the logs — the button appeared to do nothing from the
user's side, no error visible in Discord.

**Why:** `_build_testcases_moved_message()` (`qapbot/ui_tracker.py`) returns `view=None`
whenever the linked item isn't eligible for a "mark it done too?" prompt (already terminal, or
no linked item at all) — a legitimate, common case, not an error state.
`_send_testcases_moved_followup()` passed that straight through:
`interaction.followup.send(text, view=view, ephemeral=True)`. discord.py's `Webhook.send()`
(and `Interaction.followup.send()`, which wraps it) types `view` as `View = MISSING` — a bare
type, not `Optional[View]` — so `None` is simply not an accepted value; only a real
`View`/`LayoutView` instance or omitting the parameter entirely (the `MISSING` sentinel) works.
This is *inconsistent* with `interaction.edit_original_response()` / `response.edit_message()`,
which type the same parameter as `Optional[Union[View, LayoutView]] = MISSING` specifically so
`None` can mean "clear the existing view" — an easy trap when a function like
`_build_testcases_moved_message()` is shared between a `.send()` call site
(`_send_testcases_moved_followup`) and an `.edit_original_response()` call site
(`_edit_to_testcases_moved_message`), since only one of the two call sites is actually broken by
the same `view=None` return value.

**Fix:** translate `None` into `discord.utils.MISSING` at any `.send()`/`followup.send()` call
site whose `view` can be `None`: `view=view if view is not None else discord.utils.MISSING`. An
equally valid alternative already used elsewhere in this file (`_send_testcase_chunks()`) is a
ternary on the whole call: `await x.send(content, view=view) if view else await x.send(content)`.

**How to apply:** before passing a `view=` (or `embed=`, `embeds=`, `file=`, etc. — the same
`MISSING`-not-`None` convention applies to most discord.py webhook/interaction kwargs) that
might be `None` into any `channel.send()` / `interaction.followup.send()` / `webhook.send()`
call, either guard it into `discord.utils.MISSING` or branch the call entirely. Don't assume
"`None` means omitted" carries over from an edit-style call (`edit_message`/
`edit_original_response`, where it's true and deliberate) to a send-style call (where it isn't)
just because the same variable is reused between the two.


## Pitfall 43: assuming a renamed persistent view's `custom_id`s need a dedicated repair pass — check whether the STARTUP repost is already unconditional first

**Symptom (2026-08-23, found while implementing plans/cwl-personal-hub.md's Phase 2d):**
renaming `CwlManagementHubView`'s three `custom_id`s (`cwl_hub_mode_settings` etc. →
`cwl_admin_hub_mode_settings` etc., to retire a prefix collision with an unrelated feature's own
config columns) looked like it needed a one-time forced-repost repair with a persisted per-guild
"have I migrated this guild yet" marker — reasoning by analogy from the *periodic* maintenance
repost call (`QapBot.py`'s `main()`, `only_if_not_bottom=True`), which genuinely does skip a
message that's already the newest one in its channel and could in principle leave a stale
`custom_id` unfixed indefinitely.

**Why the analogy was wrong:** `repost_anchored_message()`'s `only_if_not_bottom` parameter
defaults to `False`. The *startup* repost call — `on_ready()`'s numbered `INIT-STEP-8b` (and
now `8c`) — calls e.g. `repost_cwl_management_messages()` with **no** `only_if_not_bottom`
argument at all, i.e. an **unconditional** repost, once per bot process start. Since a bot
restart is exactly what a real deploy already is for this project, any custom_id (or other
anchored-message content) change already self-heals on the very next restart with zero extra
code — the "could live stale indefinitely" risk only applies to a bot that runs forever without
ever restarting, which isn't this project's actual deployment model.

**Fix:** none needed for the case that prompted this — verified by reading the exact startup
call site (not inferred from the periodic call's behavior) and documenting the reasoning inline
in `CwlManagementHubView`'s own docstring (`qapbot/ui_cwl_roster.py`) so a future reader doesn't
have to re-derive it.

**How to apply:** before adding a one-time migration/repair mechanism (a persisted "already
migrated" marker, an extra forced-repost call, etc.) for *any* anchored-message content or
component change, check whether `on_ready()`'s own startup repost for that message already runs
unconditionally (no `only_if_not_bottom` argument, or `only_if_not_bottom=False`) — if so, the
change is already self-healing on the next deploy, and a repair pass would just be redundant
code with no behavioral benefit (or worse, a second repost racing the startup one in the same
init sequence). Only build a real repair mechanism when the *periodic* maintenance path is the
only thing that would ever touch that guild's message again (e.g. a guild that never gets a
fresh bot restart for a long time in a deployment model where that's actually possible).
