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
