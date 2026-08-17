# Retire `CACHE.player_name_index` (in-memory player-name cache)

Implementation plan for fully retiring `CACHE.player_name_index` — the multi-million-entry
in-memory `Dict[str, Tuple[str, str]]` built at startup and scanned on every `/whois` name search
— now that its SQLite/FTS5 replacement (`CWL_PROD_PERFORMANCE_FIX_PLAN.md` P2 Step 11) has run
one release on both DEV and PROD. This plan grew out of a live-testing follow-up conversation on
2026-08-17: Step 11 only migrated the CWL guest-search's name/tag-prefix paths behind
`CONFIG.cwl_use_fts_player_search`; `/whois player <name>`'s own inline scan
(`QBdiscordcmds.py:_search_player_name_index_sync`) was deliberately left on the old in-memory
path because it needs the *full, uncapped* match set for its guild-membership reorder, and the
existing SQL reader (`search_player_names_sync`) hard-caps at `LIMIT 25`. That is the only real
blocker, and it is solvable: FTS5's trigram index only touches matching rows, so a generously
(not tightly) capped SQL query is cheap even when the in-memory "give me everything" scan
wouldn't have been.

**Do not start until `cwl_use_fts_player_search` has had a confirmed burn-in period on PROD** (the
guest-search path only) — check with the user before Step 5 (retiring the in-memory dict) if that
burn-in hasn't been explicitly confirmed yet. Steps 1–4 are safe to implement and test at any time
since they only *add* a new code path; nothing is removed until Step 5.

---

## 2026-08-17 addendum: PROD incident in this same table, already fixed — read before executing

After this plan was first written, PROD's first update cycle following the Step 11 deploy stalled
silently on every cycle right after `[PHASE-3B] Completed` (alive, CPU-burning, not a deadlock).
Root cause: `player_name_fts.player_tag` is `UNINDEXED` — that excludes it from full-text `MATCH`
but gives SQLite **no index for a plain equality lookup**, so the incremental writer's
`DELETE FROM player_name_fts WHERE player_tag = ?` fell back to a full table scan *per changed
player* on every war-write flush. Invisible in tests (tiny tables); catastrophic the moment the
startup backfill populated the table to its real ~6.6M rows. Fixed across three commits, each
correcting a flaw the previous one introduced (changelog entries 52-54,
`qapbot/docs/DATABASE_ARCHITECTURE.md`'s player_name_fts section, Pitfalls 16/29 in the
cookbook): `de5428f` gave `player_name_fts` an explicit `rowid` (the one column FTS5 genuinely
indexes) so writes/deletes target `WHERE rowid = ?`; `e198211` moved the resulting one-time
rebuild off the event loop (it froze the Discord heartbeat); `b02b73a` replaced the v1 per-tag
blake2b hash derivation with a lookup of `player_name_search`'s own SQLite-assigned rowid, after
v1's 6.6M Python hash calls (355s on DEV) blew past PROD's 30-minute DB-init timeout entirely.
Final state: rowids are **looked up, never computed** — there is no hash function; marker
`player_name_fts_rowid_scheme = player_name_search_rowid_v2` in `bot_metadata` gates the one-time
rebuild. Not a blocker for this plan, but three things change as a result:

1. **Line-number drift**: those fixes reshaped `qapbot/db_manager.py` twice — every citation into
   `db_manager.py` in this document was re-verified against `b02b73a`, but re-grep before
   trusting any of them if more changes land in between. Citations into other files
   (`QBdiscordcmds.py`, `cache_manager.py`, `web_bridge.py`, `coc_cache.py`, `config.py`) are
   unaffected — the incident fixes touched `db_manager.py` (and its tests/docs) only.
2. **`_upsert_player_name_search_rows_in_conn` and `_backfill_player_name_search_if_needed` now
   look different** than they might be remembered from Step 11's original implementation — both
   are rowid-based now, not the plain `player_tag`-keyed delete+insert described in earlier
   commit messages. Step 5's removal list doesn't touch either function (they're DB write-through
   machinery for the SQL tables, not the in-memory `CACHE.player_name_index` dict this plan
   retires) — just be aware the code there has moved since Step 11 first shipped.
3. **Worth folding into Step 4's burn-in checklist**: this incident was a *write-path*
   performance bug, not a read-path correctness bug — Step 11's read paths
   (`search_player_names_sync`/`search_player_tags_by_prefix_sync`) were never broken, and Step
   1's new `search_player_names_full_sync` reader is unaffected by design (it's a `MATCH` query,
   never an equality filter on the UNINDEXED `player_tag` column, so it was never exposed to this
   bug). But it's a concrete reminder that "looks fine on DEV" isn't sufficient burn-in evidence
   for this table specifically — DEV never has PROD-scale row counts, so a regression here is
   structurally invisible until PROD. Add "update cycles complete PHASE-3B → PHASE-3 promptly,
   no silent multi-minute gaps in the log" to Step 4's DEV checklist, and treat a clean PROD cycle
   post-deploy as part of the burn-in signal this plan's own gate is waiting on, not just the
   read-path guest-search results.

---

## Mandatory project conventions (read before implementing anything)

- Read `.github/copilot-instructions.md` in full first, particularly: Rule 11 (DB access via
  `CACHE.db_manager.*` only), Rule 14 (named column access only, `row["col"]`, never positional),
  Pitfall 26 (`qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md`) — sync SQLite calls reached from the
  event loop must be wrapped in `asyncio.to_thread()`.
- **Tests**: run with `.\run_tests.ps1` (never raw pytest). Report the real pass count in every
  changelog entry.
- **Changelog**: one `changelog.txt` entry per delivered step (or per coherent group of steps), at
  the TOP, format `YYYY-MM-DD (N)`, listing files touched and test count.
- **Docs**: update `qapbot/docs/DATABASE_ARCHITECTURE.md` (it already documents
  `player_name_index`/`player_name_search`/`player_name_fts` under its own "2026-08-17" section —
  extend that section, don't create a new one) and `qapbot/docs/CODE_STRUCTURE.md` (it lists
  `search_players_by_name_sync` and friends). Update `.env`/`config.py` doc comments for the flag
  if Step 6 retires it.
- **This is backend-only** — no `activity/client` frontend files are touched by any step below;
  confirm that remains true before considering a step done (see the reasoning in the 2026-08-17
  changelog entry for the FTS5 rollout: response shapes returned to the frontend are unchanged).
- Needs a bot restart on DEV/PROD after each deployed step; note this in the changelog entry.

---

## Step 1: Add an uncapped(-ish) SQL reader for `/whois`'s full-match-set need

**File**: `qapbot/db_manager.py`, next to `search_player_names_sync` (~line 5389 as of commit
`b02b73a` — this file's line numbers have already drifted twice during the 2026-08-17 incident
fixes; treat every db_manager.py line cite in this plan as approximate and re-grep before
editing).

**Current behavior**: `search_player_names_sync(query, limit=25)` runs
`SELECT player_tag, name FROM player_name_fts WHERE player_name_fts MATCH ? ORDER BY name COLLATE NOCASE LIMIT ?`
with `LIMIT min(limit, 25)` — hard-capped for the guest search's own needs. `/whois` cannot use
this today because its guild-membership reorder (`QBdiscordcmds.py:4902-4936`) needs to see every
match, not just the alphabetically-first 25, or a guild member sorting later in the alphabet gets
silently excluded before the reorder ever runs.

**Target behavior**: add a sibling method, e.g.:

```python
def search_player_names_full_sync(self, query: str, hard_cap: int = 5000) -> List[Dict[str, str]]:
    """Same FTS5 MATCH query as search_player_names_sync, but with a generous safety-valve cap
    instead of a 25-row UX cap — for callers (currently only /whois) that need the full match
    set for their own downstream reordering/filtering before slicing to a UX-facing count.
    hard_cap exists only to bound worst-case cost against a pathological substring matching a
    huge fraction of the table; real guild rosters never come close to it."""
```

Reuse the exact same literal-quoting (`'"' + query.replace('"', '""') + '"'`), the same `<3 chars
→ []` trigram-floor guard, and the same `sqlite3.Error` catch-and-log-empty pattern as
`search_player_names_sync`. Only the `LIMIT` value and cap parameter name differ. Consider
refactoring both into one private `_search_player_names_fts_sync(query, limit)` core with two
thin public wrappers (25-cap and 5000-cap) rather than duplicating the query — your call, but
don't duplicate the FTS5-quoting logic verbatim in two places (see
`qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md` generally on not duplicating subtle logic).

**Known edge the cap reintroduces — decide, don't ignore (2026-08-17 review finding)**: with
`ORDER BY name COLLATE NOCASE LIMIT :hard_cap`, a query matching MORE than `hard_cap` players
drops the alphabetically-late matches *before* `/whois`'s guild reorder ever sees them — which is
structurally the same failure mode quoted above as the reason `/whois` can't use the 25-cap
reader, just at 5000 instead of 25. The docstring's "real guild rosters never come close to it"
defends the wrong quantity: the risk scales with total MATCH count for the substring, not roster
size (e.g. a 3-char needle like "war" over 6.6M names can clear 5000 matches easily; a guild
member named "Zwarrior" then sorts past the cap and can never be surfaced by the reorder). The
old in-memory scan had no such cutoff, so this is a genuine (if narrow) behavior regression, not
a wash. Two acceptable resolutions — pick one explicitly and record it in the changelog entry:
  1. **Accept + document**: `/whois` already appends a "too many matches, refine your query"
     hint when >25 matches; extend that reasoning — a >5000-match query is far too vague to be
     useful anyway, and the fix is typing one more character. Cheapest, probably right.
  2. **Mitigate**: after the capped query, run a second small FTS query for the same needle
     restricted to the guild's member tags (`AND player_tag IN (...)` via the chunked-IN helper,
     guild tags come from the same sources the reorder already collects) and union the results
     in. Only worth it if the user considers the edge case real for their guilds' name patterns.

**Tests** (extend `tests/unit/test_player_name_search_fts.py`):
- Query matching > 25 rows returns more than 25 (proves it's not silently reusing the 25-cap
  path).
- Query matching 0 rows → `[]`.
- Query shorter than 3 chars → `[]`, no query issued (spy on the sync connection or check no
  exception path is needed).
- `hard_cap` is actually honored: seed > `hard_cap` rows (use a small `hard_cap` override in the
  test, don't seed 5000 real rows), assert result length == `hard_cap`.
- FTS5-literal-quoting regression: a query containing `-`, `"`, or an FTS keyword like `AND`
  matches the literal substring, not query syntax (mirror the existing test for
  `search_player_names_sync`).

---

## Step 2: Add the ≥3-character floor to `/whois` explicitly, with a clear user-facing message

**File**: `QBdiscordcmds.py`, `whois_slash()` (~line 4883-4901).

**Why this step exists on its own**: this is the one real, non-cosmetic behavior change in the
whole migration. Today's in-memory scan (`needle_lower in name_lower`) has no length floor — a
1- or 2-character query works. FTS5's trigram tokenizer structurally cannot match anything below
3 characters. Silently returning "no results" for a 1-2 char query would look like a bug, not a
documented limitation — surface it explicitly instead.

**Target behavior**: before calling the new SQL reader, check
`len(player_stripped) < 3` (after the existing `#`-prefix branch, which is unaffected — explicit
tag lookups never go through name search at all) and send a clear, translated message instead of
searching, e.g. reusing the `t()` i18n pattern already used elsewhere in this function:
`commands.whois.player_search_too_short` (new key, both `en` and `de` locale files — check
`qapbot/locales/` or wherever `t()`'s translation files live for the existing key-naming
convention next to `player_not_found`/`player_select_prompt`).

**Tests** (extend wherever `whois_slash`'s name-search branch is already tested, likely
`tests/discord/` — grep for `player_not_found` or `whois_slash` to find the right file):
- 1-character and 2-character `player=` values → the new too-short message, and assert the new
  SQL reader (and the old in-memory one) was NOT called.
- 3-character value → proceeds to search as normal.

---

## Step 3: Swap `/whois`'s match-collection call site to the SQL reader

**File**: `QBdiscordcmds.py` — `_search_player_name_index_sync()` (~line 4847-4859) and its call
site in `whois_slash()` (~line 4894-4901).

**Target behavior**: replace the body of `_search_player_name_index_sync` (or rename it — it's no
longer scanning `player_name_index`) to call
`CACHE.db_manager.search_player_names_full_sync(needle, hard_cap=...)` instead of iterating
`CACHE.player_name_index`. Keep the `asyncio.to_thread()` wrapping at the call site exactly as it
is today (sync SQLite calls still must not run on the event loop, even though they're now
index-backed and fast — Pitfall 26 applies regardless of expected latency, not just to slow
calls). The guild-membership reorder code immediately below (~line 4902-4936) needs **no
changes** — it's already keyed purely on `player_tag` from whatever match list it's handed.

Do **not** delete `CACHE.player_name_index` itself in this step — leave the dict, its loader, and
`set_player_name()` in place and still being written to. This step only changes what `/whois`
*reads from*. Keeping the writes running lets you compare old-path vs new-path results side by
side on DEV before Step 5 deletes the fallback for good (e.g. temporarily log both result sets at
DEBUG level and diff them during your own DEV testing — remove that debug diff logging before
merging, don't ship it).

**Tests**: update/extend whatever test currently seeds `CACHE.player_name_index` and asserts on
`whois_slash`'s name-search results — switch the seeding to the DB tables
(`db.update_player_name_index_sync(...)`, which already mirrors into `player_name_search`/
`player_name_fts`) instead, mirroring the pattern already used in
`tests/discord/test_web_bridge.py`'s `test_..._sqlite_backed_...` tests (e.g. around line
1996-2011, which proves the SQL path was used by leaving `CACHE.player_name_index` deliberately
empty and seeding only the DB).

---

## Step 4: DEV/PROD burn-in of the `/whois` migration itself

Not a code step — a checkpoint. Deploy Steps 1-3, restart DEV, and live-test `/whois player
<substring>` with:
- A substring matching zero players.
- A substring matching exactly one player (auto-resolves, per existing `len(matches) == 1`
  branch).
- A substring matching several players in your own test guild (dropdown appears, guild members
  sort first — this is the behavior Step 11 explicitly protected; verify it still holds).
- A 1-2 character substring (verify Step 2's new message, not a silent empty result).

Only proceed to Step 5 once this is confirmed on DEV — and, per the note at the top of this plan,
once the user has separately confirmed PROD burn-in on the guest-search path from the original
Step 11 rollout. Ask before deploying Steps 5+ to PROD if that confirmation hasn't happened yet.

---

## Step 5: Retire `CACHE.player_name_index` (the in-memory dict) and its writer

Only after Step 4's checkpoint passes. Both consumers (`CACHE.search_player_names()` guest-search
path, gated by `CONFIG.cwl_use_fts_player_search`, and `/whois`, unconditionally after Step 3) no
longer need the in-memory dict as their *primary* path — see Step 6 for the guest-search flag
question specifically.

**Files and exact removals**:

- `qapbot/cache_manager.py`:
  - `self.player_name_index: Dict[str, Tuple[str, str]] = {}` declaration (~line 255).
  - `load_player_name_index()` (~line 436-452) and `_load_player_name_index_sync()`
    (~line 454-459) — the startup loader.
  - `set_player_name()` (~line 461-465) — the writer.
  - The call to `load_player_name_index()` inside the startup sequence (~line 2417-2419,
    `self._current_load_operation = "load_player_name_index"` / `await
    self.load_player_name_index()`) — remove the whole step, not just the flag.
  - `search_player_names()`'s in-memory fallback branch (~line 498-510, everything after the
    `if CONFIG.cwl_use_fts_player_search and self.db_manager is not None:` early-return) — see
    Step 6 below before deciding whether this whole method can collapse to just the SQL call.
- `qapbot/coc_cache.py`: the `cache_manager.set_player_name(tag, name)` call (~line 678) inside
  `update_player_info_in_user_accounts()`'s name-change propagation block (~line 673-685) — the
  `await asyncio.to_thread(cache_manager.db_manager.update_player_name_index_sync, updates)` call
  right after it **must stay** (that's the DB write-through to `player_name_index` +
  `player_name_search` + `player_name_fts`, unrelated to the in-memory dict).
- `qapbot/web_bridge.py` (~line 772-786, `_search_cwl_guests_sync`'s tag-prefix mode): the
  `else:` branch that iterates `CACHE.player_name_index` directly — only removable once Step 6
  below decides the flag itself goes away; if the flag stays as a permanent kill-switch, this
  branch has nothing left to fall back to and must be reworked or the flag must actually be
  retired in this same step (don't leave a flag whose `False` branch is dead code).

**Tests to update** (grep `player_name_index` across `tests/` — this list is from the 2026-08-17
audit, re-grep before starting since new tests may have been added since):
- `tests/discord/test_web_bridge.py` — many direct `CACHE.player_name_index = ...` seeds; switch
  every one to seeding the DB tables via `db.update_player_name_index_sync(...)`.
- `tests/unit/test_db_search_players_by_name.py` — the `TestSearchPlayerNames` class (~line
  440-539) specifically tests the in-memory scan (`cache.player_name_index = {...}`); this whole
  class either gets deleted (if `search_player_names()`'s in-memory branch is fully removed per
  Step 6) or repurposed to test whatever thin wrapper remains.
- `tests/unit/test_coc_cache_coverage.py:495` and `tests/integration/test_coc_cache_failures.py:113`
  — check what these seed `player_name_index={}`/`cm.player_name_index = {}` for; likely just
  satisfying a constructor/attribute that no longer exists — update the fixture, don't just delete
  the assertion.
- `tests/unit/test_player_name_search_fts.py` — **added to this list 2026-08-17, post-incident;
  it did not exist when this plan was first drafted.** Its `TestCacheManagerRolloutFlag` class
  seeds `cache.player_name_index` directly, and `test_uses_in_memory_path_when_flag_false`
  specifically asserts the in-memory branch works with `db_manager=None` — that test's entire
  premise disappears with Step 5/6 and it must be deleted or inverted (flag-False behavior
  becomes whatever Step 6 decides), not mechanically ported.
- `tests/conftest.py` — re-check nothing else there depends on `player_name_index` being
  pre-seeded for unrelated tests to pass (the file already has one FTS-flag-related override from
  the 2026-08-17 session; check it's still needed/correct after this step). Note the interaction
  with Step 6: conftest force-sets `CWL_USE_FTS_PLAYER_SEARCH*="false"` for test isolation — if
  Step 6 retires the flag this override becomes harmless dead config to clean up, but if Step 6
  keeps the flag with an error-path `False` branch, the ENTIRE test suite would run down that
  error path; conftest would then need to force `"true"` instead.

---

## Step 6: Decide the fate of `CONFIG.cwl_use_fts_player_search`

Not purely mechanical — needs a decision, not just code. Once Step 5 removes the in-memory
fallback entirely, the flag's `False` branch in `search_player_names()` (guest search) and in
`web_bridge.py`'s tag-prefix mode has nothing left to fall back *to*. Two options, present both to
the user rather than picking silently:

1. **Retire the flag**: delete `cwl_use_fts_player_search` from `config.py` (~line 194-203) and
   its DEV/PROD `.env` entries, make the SQL path unconditional in both call sites. Simpler
   codebase, but no config-only rollback lever left if FTS5 misbehaves under real load later — a
   rollback would need a code change + redeploy, not a `.env` edit.
2. **Keep the flag as a permanent kill-switch**: its `False` branch has to point somewhere.
   Cheapest option: keep a *much smaller* emergency fallback (e.g. just re-raise/log an error
   asking to re-enable the flag rather than maintaining a full parallel in-memory implementation)
   rather than resurrecting the deleted dict.

Recommendation: option 1, once Step 4's DEV burn-in and the user's separate PROD burn-in
confirmation both look solid — the flag's whole purpose was a safety net for exactly the migration
this plan just finished proving out; keeping it alive past that point just adds a permanently-dead
code path (`.github/copilot-instructions.md`'s general anti-cruft guidance applies). But this is
the user's call, not an implementation detail — ask before executing this step either way.

---

## Step 7 (optional, separate, unrelated): delete `search_players_by_name_sync`

**File**: `qapbot/db_manager.py` (~line 5457 as of commit `b02b73a`; line numbers in this file
have drifted with each 2026-08-17 incident fix — re-grep `def search_players_by_name_sync` to
confirm before editing).

Found dead during the 2026-08-17 investigation that produced this plan: this is an *older*
war_attacks `LIKE '%substr%'` fallback, already superseded before the FTS5 work even started
(`qapbot/docs/CODE_STRUCTURE.md:328` already calls it "superseded by in-memory index at runtime").
Grep confirms **zero production call sites** — only `tests/unit/test_db_search_players_by_name.py`
references it directly. Safe to delete along with its dedicated test file (or the relevant test
class within it, if that file also covers things kept from Step 1). Do this as its own small
changelog entry — it's unrelated to the player_name_index migration above and shouldn't be bundled
into the same commit/entry, to keep the history readable.

---

## Suggested delivery grouping

- **Batch A** (Steps 1-3): additive only, safe to ship independently. One changelog entry.
- **Checkpoint** (Step 4): DEV live-test, no code change, no changelog entry needed beyond a note
  once Batch B ships.
- **Batch B** (Steps 5-6): the actual retirement + flag decision — ask the user to confirm the
  Step 4 checkpoint and the flag decision before starting. One changelog entry, and this is the
  one that actually reduces PROD RSS/GC pressure — say so explicitly in the entry, since that's
  the whole point of the exercise.
- **Batch C** (Step 7): trivial, independent, can happen any time, its own tiny changelog entry.
