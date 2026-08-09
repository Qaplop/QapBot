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
# Preferred: single-message flow
await interaction.response.edit_message(content="Step 2", view=StepTwoView())

# If you need a new ephemeral message, clean up old response when possible
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

See also: ../qapbot/docs/CODE_STRUCTURE.md § Discord Interaction Pitfall

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
