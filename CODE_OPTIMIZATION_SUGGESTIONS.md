# Code Optimization Suggestions — Main Bot Code Inspection (2026-08-08)

Scope: QapBot.py, QBcore.py, QBdiscordcmds.py, QBhelperfunctions.py, qapbot/cache_manager.py,
qapbot/db_manager.py, qapbot/QBdiscocmdshelper.py, qapbot/war_notifications.py,
qapbot/coc_cache.py, qapbot/guild_role_manager.py, qapbot/formatting.py, qapbot/ui_*.py.

## Status (updated 2026-08-08)

Every item is now **DONE** or has a recorded, verified reason it wasn't changed (a handful turned
out to be false positives on closer inspection — same disposition as B1, see section E). Only the
explicitly-opportunistic items (A6, B4, B7, D5 — each says "don't do this standalone" in its own
**Fix** note) remain genuinely open, by design.

| Item | Status |
|---|---|
| A1 (on_ready re-entry lock) | ✅ DONE — `QBcore.on_ready_lock`; also fixed a related bug found while implementing it: `initialization_in_progress` wasn't reset on early-return failure paths, permanently blocking retry |
| A2 (Step-5 partial-startup failure) | ✅ DONE — terminates the same way Step 3 does |
| A3 (untracked fire-and-forget tasks) | ✅ DONE — `QBcore.spawn_tracked()`, adopted at 9 call sites |
| A4 (`bulk_update_clan_timestamps` not re-queued) | ✅ DONE — `QBcore.pending_clan_timestamp_retries`, merged into the next flush at either call site |
| A5 (`guild.chunk()` result not defended) | ✅ DONE — logs registered users missing from `guild.members` after chunk |
| A6 (QBcore global state sprawl) | OPEN (opportunistic only, by its own Fix note) |
| B1 (war-notification datetime re-parsing) | ❌ FALSE POSITIVE — see section E |
| B2 (`fetch_and_update_player_info` double-scan) | ✅ DONE — `changed_uids` collected in one pass |
| B3 (`_resolve_war_league` re-resolved per render) | ✅ PARTIAL — real redundancy only existed in `_generate_cwlinfo_archive_embeds`'s round loop (fixed with a local memo dict); `generate_war_info_text`'s claim didn't hold up (only 2 distinct tags, each resolved once — see section E) |
| B4 (`compute_roster_stats_sync` 3 queries) | OPEN (opportunistic only, by its own Fix note) |
| B5 (`text_display_width` per-row cost) | ✅ DONE — `@functools.lru_cache(maxsize=4096)` |
| B6 (`_find_optimal_space_combination` blowup) | ✅ DONE — hard-capped replace-search bound + early-exit once a good-enough candidate is found |
| B7 (`save_user` per-row execute) | OPEN (opportunistic only, by its own Fix note) |
| B8 (Phase-3 per-clan log volume) | ❌ FALSE POSITIVE — see section E |
| C1 (`_save_user_impl` FK-recovery duplication) | ✅ DONE — `_upsert_users_row()` + `_replace_user_players_rows()` |
| C2 (`war_attacks` INSERT duplication) | ✅ DONE — turned out to be ~6 sites, not ~4; shared SQL constants + `_build_war_attack_params()` |
| C3 (Win/Loss/Draw classification duplication) | ✅ DONE — `classify_war_result()`, 3 real sites converted (report cited a 4th, `_build_standings_result`, that doesn't contain this logic) |
| C4 (`TrackedView` base for on_timeout/on_error) | ✅ DONE — adopted in 9 views (ui_common.py ×2, ui_registration.py ×2, ui_notifications.py ×5); ui_clan_management.py's `ClanManagementView` and QBdiscordcmds.py's ad-hoc views intentionally left as-is (extra timeout logic, `self.sent_message` naming) per this item's own "adopt incrementally" note |
| C5 (timestamp-parser dedup) | ✅ DONE — `war_notifications.parse_war_timestamp_field()`, shared by both call sites this item names. Found a 3rd near-duplicate too (QapBot.py's `_DT_RE`, a narrower single-purpose regex for backdating timestamps) — left as-is, different shape/purpose and outside this item's stated scope |
| D1 (dead stub buttons) | ✅ DONE — removed entirely (`backlog.txt` has no plan for this feature, and they turned out to be fully unreferenced dead code, not "wired to visible buttons" as reported — see section E) |
| D2 (silent except-pass sites) | ✅ DONE — debug-level logging added to both cited sites, plus 2 sibling occurrences of the exact same pattern in the same function |
| D3 (decorator-label audit) | ✅ DONE — audited all 8 real `@discord.ui.button(label=...)` sites codebase-wide; all already correctly re-translated in `__init__`, zero gaps found |
| D4 (redundant `update_user_metadata_from_interaction`) | ✅ DONE — moved into `RegistrationView.interaction_check()` |
| D5 (oversized functions) | OPEN (opportunistic only, by design — don't refactor standalone) |

Also fixed the same day, found via live prod-log diagnosis rather than this static inspection (not
in the list above — see qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md Pitfall 21 and changelog entries
(20)-(22)+): automatic (implicit) gc gen-2 sweeps freezing the whole process for multi-second
stretches during live cycles (plus a nightly re-freeze fix once the startup-only freeze proved
insufficient during CWL), a modal not showing which account it verifies, and a modal
interaction-token-expiry bug in the verify-account flow.

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

### B1. ~~Per-war datetime re-parsing in the notification loop~~ — ❌ FALSE POSITIVE (confirmed 2026-08-08, see section E)
Withdrawn after independent re-verification: the caching this finding proposed already exists
(since commit 17b0b67, 2026-07-20) — `_get_active_wars()` parses each war's end time once per
cycle and stores `hours_remaining` in war_data (with an explicit "downstream functions must not
re-parse" comment); all three consumers check the cached value first. The claimed mechanism
(`datetime.fromisoformat`) doesn't even appear in the file. Do not "fix". Full disposition in
section E. (Optional micro-item, NOT the original claim: the one-per-war-per-cycle regex parse
in `_get_active_wars()` could be eliminated by caching a parsed `end_epoch` in
`temp_war_metadata` at save time — but it already runs inside the `to_thread` worker, so this is
low value.)

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

- **B1 "per-war datetime re-parsing in the notification loop" — false positive**, confirmed by
  two independent re-checks (a second agent via `git log -L` blame, then re-verified directly
  against the code). The proposed fix was already implemented in commit 17b0b67 (2026-07-20):
  `_get_active_wars()` parses once per war per cycle and caches `hours_remaining` in war_data;
  consumers (`_get_players_needing_reminders`, `_get_players_with_attacks_remaining`,
  `_send_channel_war_notification`) all read the cached value and only re-parse on a `None`
  fallback. The finding also misidentified the mechanism (`datetime.fromisoformat` — zero
  occurrences; the real parser regexes the `<Timestamp ...>` repr). Lesson for future
  inspections: `[reported]` findings that cite a specific API must be grepped for that exact
  API before being acted on.
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
- **B3 "generate_war_info_text resolves the same clan/opponent league 2-3 times"** — false for
  this function specifically: it calls `_resolve_war_league()` exactly twice, once each for
  `clan_tag` and `opp_tag` — two different tags, not a repeat. `_generate_cwlinfo_archive_embeds`'s
  claim held up (opponent tags genuinely can repeat across a season's rounds) and was fixed.
- **B8 "per-clan Processing/Skipping lines balloon the log"** — no `logging.info()` call with that
  text exists in the cited range (QapBot.py Phase-3 loop) or in `manage_war_files()` /
  `process_clan_war_data()` (the functions it calls). Audited every `logging.info`/`logging.debug`
  call in all three: exactly one per-clan `INFO` line exists (`[INACTIVE] ... Smart timestamp
  set...`), gated to a bounded subset (inactive clans whose war just ended), not "every clan every
  cycle." Everything else is already `DEBUG`. Not fixed — nothing to fix.
- **D1 "wired to visible buttons"** — false: `_on_set_newbie_role`/`_on_set_member_role` in
  ui_clan_management.py were never assigned as any button's `.callback` anywhere in the file —
  fully orphaned dead methods, not reachable from any UI element. Removed entirely rather than
  adding `disabled=True` to a button that doesn't exist. The real newbie/member-role configuration
  feature already exists elsewhere in the same file (the role-selection view around line ~2986),
  so these were leftover stubs from before that feature was built, not an in-progress placeholder.

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
2. ~~B1~~ (withdrawn — false positive, see section E)
3. **C1 + C2** (SQL dedup — do together, one review pass over all war-write paths)
4. **A3** (tracked-task helper) and **D4** (metadata call consolidation)
5. **C4** (`TrackedView` base) — ideally *before* the CWL roster feature adds 5+ new views
6. Everything else opportunistically, per D5's rule of thumb.

Run `.\run_tests.ps1` after each item; A1/A2 additionally deserve a dev-guild restart test.
