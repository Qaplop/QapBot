# Copilot Instructions for QapBot (Playbook)

This file is intentionally short and high-signal so Copilot can reliably apply it.
Deep dives and long examples live in the docs referenced below.

Token budget note (cl100k_base): keep this file ~≤3000 tokens.

---

## 🔴 CARDINAL RULES (Override Everything)

### 1) Account protection: never bypass security
- DO: Check `get_verified_player_owner()` and `get_any_player_owner()` before linking/re-linking.
- DON'T: Remove verified accounts without explicit bot-admin confirmation.
- WHY: Prevent account hijacking.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Account Protection Architecture

### 2) Single source of truth: CACHE only
- DO: Use `CACHE.*` for all runtime data.
- DON'T: Create shadow dicts/lists outside CACHE.
- WHY: Prevent data divergence.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Cache Management

### 3) Search before creating
- DO: Use grep_search / semantic_search before adding new helpers.
- DON'T: Create near-duplicates.
- WHY: Avoid inconsistent behavior.

### 4) Environment awareness: DEV vs PROD
- DO: Confirm environment before any file ops.
- PROD bot root: `<PROD_BOT_ROOT>` (set via `PROD_BOT_ROOT` in your environment)
- PROD data/archive (eSATA SSD): `${PROD_DATA_DIR}/data` (set via `PROD_DATA_DIR` in your environment)
- PROD SSD from Windows (UNC): `<PROD_SSD_UNC>` — **⚠️ NEVER access without explicit user confirmation. This is the live production database.**
- DO: Get explicit user confirmation before any read/write/list/search access under PROD paths.
- DON'T: Hardcode machine-specific absolute paths.
- WHY: Avoid corrupting prod data.
📖 Details: ../README.md § Deployment Locations

### 5) i18n: no hardcoded user-facing strings
- DO: Use `t('key', ...)` for Discord-visible text.
  - Ephemeral/DM: pass both `user_id=str(interaction.user.id)` and `guild_id`.
  - Public: pass `guild_id`.
- DON'T: Hardcode English in UI/commands.
- WHY: Multi-language support.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Discord.py Patterns

### 6) Discord interaction lifecycle
- DO: Modal submit: `await interaction.response.defer(...)` then `await interaction.followup.send(...)`.
- DON'T: Call `response.send_message()` after a response is consumed.
- WHY: Prevent “interaction already responded”.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Discord Interaction Pitfall

### 7) Select dropdown persistence
- DO: When rebuilding selects, set `default=True` on the selected option.
- DON'T: Expect a `default_values` API for string selects.
- WHY: Avoid selection reset after edits.

### 8) External API calls through cache_manager only
- DO: All Discord/CoC API calls go via cache_manager wrappers / cache objects.
- DON'T: Call Discord/CoC APIs directly from business logic/modules.
- WHY: Central rate limiting, caching, boundaries.
📖 Details: ../qapbot/docs/RATE_LIMITING_IMPLEMENTATION.md

### 9) discord.py modal pattern
- DO: `class MyModal(discord.ui.Modal, title="..."):` + TextInput as class attributes.
- DON'T: Pass `title=` to `super().__init__()` or create TextInputs in `__init__`.
- WHY: Required by discord.py lifecycle.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Discord.py Patterns

### 10) Database access through db_manager only
- DO: Use `CACHE.db_manager.*`.
- DON'T: Raw SQL / sqlite access in runtime modules.
- WHY: Central abstraction + error handling.
📖 Details: ../qapbot/docs/DATABASE_ARCHITECTURE.md

### 11) DB operations must be idempotent
- DO: Use `INSERT OR IGNORE`, `CREATE TABLE IF NOT EXISTS`, etc.
- DON'T: Write migrations that fail on re-run.
- WHY: Safe recovery and reruns.
📖 Details: ../qapbot/docs/DATABASE_ARCHITECTURE.md § Migration Principles

### 12) Documentation references: plain text paths only
- DO: Use `../path/to/file.md § Section`.
- DON'T: Markdown links inside docs.
- WHY: Avoid markdown lint/format drift.

### 13) DB rows: ALWAYS use named column access
- DO: Set `conn.row_factory = aiosqlite.Row` (async) or `conn.row_factory = sqlite3.Row` (sync). Access rows as `row["column_name"]`.
- DO: Alias aggregate columns: `SELECT COUNT(*) AS cnt` → `row["cnt"]`.
- DON'T: **EVER** use `row[0]`, `row[1]`, `row[N]` positional index on DB rows.
- WHY: ALTER TABLE migrations append columns at the end of the physical storage order, not the CREATE TABLE definition order. Positional indices silently read the **wrong column** with no error — this caused `coc_role_enabled` to always read `created_at` (a non-empty string) and return `True` on every bot restart, re-activating disabled features.
  ```python
  # WRONG – breaks silently after any ALTER TABLE migration:
  "coc_role_enabled": bool(row[12])  # was actually reading created_at!
  # CORRECT – immune to schema evolution:
  "coc_role_enabled": bool(row["coc_role_enabled"])
  ```

---

## Quick reference (most common)

- User-facing text → `t('key', user_id=..., guild_id=...)`
- CoC clan fetch → `await CACHE.coc_clan_cache.get_clan(clan_tag)`
- CoC war fetch → `await CACHE.get_current_war_from_api(clan_tag)`
- DB read/write → `await CACHE.db_manager.*` (never raw sqlite)
- War file lifecycle → see ../qapbot/docs/WAR_FILE_MANAGEMENT_RULES.md

---

## Workflow guardrails

- Prefer minimal diffs and reuse existing patterns.
- Preserve unknown keys when updating JSON-like dict structures in CACHE (avoid data loss).
- Multi-step UI flows: prefer editing an existing message; avoid leaving behind many ephemeral messages.

---

## Common pitfalls (short)

All pitfalls: short snippets + details in ../qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md

1) Dropdown resets after edit → rebuild select; set `default=True` on selected option.
2) Workflow message clutter → prefer `edit_message()` single-message flows; cleanup old responses.
3) Hardcoded English → always use `t()`; ephemeral/DM must pass both `user_id` and `guild_id`.
4) Interaction already responded → modal submit: defer then followup; buttons/selects: check `is_done()`.
5) Duplicate helpers → search with grep_search / semantic_search before adding.
6) Missing context/flags → thread `guild_id`, `user_id`, admin flags, and CONFIG values through signatures.
7) Losing fields in CACHE dicts → update in-place or preserve unknown keys when rebuilding.
8) Thinking indicator stuck → use `thinking=False` when only editing; `thinking=True` only with followup.
9) Raw sqlite in runtime modules → DB access only via `CACHE.db_manager.*`.
10) Positional DB row access (`row[0]`) → NEVER — use `row["column_name"]`; ALTER TABLE shifts indices silently (caused `coc_role_enabled` to always read `created_at` → always True). Use `conn.row_factory = aiosqlite.Row` / `sqlite3.Row`. Alias aggregates: `SELECT COUNT(*) AS cnt` → `row["cnt"]`.
11) New column via ALTER TABLE stays NULL for up to 12 h → add the field name to `_ESSENTIAL_CLAN_FIELDS` in `fetch_clan_war_data()` (`QBhelperfunctions.py`); the bypass forces an immediate backfill fetch for any cache entry missing it. **Exception: `is_deleted` does NOT belong in `_ESSENTIAL_CLAN_FIELDS`** — its safe default is `False` (not deleted); it is managed by the deletion detection system.
12) Phase-1 skip logic — always preserve BOTH guards in order: `track_war_updates=False → skip`, then `is_deleted=True → skip`. A subscribed clan can have `track_war_updates=True` and `is_deleted=True` simultaneously (Supercell can delete any clan).

---

## Deep-dive references (use these instead of long examples here)

- Architecture + patterns: ../qapbot/docs/CODE_STRUCTURE.md
- DB schema + migration rules: ../qapbot/docs/DATABASE_ARCHITECTURE.md
- Rate limiting + parallel pipeline: ../qapbot/docs/RATE_LIMITING_IMPLEMENTATION.md
- Welcome/registration flows: ../qapbot/docs/REGISTRATION_MESSAGE_WORKFLOWS.md
- War temp/archive lifecycle: ../qapbot/docs/WAR_FILE_MANAGEMENT_RULES.md

---

## Implementation Workflow

### Before Writing Code
1. **Search for existing functionality** (grep_search, semantic_search)
2. **Read relevant module docs** (../qapbot/docs/CODE_STRUCTURE.md for responsibilities)
3. **Identify data flow** (what CACHE properties are involved?)
4. **Check environment** (dev vs prod paths if file operations)
5. **Plan rollback** (what to revert if change fails?)

### While Writing Code
- Use CACHE for ALL runtime data
- Use `t()` for ALL user-facing text
- Follow discord.py patterns (Modal titles, TextInput as class attrs)
- Check account ownership before linking operations
- Add type hints and docstrings for new functions
- Log with appropriate level (DEBUG for verbose, INFO for actions)

### After Writing Code
- Test with validation: Does it solve the problem?
- Check integration: Do dependent functions still work?
- Verify cache consistency: Are save/load operations paired?
- Test error paths: Are failures handled gracefully?
- Update documentation if architecture changed
- **Run tests with:** `.\run_tests.ps1` — NEVER construct a raw pytest command.
  Pass extra filters as trailing args: `.\run_tests.ps1 -k my_test` or `--lf`.
  The script encodes the canonical deselects; calling it directly keeps the
  command identical every run so VS Code auto-approves without prompting.

---

## QapBot-Specific Patterns

### Cache Management
```python
# ALWAYS use CACHE object
clan_name = CACHE.clan_name_cache.get(clan_tag, "Unknown")
CACHE.user_accounts[user_id] = {"players": [...]}
await CACHE.persist_user(user_id)  # Write-through to database

# NEVER create shadow data structures
# DON'T: clan_names = {}  # Wrong!
```

### CoC API Calls
```python
# Use cached clan data to reduce API calls
clan = await CACHE.coc_clan_cache.get_clan(clan_tag)

# Direct API calls for fresh data (DEPRECATED - use CACHE instead)
# clan = await QBcore.coc_client.get_clan(clan_tag)
```

### Player Registration
```python
# Check ownership before linking
previous_owner = get_any_player_owner(player_tag, requesting_user_id)
if previous_owner:
    verified_owner = get_verified_player_owner(player_tag, requesting_user_id)
    if verified_owner:
        # Requires admin override
        show_admin_override_dialog()
    else:
        # Requires confirmation
        show_confirmation_dialog()
```

### Message Deletion Cleanup
```python
# Always wrap in try/except
try:
    await interaction.delete_original_response()
except discord.NotFound:
    pass  # Message already deleted
except Exception as e:
    logging.warning(f"Failed to delete message: {e}")
```

### Discord.py Modal Pattern
```python
# CORRECT - Title in class definition, TextInput as class attribute
class MyModal(discord.ui.Modal, title="My Modal Title"):
    my_input = discord.ui.TextInput(
        label="Input Label",
        placeholder="Placeholder text"
    )
    
    def __init__(self, guild_id=None):
        super().__init__()  # NO title parameter!
        # Translate placeholders AFTER super().__init__()
        self.my_input.placeholder = t('key', guild_id=guild_id)
    
    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        value = self.my_input.value
        # ... process ...
        await interaction.followup.send("Done", ephemeral=True)
```

---

## Code Quality Standards

### Type Hints and Documentation
- **ALWAYS provide comprehensive type hints** for new functions
- **Include parameter descriptions** in docstrings
- **Document return types** and exceptions
- **Provide usage examples** for complex functions
- **Use specific types** (`List[Dict[str, Any]]` not just `list`)

### Comment Preservation
- **DON'T remove or rewrite existing comments** unless explicitly requested
- **Favor minimal diffs**: Insert new code without touching surrounding comments
- **If comment becomes outdated**: Add "OUTDATED" prefix and new comment below
- **Preserve context**: Comments often explain non-obvious design decisions

### Error Handling
- Wrap Discord API calls in `try/except` for NotFound, Forbidden, HTTPException
- Use `discord_retry()` wrapper for rate limit handling
- Log errors with context (user ID, clan tag, etc.)
- Return graceful error messages to users via `t()`

---

## Changelog Management

**When implementing features or fixes, document in `backlog.txt`:**

- **Location**: Under `Change-Log:` section (not at top)
- **Format**: Date-based (YYYY-MM-DD)
- **Content**: Concise description (Fixed/Added/Modified/Removed), root cause, implementation details, impact
- **Documentation Updates**: Update `copilot-instructions.md` and `../qapbot/docs/CODE_STRUCTURE.md` together with `changelog.txt` if architecture changed

**Example**:
```
Change-Log:
===========
2026-01-03
- Refactored: Copilot instructions optimized for token efficiency
  - Reduced from 8,000 to 4,000 tokens via consolidation and referencing
  - Moved detailed architecture to CODE_STRUCTURE.md
  - Added Cardinal Rules section for priority focus
  - Result: 2x effectiveness, AI processes entire file
```

---

## Additional Resources

📖 **../qapbot/docs/CODE_STRUCTURE.md**: Module responsibilities, data flows, function trees, architecture details  
📖 **../qapbot/docs/DATABASE_ARCHITECTURE.md**: SQLite schema, migration history, backup/recovery, performance  
📖 **../qapbot/docs/COC_GAME_MECHANICS.md**: CWL round schedule, regular war states, passive/inactive tracking, warlog visibility  
📖 **../README.md**: Installation, environment variables, commands, deployment, features  
📖 **../qapbot/docs/REGISTRATION_MESSAGE_WORKFLOWS.md**: Player registration and notification workflows  
📖 **../backlog.txt**: Feature roadmap
📖 **../changelog.txt**: Changelog

**When in doubt**: Search the codebase first, reference documentation second, ask for clarification third.


