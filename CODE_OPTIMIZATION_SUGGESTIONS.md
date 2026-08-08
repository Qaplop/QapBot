# Code Optimization Suggestions — Main Bot Code Inspection (2026-08-08)

Scope: QapBot.py, QBcore.py, QBdiscordcmds.py, QBhelperfunctions.py, qapbot/cache_manager.py,
qapbot/db_manager.py, qapbot/QBdiscocmdshelper.py, qapbot/war_notifications.py,
qapbot/coc_cache.py, qapbot/guild_role_manager.py, qapbot/formatting.py, qapbot/ui_*.py.

Method: four parallel deep inspections (orchestration, data layer, command/UI layer,
helpers/notifications), findings cross-checked against the code where impact claims were high.
Items already fixed earlier today (deep-link helper, repost extraction, ChannelConfigurationView
slots, orphan-purge registry, RegistrationView add_view + startup gates, users_loaded guard,
_get_active_wars to_thread) and documented pitfalls are excluded.

Each item is tagged **[verified]** (I read the code and confirm it) or **[reported]**
(subagent finding, spot-checks pending — re-verify the line numbers before acting).
Prod scale assumptions: ~380K clans in clan_name_cache, 17K+ active wars during CWL,
65M+ war_attacks rows, maindata (users/user_players) low hundreds of rows.

---

## A. Correctness / robustness (highest value)

### A1. `on_ready` has no lock against concurrent re-entry — [reported, high]
QapBot.py ~2638-2650: initialization is guarded by an `initialization_in_progress` bool, not an
`asyncio.Lock`. Two rapid gateway reconnects could both pass the check window and run
`CACHE.load_all()` / DB init twice. `initialize_database()` has its own idempotence check
(`if CACHE.db_manager is not None: return`) but the cache-load and periodic-task steps don't.
**Fix**: wrap the whole init section in a module-level `asyncio.Lock`; second entrant returns
immediately.

### A2. Partial startup: cache loaded but periodic task failed to start — [reported, medium]
QapBot.py ~2768-2805 (on_ready Step 5): if `create_task(periodic_main())` raises, the function
`return`s with a loaded cache, `fully_initialized` never set, and no cleanup/retry — bot sits
half-alive. **Fix**: on Step-5 failure, log CRITICAL and either retry once or run
`async_cleanup()` + `bot.close()` like the cache-load failure path does.

### A3. Untracked fire-and-forget tasks — [verified, medium]
Multiple `asyncio.create_task(...)` calls (WAL checkpoint, deferred optimize-DB,
`_warm_global_db_stats_cache`, repost triggers in ui_clan_management.py) discard the task
reference. Python can garbage-collect running tasks whose only reference is lost, and duplicate
spawns are invisible. **Fix**: small `spawn_tracked(name, coro)` helper in QBcore that stores
refs in a set (discard on done) — one place to see/cancel background work; prevents GC of
in-flight tasks.

### A4. `bulk_update_clan_timestamps` failures not re-queued — [reported, medium]
QapBot.py ~1583-1590: if the batched timestamp write fails, clans stay updated in-memory but not
in DB; after a restart they look overdue and get re-polled (self-healing but wasteful), and the
failure is only logged. **Fix**: on failure, re-append the batch to next cycle's write list (or
retry once with backoff).

### A5. `guild.chunk()` result not defended in role sync — [reported, low-medium]
guild_role_manager.py ~824-829: after `guild.chunk()`, the member loop assumes the local cache is
complete; a member missing from cache is silently skipped (no role sync, no log). **Fix**: log
cache misses for registered users; optionally `fetch_member()` fallback for those specific IDs.

### A6. Global mutable state sprawl in QBcore — [verified, low (works today, risk grows)]
15+ module-level flags/events (`maintenance_mode`, `db_maintenance_mode`, `maintenance_pending`,
`optimize_db_pending`, `cycle_idle_event`, `force_cycle_event`, ...). Each is individually fine;
collectively the implicit state machine has no single owner and every new flag multiplies
interactions. **Fix (opportunistic)**: group into one dataclass (`BotState`) next time a new flag
would be added — not worth a big-bang refactor on its own.

---

## B. Performance

### B1. Per-war datetime re-parsing in the notification loop — [reported, medium]
war_notifications.py ~334-360: `_get_hours_until_war_end()` runs `datetime.fromisoformat()` on
each war's `end_time` string, every notification cycle, for every active war (17K+ during CWL —
this loop is inside the same `check_wars_for_notifications()` that just got the
`_get_active_wars` to_thread fix; parsing is a meaningful share of the remaining `war_loop=3.6s`
prod timing). **Fix**: parse once when the war dict is built (store the datetime/epoch alongside
the string) and compare numbers in the loop.

### B2. `fetch_and_update_player_info` scans all users twice per player — [verified, low-medium]
cache_manager.py ~1504-1543: one full pass over `user_accounts` to mutate, then a second full
pass to find whom to persist. Maindata is small (low hundreds), so this is not a hot cost today —
but it's O(users×players) per fetched player and trivially avoidable. **Fix**: collect
`changed_uids: set` in the first loop; persist from that set. (A global `player_tag → user_id`
reverse index would also serve `get_*_player_owner()` — check those call sites before building it.)

### B3. Result of `_resolve_war_league()` re-resolved within one render — [reported, medium]
QBhelperfunctions.py: `generate_war_info_text` (~812-848) resolves the same clan/opponent league
2-3 times per render; `_generate_cwlinfo_archive_embeds` (~1079-1145) calls it per round where
opponent tags repeat across a season. Each miss can trigger an API fetch. **Fix**: resolve once
into a local `{tag: league}` dict at the top of each render and pass it down.

### B4. `compute_roster_stats_sync` — 3 sequential aggregate queries — [verified, low]
QBhelperfunctions.py ~3641-3723: three separate full aggregate queries over hot+history per
invocation. Each is index-supported and correct (post-materialize-fix); merging them is possible
but the join/GROUP BY shapes differ. **Fix**: only worth touching when the CWL Phase-3 extension
(attack counts) lands anyway — fold the count into an existing query instead of adding a 4th.

### B5. `render_leaderboard` per-row width computation — [reported, low]
formatting.py ~700-818: `text_display_width()` (wcwidth per character) is called repeatedly per
player row per render. 50-100 rows × several columns is fine normally, but leaderboard posting
runs for all subscribed channels in a batch. **Fix**: memoize width per (already-normalized) name
string with a small `functools.lru_cache`.

### B6. `_find_optimal_space_combination` worst-case blowup — [reported, low]
formatting.py ~519-573: 4-level nested loop over space-replacement combinations per player cell.
Normal inputs are tiny; pathological names could spike. **Fix**: early-exit when `diff` is below
a threshold; cap iterations.

### B7. `save_user`/bulk user writes use per-row execute — [verified, low]
db_manager.py ~4944+: per-player `INSERT` in a loop inside the transaction. Correct and atomic;
at current maindata scale (a few hundred players total) not a bottleneck. **Fix**: switch to
`executemany()` opportunistically next time the function is edited — don't ship a standalone
change for this.

### B8. Per-clan log lines in Phase-3 loop — [reported, low]
QapBot.py ~1247-1320: per-clan "Processing/Skipping" lines at scale balloon the log (log volume
was already reduced 90% for Phase-1 inactive fetches by grouping — same treatment applies here).
**Fix**: batch log lines in groups of 10 like the `[INACTIVE 22H FETCHING]` pattern, or demote to
DEBUG and log per-batch counts at INFO.

---

## C. DRY / duplication

### C1. `_save_user_impl` FK-recovery retry duplicates the whole INSERT — [verified, medium]
db_manager.py ~4899-4990: the `FOREIGN KEY` failure recovery path re-states the entire
users-INSERT and players-INSERT SQL with one difference (`current_clan_tag=NULL`). Any schema
change must now be applied twice, invisibly. **Fix**: extract
`_insert_user_row(conn, discord_id, user_data)` + `_insert_player_rows(conn, ..., null_clan_tags: bool)`
and call from both paths.

### C2. `INSERT INTO war_attacks` repeated ~4× — [reported, medium]
db_manager.py ~760-780, ~2110-2122, ~2273-2285, ~2360-2372: near-identical column lists/parameter
mapping in different write paths (async single, sync batch, recovery). Same maintenance hazard as
C1 but wider. **Fix**: one module-level SQL constant + a shared param-tuple builder; each path
keeps its own transaction/locking.

### C3. Win/Loss/Draw classification duplicated across embed builders — [verified, low]
QBhelperfunctions.py: the stars-then-destruction tiebreak block appears in
`_generate_cwlinfo_archive_embeds` (~1105-1116), the DB-past-round block (~2040+), the live-round
block, and `_build_standings_result`-adjacent code. **Fix**: single
`classify_war_result(my_stars, opp_stars, my_dest, opp_dest) -> str` helper.

### C4. View boilerplate: on_timeout delete + 10062 on_error + message tracking — [verified, medium]
5+ views carry copies of the ~4-line on_timeout delete-message pattern; only 2 views implement
the 10062 suppression (others let expired-token errors propagate noisily); message-tracking
attribute is `self.message` in some views and `self.sent_message` in others. **Fix**: one
`TrackedView(discord.ui.View)` base in ui_common.py (unified `self.message`, shared on_timeout,
shared 10062-aware on_error) — adopt incrementally as views get touched, plus in all new CWL
roster views from day one.

### C5. `generate_war_info_text`'s timestamp parser is function-local — [verified, low]
QBhelperfunctions.py ~726-751: `import re` + `_parse_timestamp_field` nested inside the function;
the same `<Timestamp ...>` string format is parsed in war_notifications.py too. (Note: Python
caches compiled regexes internally, so the "recompile per call" cost claim is overstated — this
is a DRY/structure item, not perf.) **Fix**: move the parser + module-level compiled patterns to
a shared location (constants.py or war_notifications.py) and use it from both.

---

## D. Cleanup / hygiene

### D1. Dead stub buttons — [reported, low]
ui_clan_management.py ~1617/~1629: `_on_set_newbie_role` / `_on_set_member_role` respond
"feature coming soon" but are wired to visible buttons. **Fix**: disable (`disabled=True`) or
remove until implemented; check backlog.txt for whether the feature is still planned.

### D2. Silent `except Exception: pass` sites — [reported, low]
QBcore.py ~122-125 (startup-block send), QapBot.py ~2052-2059 (malloc_trim). Both are best-effort
by design, but a `logging.debug(...)` inside costs nothing and preserves forensics — the
2026-08-08 data-loss incident showed how much silent failure paths cost during diagnosis.
**Fix**: add debug-level logging to each; keep the swallow.

### D3. Decorator-time hardcoded English labels — audit, mostly OK — [verified, low]
Spot-check result: views like `WarNotificationPromptView` and `RegistrationView` correctly
re-translate their decorator labels in `__init__`, so the decorator strings never reach users.
The inspection flagged ~13 raw instances; at least some are false positives. **Fix**: one audit
pass over `@discord.ui.button(label=` / `placeholder=` sites confirming each is either
re-translated in `__init__` or genuinely allowed (modal TextInput.label); add the missing
re-translations only where actually absent.

### D4. Redundant `update_user_metadata_from_interaction` per-button — [verified, low]
ui_registration.py: all 4 RegistrationView buttons call it first thing. Since the new
`interaction_check` already runs on every click, moving the call there (post-guard) removes 4
copies and guarantees it can never run pre-load. **Fix**: call it once at the end of
`RegistrationView.interaction_check()` after the `fully_initialized` gate passes.

### D5. Oversized functions (structural debt, refactor only with a reason) — [verified]
The worst offenders, for when a feature touches them anyway:
- `main()` QapBot.py ~579-1640 (~1060 lines, Phases 1-3 inline)
- `admin()` QBdiscordcmds.py ~1176+ (~1200 lines incl. inline modal classes)
- `periodic_main()` QapBot.py ~1883-2380 (~500 lines)
- `generate_cwlinfo_embeds()` QBhelperfunctions.py ~1786-2264 (~478 lines)
- `save_war_object()` cache_manager.py ~2255-2633 (~378 lines)
Rule of thumb per project convention: don't refactor these standalone (huge diff, high regression
risk, 1517-test suite has good but not perfect coverage of their branches); extract the piece you
need when a change lands inside one.

---

## E. Claims from the inspection that did NOT hold up (recorded so they aren't re-reported)

- "Missing hot/history schema qualifiers at db_manager.py ~3169" — the code there is the
  **correct** post-materialize-fix pattern (UNION ALL CTE for the INNER side + split ws_h/ws_a
  LEFT JOINs). Any future audit should classify bare `FROM war_summary` hits per the repo rule:
  current-season-only queries are main-only **by design**.
- "Regex recompiled 34K×/cycle in generate_war_info_text" — Python's `re` module caches compiled
  patterns; the real issue is structural (see C5), not recompilation cost.
- "ui_notifications.py hardcoded button labels" — false for `WarNotificationPromptView`; labels
  are re-translated in `__init__` (see D3 for the remaining audit).
- "10K users × N players" scale claims for user-account loops — prod maindata is low hundreds of
  rows (see DATABASE_ARCHITECTURE.md § Database Size); those loops are cheap today (B2 kept at
  low-medium for structural reasons, not measured cost).

## F. Verified-good (no action)

- Event-loop yield discipline in the categorization loop and Phase-3 (sleep(0) cadence).
- Write-through coverage in cache_manager (mutation → persist pairing).
- Interaction paths: no `*_sync` DB calls found directly in button callbacks without defer.
- Idempotent DB writes (`OR IGNORE`/`OR REPLACE`) throughout.
- `war_write_batch()` commit batching; sync connection pool.
- Hot/history split queries in the whois/roster paths (post-2026-07-16 fixes).

---

## Suggested order of attack

1. **A1 + A2** (startup races — small, high value, easy to test)
2. **B1** (notification-loop parse caching — directly reduces the remaining prod `war_loop` time)
3. **C1 + C2** (SQL dedup — do together, one review pass over all war-write paths)
4. **A3** (tracked-task helper) and **D4** (metadata call consolidation)
5. **C4** (`TrackedView` base) — ideally *before* the CWL roster feature adds 5+ new views
6. Everything else opportunistically, per D5's rule of thumb.

Run `.\run_tests.ps1` after each item; A1/A2/B1 additionally deserve a dev-guild restart test.
