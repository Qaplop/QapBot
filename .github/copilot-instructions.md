# Copilot Instructions for QapBot (Playbook)

This file is intentionally short and high-signal so Copilot can reliably apply it.
Deep dives and long examples live in the docs referenced below.

Token budget note (cl100k_base): keep this file ~≤3000 tokens.

---

## 🔴 CARDINAL RULES (Override Everything)

### 1) Hot/history schema parity: NEVER let `main` and `history` schemas drift
- DO: Whenever you `ALTER TABLE main.x ADD COLUMN ...` (or otherwise change a table's schema), apply the byte-for-byte identical change to `history.x` in the **same commit**, in the same order, immediately.
- DO: Any cross-schema copy (`INSERT INTO history.x (...) SELECT (...) FROM main.x`) MUST list columns explicitly by name on both sides.
- DON'T: **EVER** write a bare `SELECT *` (or `INSERT INTO x SELECT *`) that copies rows from one schema to the other.
- DON'T: Assume identical `CREATE TABLE` text in code means two existing tables share a physical column layout. `CREATE TABLE IF NOT EXISTS` never retroactively reorders an already-existing table — so two schemas whose columns were added via different `ALTER TABLE ADD COLUMN` sequences at different points in time can have the same column *names* sitting at completely different physical positions (`cid`), even when their CREATE TABLE source text matches today. Verify with `PRAGMA main.table_info(x)` vs `PRAGMA <history-db>.table_info(x)` (opened as a second connection) if there's ever any doubt — never assume from source text alone.
- WHY: A 2026-08-14 incident found `main.war_attacks`/`war_summary` and `history.war_attacks`/`war_summary` had silently diverged exactly this way. The monthly hot→history migration (`_migrate_table_batch_by_date`/`_migrate_cwl_table_by_season` in `db_manager.py`) used `SELECT *`, which matches columns by *position*, not name — so every row that ever migrated to history landed roughly a dozen values in the wrong columns (including `stars`, `date`, and `war_tag`) with **no error at any point**, for the entire lifetime of the history DB. This is the same root mechanism as Rule 14's positional-row-access incident below (`ALTER TABLE` appends at the end of physical storage, not the `CREATE TABLE`'s logical position) — that incident already proved this exact pitfall existed for one symptom; this one is the same pitfall for a different symptom (cross-schema bulk copy instead of app-code row access), so the lesson needed generalizing, not just patching once.
📖 Details: ../qapbot/docs/DATABASE_ARCHITECTURE.md § Hot/History Schema Parity Incident (2026-08-14)

### 2) Account protection: never bypass security
- DO: Check `get_verified_player_owner()` and `get_any_player_owner()` before linking/re-linking.
- DON'T: Remove verified accounts without explicit bot-admin confirmation.
- WHY: Prevent account hijacking.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Account Protection Architecture

### 3) Single source of truth: CACHE only
- DO: Use `CACHE.*` for all runtime data.
- DON'T: Create shadow dicts/lists outside CACHE.
- WHY: Prevent data divergence.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Cache Management

### 4) Search before creating
- DO: Search the codebase for an existing implementation before adding a new helper (use whatever search tool your environment provides — grep/ripgrep, semantic search, etc.).
- DON'T: Create near-duplicates.
- WHY: Avoid inconsistent behavior.

### 5) Environment awareness: DEV vs PROD
- DO: Confirm environment before any file ops.
- PROD bot root: `<PROD_BOT_ROOT>` (set via `PROD_BOT_ROOT` in your environment)
- PROD data/archive (eSATA SSD): `${PROD_DATA_DIR}/data` (set via `PROD_DATA_DIR` in your environment)
- PROD SSD from Windows (UNC): `<PROD_SSD_UNC>` — **⚠️ NEVER access without explicit user confirmation. This is the live production database.**
- DO: Get explicit user confirmation before any read/write/list/search access under PROD paths.
- DON'T: Hardcode machine-specific absolute paths.
- WHY: Avoid corrupting prod data.
📖 Details: ../README.md § Deployment Locations

### 6) i18n: no hardcoded user-facing strings
- DO: Use `t('key', ...)` for Discord-visible text.
  - Ephemeral/DM: pass both `user_id=str(interaction.user.id)` and `guild_id`.
  - Public: pass `guild_id`.
- DON'T: Hardcode English in UI/commands.
- WHY: Multi-language support.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Discord.py Patterns

### 7) Discord interaction lifecycle
- DO: Modal submit: `await interaction.response.defer(...)` then `await interaction.followup.send(...)`.
- DON'T: Call `response.send_message()` after a response is consumed.
- WHY: Prevent “interaction already responded”.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Discord Interaction Pitfall

### 8) Select dropdown persistence
- DO: When rebuilding selects, set `default=True` on the selected option.
- DON'T: Expect a `default_values` API for string selects.
- WHY: Avoid selection reset after edits.

### 9) External API calls through cache_manager only
- DO: All Discord/CoC API calls go via cache_manager wrappers / cache objects.
- DON'T: Call Discord/CoC APIs directly from business logic/modules.
- WHY: Central rate limiting, caching, boundaries.
📖 Details: ../qapbot/docs/RATE_LIMITING_IMPLEMENTATION.md

### 10) discord.py modal pattern
- DO: `class MyModal(discord.ui.Modal, title="..."):` + TextInput as class attributes.
- DON'T: Pass `title=` to `super().__init__()` or create TextInputs in `__init__`.
- WHY: Required by discord.py lifecycle.
📖 Details: ../qapbot/docs/CODE_STRUCTURE.md § Discord.py Patterns

### 11) Database access through db_manager only
- DO: Use `CACHE.db_manager.*`.
- DON'T: Raw SQL / sqlite access in runtime modules.
- WHY: Central abstraction + error handling.
📖 Details: ../qapbot/docs/DATABASE_ARCHITECTURE.md

### 12) DB operations must be idempotent
- DO: Use `INSERT OR IGNORE`, `CREATE TABLE IF NOT EXISTS`, etc.
- DON'T: Write migrations that fail on re-run.
- WHY: Safe recovery and reruns.
- DO: Any DDL on a newly-migrated column (an index, a constraint, anything) belongs immediately after that column's own `_add_column_if_missing()` call in the migration block — never placed inline right after the table's `CREATE TABLE IF NOT EXISTS`.
- DON'T: Assume `CREATE TABLE IF NOT EXISTS` ever adds a column to an *existing* table — it's a guaranteed no-op there (the normal case on every DEV/PROD restart, not the exception), so DDL referencing a new column placed right after it will read "no such column" against any database that isn't brand new.
- WHY: A 2026-08-15 incident placed `CREATE INDEX ... ON cwl_signups(origin_shared_clan_id, ...)` right after `CREATE TABLE IF NOT EXISTS cwl_signups (...)`, instead of after the migration block's `_add_column_if_missing("cwl_signups", "origin_shared_clan_id", ...)` call further down. Against any already-initialized database the table already existed (so the CREATE TABLE was a no-op and the column genuinely didn't exist yet at that point), and the index statement threw `no such column` — aborting the **entire** database initialization, so the bot couldn't start at all. Fixed by deleting the inline index (an identical, correctly-placed one already existed after the migration call).
📖 Details: ../qapbot/docs/DATABASE_ARCHITECTURE.md § Migration Principles

### 13) Documentation references: plain text paths only
- DO: Use `../path/to/file.md § Section`.
- DON'T: Markdown links inside docs.
- WHY: Avoid markdown lint/format drift.

### 14) DB rows: ALWAYS use named column access
- DO: Set `conn.row_factory = aiosqlite.Row` (async) or `conn.row_factory = sqlite3.Row` (sync). Access rows as `row["column_name"]`.
- DO: Alias aggregate columns: `SELECT COUNT(*) AS cnt` → `row["cnt"]`.
- DON'T: **EVER** use `row[0]`, `row[1]`, `row[N]` positional index on DB rows.
- WHY: ALTER TABLE migrations append columns at the end of the physical storage order, not the CREATE TABLE definition order. Positional indices silently read the **wrong column** with no error — this caused `coc_role_enabled` to always read `created_at` (a non-empty string) and return `True` on every bot restart, re-activating disabled features. Rule 1 above is this same lesson applied to cross-schema bulk copies, not just single-row app-code access.
  ```python
  # WRONG – breaks silently after any ALTER TABLE migration:
  "coc_role_enabled": bool(row[12])  # was actually reading created_at!
  # CORRECT – immune to schema evolution:
  "coc_role_enabled": bool(row["coc_role_enabled"])
  ```

### 15) Keep docs current — a stale doc is a bug, not a formatting nit
- DO: When a change touches something already documented (in this file or `../qapbot/docs/*.md`), update that documentation in the **same** change — not as a follow-up.
- DO: When adding a significant feature or doing a significant refactor, add a new doc under `../qapbot/docs/` (or a new section in an existing one) if nothing already covers it.
- DO: When adding, removing, or changing the behavior/options of a `/` slash command, update its `/help` entry (`help()` in `QBdiscordcmds.py`) in the same change, plus the command list in `../README.md` if it's listed there.
- DON'T: Leave a doc referencing a file, convention, or location that the change just moved or renamed.
- WHY: This file's own Changelog Management section pointed at `backlog.txt` long after that convention moved to `changelog.txt` — a leftover from when both lived in one file that was later split, and nobody updated the doc when the split happened. A stale doc actively misleads (worse than no doc), and any tool/model following it reproduces the mistake as project practice.
📖 Where: cardinal rules/patterns → this file. Architecture deep-dives → `../qapbot/docs/*.md`.

### 16) Never commit real infrastructure details — placeholders only
- DO: Use the established placeholder convention for anything about the production
  environment: `<PROD_BOT_ROOT>`, `${PROD_DATA_DIR}`, `<PROD_SSD_UNC>`, `HOST-NAME`,
  "server-machine", etc. — see Rule 5.
- DON'T: Write a real hostname, IP address, UNC path, hardware/vendor/model name, or
  credential into any tracked file (docs, changelog, code comments, commit messages).
- OK to reference directly: in-game/Discord identifiers (clan tags/names, player tags,
  guild/channel IDs) — these are public data, not infrastructure, and don't need scrubbing.
- WHY: The project owner deliberately keeps hosting details out of the repo (which is or may
  become public) — that's *why* Rule 5's placeholders exist. Before syncing a batch of doc
  changes, a 2026-07-26 sweep confirmed no leaks had occurred, but it was a manual check with
  no rule backing it — this makes the expectation explicit instead of relying on catching it
  after the fact each time.
- Related editor-workflow risk (not a commit issue, but same "secrets out of the repo's
  reach" spirit): the Claude Code VS Code extension auto-attaches whatever file is currently
  focused in the editor to chat context, and as of 2026-08 this bypasses both `.claudeignore`
  and `settings.json` deny rules (tracked upstream: anthropics/claude-code#52419 — open,
  unfixed). Workspace-level exclusion doesn't work around this. Practical mitigation until
  it's fixed upstream: never leave `.env` (or any credential-bearing file) focused/open in a
  VS Code window where Claude Code chat is active — edit it in a separate editor/window, or
  use the terminal-only `claude` CLI for sessions touching this repo.

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
5) Duplicate helpers → search the codebase before adding (Cardinal Rule 4).
6) Missing context/flags → thread `guild_id`, `user_id`, admin flags, and CONFIG values through signatures.
7) Losing fields in CACHE dicts → update in-place or preserve unknown keys when rebuilding.
8) Thinking indicator stuck → use `thinking=False` when only editing; `thinking=True` only with followup.
9) Raw sqlite in runtime modules → DB access only via `CACHE.db_manager.*`.
10) Positional DB row access (`row[0]`) → NEVER — use `row["column_name"]`; ALTER TABLE shifts indices silently (caused `coc_role_enabled` to always read `created_at` → always True). Use `conn.row_factory = aiosqlite.Row` / `sqlite3.Row`. Alias aggregates: `SELECT COUNT(*) AS cnt` → `row["cnt"]`.
11) New column via ALTER TABLE stays NULL for up to 12 h → add the field name to `_ESSENTIAL_CLAN_FIELDS` in `fetch_clan_war_data()` (`QBhelperfunctions.py`); the bypass forces an immediate backfill fetch for any cache entry missing it. **Exception: `is_deleted` does NOT belong in `_ESSENTIAL_CLAN_FIELDS`** — its safe default is `False` (not deleted); it is managed by the deletion detection system.
12) Phase-1 skip logic — always preserve BOTH guards in order: `track_war_updates=False → skip`, then `is_deleted=True → skip`. A subscribed clan can have `track_war_updates=True` and `is_deleted=True` simultaneously (Supercell can delete any clan).
13) Deleting a tracked Discord message → wrap in try/except: `except discord.NotFound: pass` (already deleted, safe to proceed), `except Exception: log and skip this item` (never let cleanup crash the calling flow) — "skip" means don't fall through to whatever comes after a confirmed delete (e.g. posting a replacement, clearing tracking); an inconclusive delete + unconditional post-after is how you get duplicate messages (Pitfall 15).
14) `bot.get_channel()` returning `None` ≠ channel deleted → it's a cache-only lookup; a miss can be a transient gap. Before purging CACHE entries, confirm via `await bot.fetch_channel(id)` and only purge on `discord.NotFound` — `Forbidden`/`HTTPException`/network errors are inconclusive. See Pitfall 15.
15) `asyncio.to_thread()` doesn't help an atomic C call (`gc.collect()`, hashlib) — CPython only releases the GIL at bytecode boundaries, not mid-C-call. Reduce the call's scope instead (e.g. `gc.collect(1)`). A plain Python loop, by contrast, DOES get preempted — `await asyncio.sleep(0)` every N iterations works there. See Pitfall 16.
16) FK-integrity helpers (`_ensure_clan_exists()` etc.) on a `clan_tag`-named column → check the `CREATE TABLE` for a real `FOREIGN KEY ... REFERENCES clans` first; some `clan_tag` columns (`subscriptions`, `leaderboard_messages`) deliberately hold non-clan tags (player tags, family tags) with no FK. See Pitfall 17.
17) `normalize_clan_tag()` used as a first-resort classifier on freeform text → only prioritize it when input has an explicit leading `#`; otherwise try a name search first. See Pitfall 18.
18) Guild-coverage checks ("is this clan covered by this guild") → must expand BOTH `member_clans` AND `member_families` (family IDs → `CACHE.clan_families`), never just the flat list. See Pitfall 19.
19) `bot.add_view()`-registered views dispatch before `CACHE`/DB finish loading → a click in that window reads empty cache AND (via `update_user_metadata`'s skeleton-create + write-through) can hard-DELETE the user's real `user_players` rows (confirmed prod data loss 2026-08-08). Three-layer fix: view `interaction_check()` gates on `QBcore.bot.fully_initialized`; `CACHE.users_loaded` blocks all user write-through pre-load; `_save_user_impl` warns on empty-players wipes. See Pitfall 20.
20) Multi-second freezes with zero log output, followed by a burst of concurrent `[COC-API-SLOW]` lines all reporting ~the same elapsed time → that's CPython's *automatic* gen-2 GC sweep (never disabled; Pitfall 16's `gc.collect(1)` fix only scopes the bot's own *explicit* end-of-cycle call), not real API latency. Startup now registers a `[GC-AUTO]` pause logger and calls `gc.freeze()` after `CACHE.load_all()` to shrink what automatic sweeps walk. See Pitfall 21.
21) Adding module-level code near the top of `QapBot.py` that mutates a shared collection (`list.append`, `set.add`, registering a callback) → guard it by name, not identity (`if not any(getattr(cb, "__name__", None) == "my_fn" ...)`). `QBdiscordcmds.py` imports back from `QapBot.py`, which loads the whole file a second time under the module name `"QapBot"` (separate from the `__main__` execution) — a bare `.append()` silently ends up with two entries and double-fires. `def`/`class`/`import`/plain assignment are safe; anything that appends/registers into pre-existing shared state is not. See Pitfall 22.
22) RTL (Arabic/Hebrew) name mixed with other fields on one Discord embed line (`rank · player · clan → stars (info)`) → Discord merges the RTL name with everything up to the next unambiguous Latin text into ONE run and mirrors that whole span, not just the name. Unicode bidi isolates (FSI/PDI) do NOT work on Discord — confirmed by live testing, don't use them. Fix needs plain LRM (U+200E) TWICE: once bracketing the name (`normalize_player_name()` already does this) AND again as a bare mark at every field-transition boundary after the name (before ` · `, before ` → `, etc.), matching the proven `vs. [LRM name LRM](url) LRM TAG` pattern. See Pitfall 23.
23) Global command bulk sync on an app with Discord Activities enabled → enabling Activities auto-creates a global `PRIMARY_ENTRY_POINT` command that `discord.py` has zero model of. `tree.sync(guild=None)`/`tree.clear_commands(guild=None)` submit a bulk-overwrite payload that omits it, which Discord rejects outright (HTTP 400, error code 50240) instead of deleting it — this crashed PROD startup outright (uncaught exception in `setup_hook`) the first time Activities was enabled there. Use `bulk_sync_global_commands()` (`qapbot/discord_health.py`) instead of `tree.sync(guild=None)` for any *global* sync — it fetches existing global commands and always re-includes any Entry Point one. Guild-scoped syncs are unaffected. See Pitfall 25.

---

## Implementation Workflow

### Before Writing Code
1. **Search for existing functionality** (Cardinal Rule 4)
2. **Read relevant module docs** (../qapbot/docs/CODE_STRUCTURE.md for responsibilities)
3. **Identify data flow** (what CACHE properties are involved?)
4. **Check environment** (dev vs prod paths if file operations)
5. **Plan rollback** (what to revert if change fails?)
6. **Check `../backlog.txt` for opportunistic items in the file/function you're about to touch** —
   several entries there are deliberately-deferred fixes tagged "do this next time this code is
   touched anyway" (e.g. "switch to `executemany()` opportunistically next time the function is
   edited"). If your change already lands inside that file/function, fold the matching backlog
   item into the same change and remove it from `backlog.txt`; otherwise leave it. This is how
   those items get picked up instead of sitting there indefinitely — nobody re-reads the whole
   backlog before every edit, but this step only requires noticing when a match applies.

### While Writing Code
- Follow Cardinal Rules 2, 3, 6, 10 (account protection, CACHE, i18n, modal pattern)
- Meet the Code Quality Standards below (type hints, docstrings, comments, error handling)
- Log with appropriate level (DEBUG for verbose, INFO for actions)

### After Writing Code
- Test with validation: Does it solve the problem?
- Check integration: Do dependent functions still work?
- Verify cache consistency: Are save/load operations paired?
- Test error paths: Are failures handled gracefully?
- Update documentation per Cardinal Rule 15 if the change touches anything documented
- **Run tests with:** `.\run_tests.ps1` — NEVER construct a raw pytest command.
  Pass extra filters as trailing args: `.\run_tests.ps1 -k my_test` or `--lf`.
  The script encodes the canonical deselects; calling it directly keeps the
  command identical every run so VS Code auto-approves without prompting.

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

**Update `../changelog.txt` for every code change (feature, fix, refactor), without being asked:**

- **Location**: `changelog.txt` at the repo root (NOT `backlog.txt` — that's the separate feature/idea roadmap).
- **Position**: New entries go at the very TOP of the file, immediately after the `===========` header line.
- **Format**: `YYYY-MM-DD (N)` heading using TODAY's real date — `N` increments per entry added that day (1, 2, 3, ... do not reuse or renumber older entries, even across days).
- **Content**: Short and crisp — a few lines stating what changed (Fixed/Added/Changed) and why, plus the file(s) touched and the test result. Not a multi-paragraph blow-by-blow of every branch/file touched.
- **Documentation Updates**: See Cardinal Rule 15 — update the relevant doc(s) in the same pass, not as a follow-up.
- **Tests**: Run `.\run_tests.ps1` (see "After Writing Code" above) and report the real pass count in the entry — never a raw `pytest` count, which misreports deliberately-deselected tests as failures.

**Example** (see `changelog.txt` itself for the full live convention):
```
2026-07-25 (12)
- Fixed: Re-linking an account previously moved to the UNASSIGNED pool never got a CoC
  in-game role, since that pool is excluded from the periodic per-clan sync. Restore path
  in `_link_player_to_user()` now refetches live player data before restoring.
  File: qapbot/QBdiscocmdshelper.py. 1403 tests pass.
```

---

## Additional Resources (deep dives — use these instead of long examples here)

📖 **../qapbot/docs/CODE_STRUCTURE.md**: Module responsibilities, data flows, function trees, architecture details  
📖 **../qapbot/docs/DATABASE_ARCHITECTURE.md**: SQLite schema, migration history, backup/recovery, performance  
📖 **../qapbot/docs/RATE_LIMITING_IMPLEMENTATION.md**: CoC API rate limiting, parallel fetch pipeline  
📖 **../qapbot/docs/REGISTRATION_MESSAGE_WORKFLOWS.md**: Player registration and notification workflows  
📖 **../qapbot/docs/WAR_FILE_MANAGEMENT_RULES.md**: War temp/archive file lifecycle  
📖 **../qapbot/docs/CLAN_AND_WAR_CYCLE_ARCHITECTURE.md**: DB schema for clans/CWL, the Phase-1 update-cycle pipeline, coc_client init, hot/history DB split mechanics  
📖 **../qapbot/docs/CLAN_WAR_TRACKING.md**: Clan war tracking logic — track_war_updates tiers, league promotion/demotion handling  
📖 **../qapbot/docs/CWL_ROUND_TRACKING_PLAN.md**: CWL round-number tracking design (implemented) — `cwl_league_rounds`/`cwl_league_groups` schema and population  
📖 **../qapbot/docs/COC_GAME_MECHANICS.md**: CWL round schedule, regular war states, passive/inactive tracking, warlog visibility  
📖 **../qapbot/docs/ARCHIVE_SCAN_PERFORMANCE_ANALYSIS.md**: Archive-directory rescan cost analysis — dated investigation, revisit trigger noted inside  
📖 **../qapbot/docs/TEST_CONCEPT.md**: Test tier design (smoke/integration/discord/live/e2e), fixture strategy, coverage targets, CI pipeline — see also "Run tests with" above for the one-line version  
📖 **../qapbot/docs/CWL_CLAN_CONFIG_ACTIVITY_PLAN.md**: CWL Clan-Config Discord Activity — Cloudflare Pages/Workers + `qapbot/web_bridge.py` architecture, auth model, phase-by-phase history (see also Pitfall 25 / `../qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md` for the Activities Entry Point command gotcha)  
📖 **../activity/README.md**: Activity dev setup, deploy commands, PROD rollout runbook (domain, named `cloudflared` tunnel, NAS auto-start)  
📖 **../README.md**: Installation, environment variables, commands, deployment, features  
📖 **../backlog.txt**: Feature roadmap
📖 **../changelog.txt**: Changelog

**When in doubt**: Search the codebase first, reference documentation second, ask for clarification third.


