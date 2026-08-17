# CWL Management PROD Performance Fix Plan

Implementation plan for the 11 improvements (P0–P2) identified from the 2026-08-16 PROD test
session log analysis (`.\data\log\qapbot_PROD.log`). Each step is self-contained and ordered so the plan can
be executed top-to-bottom; P0 items fix the two observed production meltdowns, P1 items fix
responsiveness, P2 items buy headroom on the weak PROD box (10 GB RAM, 2-core Celeron J3355).

---

## Background: what the log showed (evidence, do not skip)

Two near-identical meltdowns, both triggered by the Guests search
(`GET /api/cwl/guest-search`, backend `_search_cwl_guests()` in `qapbot/web_bridge.py`):

- **Incident 1 (19:43:04–19:45:03)**: escalating automatic gen-2 GC pauses with `collected=0`
  (2.4s → 4.8s), 3× `[ERROR] get_player_links_sync failed: too many SQL variables`, 3× Discord
  gateway disconnects (event loop blocked past heartbeat deadline), climaxing in **one 11.79s GC
  pause that freed 13,747,172 objects**.
- **Incident 2 (19:54:30–19:59:30)**: same shape, 11.9s pause freeing 3,849,338 objects, 3 more
  SQL-variable errors, 2 more gateway disconnects.

Root causes confirmed by code inspection:

1. `_search_cwl_guests()` is `async def` but does ALL work synchronously **on the bot's event
   loop**. Per keystroke (client debounce 300ms, **no minimum query length**):
   - Clan pass iterates all ~431,800 `CACHE.clan_name_cache` entries AND fires one synchronous
     DB query per *matching* clan (`find_cwl_clan_participation_across_guilds_sync`) with no cap
     during the loop — the `[:12]` cap is applied only at the very end.
   - `#` tag mode iterates all ~6,657,664 `CACHE.player_name_index` entries
     (`tag.upper().startswith(...)`); a short prefix like `#2` matches millions of tags, all
     accumulated into an **unbounded** `player_hits` dict (→ the 13.7M freed objects).
   - The unbounded dict goes to `get_player_links_sync(list(player_hits.keys()))` → an
     `IN (?,?,…)` with millions of placeholders → `too many SQL variables` (SQLite limit).
2. `handle_get_cwl_player_stats` runs `get_recent_cwl_player_stats()` (3-month main+history SQL
   aggregation) synchronously on the loop, once per tile hover (~100+ calls in the session).
3. `GET /api/cwl/enrollment` is polled every 12s by the client (`main.ts` `POLL_INTERVAL_MS`);
   each poll runs `_build_enrollment_payload()` — a dozen sync DB queries plus a skill-score /
   avg-stars pass over 3 months of attacks for the whole player pool — on the loop.
4. `CACHE.search_player_names()` (`qapbot/cache_manager.py`) scans all 6.6M index entries calling
   `name.lower()` on each and materializes ALL matches before sorting/capping to 25.
5. Baseline: RSS 2.56→3.08 GB during the session; automatic gen-2 GC pauses of 1–3.5s even during
   normal update cycles. The 2026-08-16 (35) Pitfall-26 sweep fixed the web-bridge **write**
   paths with `asyncio.to_thread()`; the **read** paths were never covered.

---

## Mandatory project conventions (read before implementing anything)

- Read `.github/copilot-instructions.md` in full first. Rules that matter most here:
  - **Rule 11**: DB access via `CACHE.db_manager.*` only. **Rule 14**: named column access only
    (`row["col"]`), never positional. **Rule 1**: any query touching both `main.*` and
    `history.*` must name columns explicitly on both sides.
  - **Pitfall 26** (`qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md`): sync SQLite calls in async
    context must be bundled into `asyncio.to_thread()` hops — one hop per atomic unit of work.
    Follow the exact pattern already used by the write handlers in `qapbot/web_bridge.py`
    (see `handle_post_cwl_enrollment_assign`, `_prepare_and_save_clan_config_sync`).
- **Tests**: run with `.\run_tests.ps1` (never raw pytest). Add/extend tests in
  `tests/discord/test_web_bridge.py` (bridge endpoints), `tests/unit/test_db_manager_cwl_roster.py`
  (db_manager sync helpers), `tests/unit/` for cache_manager. Report the real pass count.
- **Changelog**: add one `changelog.txt` entry per delivered step (or per coherent group of
  steps), at the TOP, format `YYYY-MM-DD (N)`, including files touched and test count.
- **Docs (Rule 15)**: update `qapbot/docs/CWL_CLAN_CONFIG_ACTIVITY_PLAN.md` (and
  `COPILOT_PITFALLS_COOKBOOK.md` where a step generalizes a pitfall) in the same change.
- **Frontend deploys**: `activity/client` changes need `npm run typecheck && npm run build` and a
  Cloudflare Pages deploy (see `activity/README.md` for DEV/PROD commands). Backend (Python)
  changes need a bot restart on DEV/PROD — note this in the changelog entry.
- Backend/front-end contract types live in `activity/client/src/types.ts` — keep them in sync
  with any payload change.

---

## P0 — Fix the production meltdowns (guest search)

### Step 1: Bound the guest-search scans; enforce a server-side minimum query length

**File**: `qapbot/web_bridge.py`, function `_search_cwl_guests()` (~line 527).

**Current behavior**: full scans of `CACHE.clan_name_cache` (431K) and, in `#` mode,
`CACHE.player_name_index` (6.6M); unbounded `clan_hits` list and `player_hits` dict; `[:12]` caps
applied only after both scans complete.

**Target behavior**:
1. **Minimum query length, server-side** (do not rely on the client): after stripping the `@`/`#`
   prefix, require ≥ 2 characters of actual needle (i.e. `#2` → needle `2` → reject; `#2P` → OK;
   plain-text queries require ≥ 3 characters). Return `[]` for anything shorter. Choose the
   constants as module-level names (`GUEST_SEARCH_MIN_NEEDLE_TAG = 2`,
   `GUEST_SEARCH_MIN_NEEDLE_TEXT = 3`) so tests can reference them.
2. **Cap during iteration, not after**:
   - Clan loop: `break` once `len(clan_hits) == 12`. Collection order note: the current code
     collects in cache-iteration order and truncates identically, so an early break does not
     change which 12 results render — no sorting exists today. Keep it that way (document with a
     one-line comment).
   - `#` tag-mode player loop over `player_name_index`: `break` once 12 hits are collected
     (matching the final player cap of 12). Same for the non-tag path's Discord-display-name
     merge — stop adding once 12 total player hits exist.
   - Keep the existing "raw unindexed tag still returned" fallback behavior (tests
     `test_guest_search_raw_unindexed_tag_still_returned` must still pass) — only add the raw-tag
     synthetic hit when there is room under the cap OR replace the check so it is added
     regardless (it is a single entry; adding it above the cap to 13 then re-capping is fine —
     pick one and assert it in a test).
3. **Result**: `player_hits` can never exceed ~13 entries → `get_player_links_sync` never sees a
   big list; the 6.6M-iteration scan short-circuits as soon as 12 matches are found (a common
   prefix exits after a few thousand iterations; a rare prefix still walks the full index — that
   residual cost is addressed by Step 3's `to_thread` and P1 Step 9's index improvements).

**Tests** (extend `tests/discord/test_web_bridge.py`):
- Query shorter than minimum (plain, `@`, `#` variants) → 200 with `results: []`, and assert
  `get_player_links_sync` was NOT called (monkeypatch spy).
- A `#` prefix matching >12 indexed players → exactly 12 player hits returned.
- A text query matching >12 clans → exactly 12 clan hits, and assert
  `find_cwl_clan_participation_across_guilds_sync` called ≤ 12 times (spy) — this partially
  fronts Step 2 but write the assertion here.
- Existing guest-search tests must still pass unchanged (they use short but ≥-minimum queries —
  check each; if any uses a 2-char text query, lengthen the fixture names/queries rather than
  weakening the minimum).

### Step 2: Move the per-clan cross-guild DB check after the cap

**File**: `qapbot/web_bridge.py`, `_search_cwl_guests()` clan loop (~line 636).

**Current behavior**: `db.find_cwl_clan_participation_across_guilds_sync(clan_tag, season, …)` +
optional `QBcore.bot.get_guild(...)` executed inside the scan loop for every *matching* clan.

**Target behavior**: the scan loop only collects `{clan_tag, clan_name, clan_tier}` (pure cache
lookups). After the loop breaks at 12 (Step 1), run a second small loop over those ≤ 12 hits that
fills in `already_shared_with` via the DB query + guild-name resolution. Net effect: ≤ 12 DB
queries per search, ever.

**Tests**: covered by the call-count spy from Step 1; additionally keep
`test_guest_search_highlights_clan_already_on_another_guilds_roster` green (the annotation must
still appear on capped results).

### Step 3: Run `_search_cwl_guests` off the event loop + single-flight guard

**File**: `qapbot/web_bridge.py`, `_search_cwl_guests()` and its caller
`handle_get_cwl_guest_search` (~line 978).

**Target behavior**:
1. Split `_search_cwl_guests` into a plain synchronous function
   `_search_cwl_guests_sync(guild_id, query) -> List[Dict]` containing everything it does today
   EXCEPT the `await`-free async wrapper, and call it from the handler as ONE bundled hop
   (Pitfall-26 pattern):
   `results = await asyncio.to_thread(_search_cwl_guests_sync, guild_id, query)`.
   Note: `resolve_selected_cwl_season`, the `db.get_cwl_event_sync`/`get_cwl_event_clans_sync`
   pre-queries, the cache scans, and `get_player_links_sync` are all already synchronous — the
   whole body moves as-is. The only thing that must stay on the loop is nothing; there is no
   `await` inside the current body.
2. **Single-flight, newest-wins guard** per `(guild_id, discord_user_id)`: keep a module-level
   dict of the latest search generation number. The handler increments the generation before
   dispatching; when the thread completes, if a newer generation exists for that key, discard the
   result and return it anyway (cheapest correct behavior: still return the stale result — the
   CLIENT already discards stale renders via `searchRequestId`) **but** prevent *pile-up* by
   holding an `asyncio.Semaphore(1)` per key around the `to_thread` call so at most one search
   per admin runs at a time; queued keystrokes coalesce because each waiter re-checks "am I still
   the newest generation?" after acquiring and returns `{"results": [], "stale": true}`
   immediately if not. Add `stale` to the response shape; the client treats a `stale: true`
   response as ignorable (one-line change in `main.ts`'s guest-search callback: return `[]` —
   the request-id guard already prevents rendering).
3. Keep the `[WEB-BRIDGE]` completion log line unchanged (tests and ops grep for it).

**Tests**:
- Handler returns results identical to pre-change for a normal query (behavioral no-op).
- Concurrency test: fire two overlapping requests (second while first is blocked inside a
  monkeypatched slow `_search_cwl_guests_sync`); assert the first returns `stale: true` OR the
  sync function ran only once for the newest query (either assertion proves coalescing — pick
  the one that is deterministic under `asyncio` test harness).

### Step 4: Chunk `get_player_links_sync`'s `IN()` into ≤ 900-variable batches

**File**: `qapbot/db_manager.py`, `get_player_links_sync()` (~line 3710).

**Target behavior**: never fail on input size again. Split `player_tags` into chunks of 900,
execute the same query per chunk against ONE `self._sync_conn()` connection, merge rows, then
apply the existing verified-wins dedup across the merged rows. Preserve the
`ORDER BY verified DESC` semantics: dedup must consider all chunks' rows together (collect all
rows first, sort merged rows by `verified DESC`, then first-wins dedup — do NOT dedup per chunk,
a verified row in chunk 2 must beat an unverified row in chunk 1).

Audit siblings while there: `get_current_clan_tags_for_players_sync` and
`get_current_clan_members_sync` use the same `IN (placeholders)` construction — apply the same
chunking via one shared private helper `_chunked_in_query_sync(conn, sql_template, values)` if
trivially factorable; otherwise chunk each inline. (After Step 1 these should never see huge
lists, but Rule: defensive at the DB boundary.)

**Tests** (`tests/unit/test_db_manager_cwl_roster.py`):
- 2,000 tags (mix of linked/unlinked) → correct merged result, no error.
- Verified-vs-unverified rows for the same tag placed in different chunks → verified wins.

### Step 5: Frontend — min query length + AbortController

**Files**: `activity/client/src/clanConfigTable.ts` (search input handler, ~line 421),
`activity/client/src/main.ts` (guest-search fetch callback, ~line 261),
`activity/client/src/types.ts` (add optional `stale?: boolean` to the search response type if
Step 3.2's shape change is implemented).

**Target behavior**:
1. In the input handler: compute the effective needle exactly like the server (strip `@`/`#`,
   min 2 for tag/@ mode, min 3 for text) and do not fire the fetch below the minimum — clear the
   results list instead and show a subtle hint in `guestsStatus`
   ("Type at least N characters…"). Keep the 300ms debounce as-is.
2. In `main.ts`'s guest-search callback: create an `AbortController` per call, abort the previous
   in-flight request before firing the next, pass `signal` to `fetch`. Treat `AbortError` as a
   silent no-op (do not surface "Search failed" for aborts — extend the existing `.catch` in
   `clanConfigTable.ts` to ignore `err.name === 'AbortError'`).
3. Keep the existing `searchRequestId` stale-render guard — it still protects against
   out-of-order completions the abort didn't catch.

**Verify**: `npm run typecheck && npm run build` in `activity/client`; deploy to DEV Pages first,
click-test, then PROD (commands in `activity/README.md`).

**Status (2026-08-17, Batch 3): implemented.** `types.ts` got a named `GuestSearchResponse =
{ results: GuestSearchResult[]; stale?: boolean }` type (used in `main.ts`'s fetch callback rather
than inlined) instead of just adding `stale?` to an anonymous inline type — same effect, easier to
reuse. `main.ts`'s guest-search callback returns `[]` for a `stale: true` response before it ever
reaches the caller. Both `typecheck` and `build` clean; not yet deployed.

---

## P1 — Responsiveness of the enrollment board

### Step 6: `asyncio.to_thread()` for all remaining web-bridge READ handlers

**File**: `qapbot/web_bridge.py`.

**Scope** (audit every `async def handle_get_*` and payload builder for inline sync DB/cache-scan
work; the known offenders):
- `_build_enrollment_payload()` (~line 269) — split into a sync core
  `_build_enrollment_payload_sync(guild_id)` executed via one `to_thread` hop from
  `handle_get_cwl_enrollment`. The function body today contains no `await`; if any is introduced
  by Step 7's cache it stays outside the sync core.
- `_build_clan_config_payload()` (~line 150) — same treatment, called from the clan-config GET
  handler and (check!) from `handle_post_clan_config`'s response path; both call sites switch to
  the threaded sync core.
- `handle_get_cwl_player_stats` (~line 768) — wrap the `get_recent_cwl_player_stats(player_tag)`
  call: `await asyncio.to_thread(get_recent_cwl_player_stats, player_tag)`.
- `handle_get_cwl_guest_search` — already done in Step 3.
- While auditing: any other `db.*_sync(...)` call reachable from a GET handler without a
  `to_thread` wrapper gets the same one-hop treatment (e.g. the event lookups in enrollment
  signup GET paths, `get_cwl_event_sync` calls in read handlers).

**Rules**: one bundled hop per handler (not per query) — same rationale as changelog 2026-08-16
(35): per-call wrapping introduces interleaving windows. Do not wrap pure CACHE dict lookups that
are O(1); the hop is for the DB-query sequences and O(n) scans.

**Tests**: existing `test_web_bridge.py` suites must pass unchanged (they call the handlers
through the aiohttp test client, so threading is transparent). Add one regression test asserting
`_build_enrollment_payload_sync` is not a coroutine function
(`assert not asyncio.iscoroutinefunction(...)`) to prevent someone re-async-ifying the core.

**Docs**: extend Pitfall 26 in `qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md` with one sentence: the
rule covers READ paths too, not just writes (this incident is the proof).

### Step 6a: Debounce the hover pop-up itself (2026-08-17, added post-Batch-2, live-testing
feedback)

**File**: `activity/client/src/enrollmentBoard.ts`.

**Problem**: Step 6 moves the hover pop-up's two fetches (clan-name resolve, player-stats) off
the event loop, and Step 7 below caches repeat player-stats lookups — but neither reduces *how
many* fetches a single mouse sweep triggers in the first place. `buildCard()`'s `mouseenter`
listener called `showTooltip()` (and therefore both fetches, for any not-yet-attempted tag)
immediately, with no delay — so dragging the cursor across a filled column fired one clan-name +
one player-stats fetch per DISTINCT card merely brushed past on the way to somewhere else, not
just the one the admin actually paused on. Purely a frontend request-volume problem; nothing on
the backend can fix it.

**Target behavior** (implemented): a `HOVER_TOOLTIP_DELAY_MS = 1500` debounce. `mouseenter` no
longer calls `showTooltip()` directly — it calls a new `scheduleTooltip()`, which arms a
`setTimeout(..., HOVER_TOOLTIP_DELAY_MS)` that calls the real `showTooltip()` (pop-up display +
both fetches) only once it fires. `hideTooltip()` — already the `mouseleave`/`dragstart` handler —
clears that pending timer before removing any visible tooltip, so a fast sweep cancels the timer
before it ever fires: no pop-up, no fetch, for any card the cursor didn't actually pause on for
1.5s. A genuine pause still shows the pop-up and fires the fetches exactly as before.

**Tests**: no client test harness exists (see Step 5's own note) — `npm run typecheck && npm run
build` only. Verified clean.

**Docs**: none needed beyond this entry — no backend contract change, no new endpoint.

### Step 7: Cache `get_recent_cwl_player_stats` per player_tag (TTL)

**Files**: `qapbot/web_bridge.py` (preferred cache location: bridge-side, since the Discord-side
`/whois` caller may want always-fresh data — confirm `get_recent_cwl_player_stats`'s other
callers in `QBhelperfunctions.py` before deciding; if the only hot path is the bridge, cache in
the bridge).

**Target behavior**: module-level dict `_player_stats_cache: Dict[str, Tuple[float, dict]]`
(tag → (monotonic timestamp, payload)), TTL 15 minutes, consulted in
`handle_get_cwl_player_stats` before the `to_thread` hop; store after computing. Add a size guard
(evict oldest when > 2,000 entries — plain dict + popitem-on-overflow is fine; no external LRU
dependency). Invalidate wholesale (clear the dict) at the start of each update cycle IF trivially
hookable (there is an existing per-cycle cleanup — `[CYCLE-CLEANUP]` in `QapBot.py` — clear it
there via a small exported function `clear_player_stats_cache()`); otherwise TTL alone is
acceptable, stats change at most once per war round.

**Tests**: two sequential handler calls for the same tag → underlying
`get_recent_cwl_player_stats` called once (spy); TTL expiry (monkeypatch the clock) → called
again; cache cleared on `clear_player_stats_cache()`.

**Status (2026-08-17, Batch 4): implemented as designed.** Confirmed `get_recent_cwl_player_stats`
has exactly one caller anywhere (this handler) — no `/whois`/other caller exists today, so no
always-fresh-vs-cached split was needed. `clear_player_stats_cache()` wired into `QapBot.py`'s
`[CYCLE-CLEANUP]` hook. 3 new tests (repeat-lookup caching, TTL expiry via a monkeypatched clock,
explicit clear) — all pass.

### Step 8: Replace the 12s enrollment poll with event-driven long-polling

**Files**: `qapbot/web_bridge.py`, `activity/server/src/index.ts` (one new proxy route),
`activity/client/src/main.ts`, `activity/client/src/types.ts`.

**Design decision (2026-08-17)**: replace fixed-interval polling with an event-triggered
mechanism. Three candidates were evaluated against the delivery chain
(Discord Activity proxy → Cloudflare Worker → cloudflared tunnel → aiohttp bridge):

- **WebSockets** — rejected: true push, but breaks the Worker's per-request `/users/@me`
  identity re-verification model (needs per-connection auth), adds connection-lifecycle
  management (heartbeats, reconnects, stale cleanup) on all four hops, and pins a Worker
  instance per connection. Overkill for "board changed, refetch" with 1–3 concurrent admins.
- **Server-Sent Events** — rejected: keeps per-request auth, but requires the Worker to switch
  from buffered `c.json(await upstream.json())` to a streaming pass-through, and any silent
  buffering at the Discord proxy breaks SSE invisibly (events sit in a buffer — brutal to debug
  remotely). Cloudflare's ~100s idle timeout would also force keepalive frames.
- **Long-polling (hanging GET)** — **chosen**: genuinely event-triggered (the bridge releases
  the parked request the instant a write happens → sub-second board updates), yet every hop
  only ever sees a normal HTTP request that takes up to ~25s. No streaming, no protocol
  upgrade, no buffering concerns, per-request auth unchanged. Idle cost: one parked asyncio
  coroutine on the bridge and one Worker `/users/@me` verification per ~25s per open board —
  LESS Discord API traffic than today's 12s poll.

The version counter below is a prerequisite for ANY of the three options (reconnect/resync
correctness after a bot restart or tunnel blip) — it is not polling-specific machinery.

**Target behavior**:
1. **Backend version counter**: module-level `_enrollment_version: Dict[str, int]`
   (guild_id → int) in `web_bridge.py`. Every write path that mutates what the enrollment payload
   renders bumps it: enrollment signup/assign handlers, guest add, clan-config save, shared-clan
   evict/detach, Start Enrollment — i.e. every `handle_post_*` in the bridge that already calls
   `refresh_cwl_management_hub_message`, plus the Discord-side write paths in
   `qapbot/QBdiscocmdshelper_cwl.py`/`qapbot/ui_cwl_roster.py` that touch cwl_signups /
   cwl_assignments / cwl_event_clans (export a `bump_enrollment_version(guild_id)` from
   `web_bridge.py` and call it next to each existing hub-refresh call — the hub-refresh call
   sites are exactly the right hook points, they already mark "board-visible state changed").
   Note: cross-guild shared clans mean a write in guild A can change guild B's board — bump the
   version for the OWNER guild and every sharing guild where the call site already knows them
   (the shared-clan write paths already resolve the partner guild for the hub refresh; reuse
   that). Where the partner set is not already resolved, bump globally (increment all known
   guilds' counters or keep one global counter) — a spurious wake-up only when a write happened
   anywhere is still a ~99% reduction versus today.
2. **Notification primitive**: alongside the counter, keep a per-guild `asyncio.Condition`
   (`_enrollment_changed: Dict[str, asyncio.Condition]`). `bump_enrollment_version(guild_id)`
   increments the counter AND `notify_all()`s that guild's condition. IMPORTANT: bump is called
   from both the event loop (bridge handlers) and potentially from Discord-side callbacks — the
   Condition must only be touched from the event loop; if any bump call site runs inside a
   `to_thread` sync core, expose the bump as loop-safe via
   `asyncio.get_running_loop().call_soon_threadsafe(...)` or bump AFTER the `to_thread` hop
   returns (prefer the latter — it keeps the sync cores pure; audit each call site).
3. **New wait endpoint (bridge)**: `GET /api/cwl/enrollment/wait?guild_id=…&discord_user_id=…
   &known_version=N` — same admin-or-leader gate as the enrollment GET. If the current version
   already differs from `known_version`, respond immediately `{"changed": true, "version": M}`.
   Otherwise park on the guild's Condition with `asyncio.wait_for(..., timeout=25)`; on notify
   → `{"changed": true, "version": M}`; on timeout → `{"changed": false, "version": N}`. The
   response deliberately carries NO payload — the client refetches the full enrollment payload
   via the existing GET on `changed: true` (keeps the wait handler allocation-free and avoids
   duplicating the payload-builder path). Cap parked waiters per guild (e.g. 10) — beyond that,
   respond `{"changed": true}` immediately to degrade gracefully rather than accumulate
   coroutines. 25s stays safely under Cloudflare's 100s and the Discord proxy's limits.
   **Hold-duration verification (do this FIRST, before building the client loop)**: verified
   2026-08-17 — `web_bridge.py` configures no timeout middleware and aiohttp's server does not
   time out in-flight handlers by default (`keepalive_timeout` only applies to idle connections
   BETWEEN requests), so no bridge config change is expected. But the 25s hold crosses four hops
   (Discord proxy → Worker → cloudflared → aiohttp), so empirically verify end-to-end on DEV
   with a throwaway handler that sleeps 25s before responding, called from inside the real
   Activity. If any hop cuts the connection, lower the hold to the longest value that survives
   (even 15s retains the design's benefits) and record the measured limit in a code comment on
   the timeout constant.
4. **Worker route** (`activity/server/src/index.ts`): add `GET /cwl/enrollment/wait`, identical
   verify-identity-then-proxy shape as the existing `/cwl/enrollment` route (the upstream fetch
   simply takes up to 25s; Worker wall-clock waiting on origin I/O is not billed CPU time and is
   free-plan safe).
5. **Client** (`main.ts`, replacing the `setInterval` poll at ~line 215): a `while`-style async
   wait loop — call `/api/cwl/enrollment/wait` with the last seen version; on `changed: true`
   refetch the enrollment payload and hand it to `applyPolledUpdate` (the existing mid-drag
   deferral is reused as-is); on `changed: false` immediately re-issue the wait. Error handling:
   on ANY wait failure (bot restart, tunnel blip, non-200), back off (5s → 10s → … max 60s) and
   fall back to re-issuing the wait; as a safety net, if the wait endpoint fails 3× in a row,
   degrade to a plain 60s versioned poll of the enrollment GET until a wait succeeds again.
   Pause the loop when `document.visibilityState === 'hidden'`; resume + immediate refetch on
   `visibilitychange` to visible. Clean up (abort in-flight wait via `AbortController`) in the
   same place the old `clearInterval(pollTimer)` ran (~line 172).
6. The enrollment GET also returns `"version": current` in its payload (the wait loop's starting
   point). `types.ts`: add `version` to `EnrollmentPayload` and a `WaitResponse` type.

**Tests** (bridge, `tests/discord/test_web_bridge.py`):
- Wait with stale `known_version` → immediate `{"changed": true}`.
- Wait with current version + a concurrent POST assign → resolves promptly with
  `{"changed": true}` (use `asyncio.wait_for` in the test with a short bridge-side timeout).
- Wait with current version and no write → `{"changed": false}` after the timeout (monkeypatch
  the timeout constant to ~0.1s for the test).
- Waiter-cap overflow → immediate `{"changed": true}`.
- Cross-guild bump: a shared-clan write in guild A releases guild B's parked waiter.
Frontend: typecheck/build only (no test harness exists for the client).

**Status (2026-08-17, Batch 4): implemented, backend verification pending live Discord testing.**
- Version counter: per-guild (`Dict[str, int]`, string keys matching this module's own
  convention), not a single global counter — `bump_enrollment_version(guild_id)` for the acting
  guild, `bump_enrollment_version(None)` (bumps every currently-tracked guild) as the
  plan-sanctioned simpler fallback for write paths where resolving the exact cross-guild partner
  set isn't already cheap (assign's conflict-purge, detach-on-deactivate, Delete Season's
  prune-or-detach); precise per-guild targeting used wherever the partner guild(s) were already
  at hand for free (shared-clan evict's `target_guild_id`, `ensure_cwl_clan_sharing`'s own
  `other_guild_ids` return value in both its trigger points — `handle_post_clan_config` and Start
  Enrollment). 10 total call sites wired: 5 in `web_bridge.py` (signup, assign, guest, clan-config
  save, shared-clan evict) + 5 in `ui_cwl_roster.py` (season select, Add New Season ×2, Delete
  Season, Start Enrollment). `handle_post_cwl_activity_closed` deliberately excluded — see its own
  docstring — it refreshes unconditionally on every close regardless of whether anything changed,
  so bumping there would wake every parked waiter for a plain Cancel/back-gesture close.
- Client backoff/degrade (main.ts): implemented as ONE loop rather than two separate code paths —
  exponential backoff (5s→10s→20s→40s→60s cap) on any wait failure, and once 3+ failures have
  accumulated, each backoff cycle ALSO does a plain GET refetch (so the board doesn't go stale for
  the whole backoff duration) before retrying `/wait` again next cycle — achieves the same
  "degrade to ~60s polling until a wait succeeds again" outcome the spec describes without a
  separate degraded-mode branch. Visibility pause checks `document.visibilityState` between loop
  iterations (doesn't abort an in-flight wait mid-request); resume does one immediate refetch.
- 8 new bridge tests (stale version, timeout, resolves-via-real-POST, waiter-cap overflow,
  cross-guild release via a real shared-clan signup, version field present, activity-closed
  non-bump regression) — all pass. `npm run typecheck && npm run build` clean for both
  `activity/client` and `activity/server`.
- **NOT yet done**: the hold-duration verification this step's own design notes call for doing
  FIRST (empirically confirming the ~25s hold survives Discord proxy → Worker → cloudflared →
  aiohttp intact) — this requires a live Discord Activity session, which isn't something that can
  be exercised outside one. Deployed to DEV for that live test; until confirmed, treat the 25s
  constant as unverified. The client's own 3-failures-and-degrade safety net means a hop that
  silently truncates the hold would show up as: `/wait` responses returning near-instantly
  instead of holding, but should NOT break the board (it'd just settle into noisier polling until
  the constant is lowered here per this step's own instructions).

### Step 9: Fix `search_player_names` scan cost

**File**: `qapbot/cache_manager.py`, `search_player_names()` (~line 434) and the
`player_name_index` load path (`load_all()` → the `[DB-READ] Loaded … player_name_index` step).

**Target behavior** (in-memory, incremental — the FTS move is Step 11):
1. **Stop lowercasing 6.6M names per call**: maintain lowercase forms built once at load time.
   Memory tradeoff on the 10 GB box is real, so do NOT duplicate all 6.6M names into a second
   dict (+~600 MB — not acceptable). Chosen variant: values become `(name, name_lower)` tuples
   (a tuple of two strings costs far less than a second dict, and for the very common
   already-lowercase name, store the SAME string object twice — `name_lower = name if
   name == name.lower() else name.lower()` at build time — so most entries add only one tuple,
   no new string). This is a breaking internal-shape change; the complete audited list of
   production access sites (verified 2026-08-17) is:
   - `qapbot/cache_manager.py:234` — declaration `self.player_name_index: Dict[str, str]`
     (type annotation changes to `Dict[str, Tuple[str, str]]`).
   - `qapbot/cache_manager.py:424` — bulk load via
     `db_manager.load_player_name_index_sync` (build tuples HERE, inside the loader, so the
     `to_thread` hop pays the one-time lowercase cost, not the event loop).
   - `qapbot/cache_manager.py:432` — error-fallback reset `= {}` (no change needed).
   - `qapbot/cache_manager.py:449` — reader in `search_player_names()`.
   - `qapbot/coc_cache.py:678` — incremental writer in `update_player_info_in_user_accounts()`.
   - `qapbot/web_bridge.py:664` — reader in `_search_cwl_guests()` `#` tag mode (values unused
     there except as name — adjust unpacking).
   - Tests that inject the index directly: `tests/discord/test_web_bridge.py` (multiple
     fixtures), `tests/unit/test_coc_cache_coverage.py:495` — update fixtures to tuples.
   To keep future writers honest, add a single writer helper
   `CACHE.set_player_name(tag, name)` that builds the tuple, and switch the `coc_cache.py`
   writer to it; readers may unpack directly (documented in the attribute's docstring).
2. **Early-exit with bounded collection**: collect at most 200 matches (module constant), then
   sort those alphabetically and cap at `limit` (≤25). Semantics change: with >200 matches the
   returned 25 are no longer the globally-alphabetically-first 25 — document this in the
   docstring ("best-effort under heavy match counts"); for /whois-style lookups this is
   irrelevant in practice (the user refines the query).
3. Fix the docstring's "no asyncio.to_thread needed" claim: with (1)+(2) a worst-case scan is a
   6.6M-iteration substring check without per-entry allocations (~1s on the Celeron worst case).
   Callers on the event loop should still thread it: `/whois` (`QBhelperfunctions.py`) and the
   guest search (already threaded after Step 3). Wrap the `/whois` call site in
   `asyncio.to_thread`.

**Tests** (`tests/unit/`): tuple-shape index round-trip (writer→reader); early-exit bound
respected; results still sorted and capped; guest-search integration tests still green.

**Status (2026-08-17, Batch 5): implemented, with one correction to the plan's own audit.** The
"/whois (`QBhelperfunctions.py`)" reference above is wrong — verified 2026-08-17: `/whois`'s
name-substring path is implemented in `QBdiscordcmds.py`'s `whois_slash`, and it does **not**
call `search_player_names()` at all — it has its own separate, inline, previously-uncapped scan
over `player_name_index` (needed because `search_player_names()`'s 25-cap would cut off guild
members that sort later alphabetically, before the guild-membership reorder that follows). That
inline scan was *also* re-lowercasing every name on every call — the exact cost Step 9 exists to
fix — so it needed the same treatment as `search_player_names()`, just at its real location:
extracted into `_search_player_name_index_sync()` (still uncapped, now reads the precomputed
`name_lower` from the tuple instead of calling `.lower()`), wrapped in `asyncio.to_thread()` at
the `whois_slash` call site. All other target-behavior items implemented as specified:
`CACHE.set_player_name()` writer helper, `SEARCH_PLAYER_NAMES_MAX_COLLECT = 200` early-exit,
`_load_player_name_index_sync()` building tuples inside the `to_thread` hop. Also corrected a
stale "~125K entries" estimate in two docstrings (`cache_manager.py`, `db_manager.py`) that
predated the growth to PROD's actual ~6.6M-row scale evidenced in the incident log. 6 new tests
(early-exit-200 proof via reverse-insertion ordering, writer→reader round trip, loader tuple
construction) — 2025 tests pass.

---

## P2 — Headroom on the PROD box

### Step 10: GC tuning for burst allocations

**File**: `QapBot.py` (the existing `[GC-AUTO]` logger + startup `gc.freeze()` block, ~line 121,
and the `[CYCLE-CLEANUP]` end-of-cycle hook).

**Target behavior** (conservative, measurable — this is mitigation, not the fix; P0 removed the
allocation source):
1. Raise gen-2 thresholds: `gc.set_threshold(700, 10, 20)` → keep gen0/gen1 defaults, raise the
   gen-2 multiplier (third value) from 10 to ~20 so full sweeps run half as often. Set it once at
   startup next to the existing GC setup, with a comment citing this incident and the log
   evidence (escalating `collected=0` gen-2 sweeps at 1.5–5.5s each).
2. Nightly re-freeze — **verified 2026-08-17: this does NOT exist yet and must be implemented**
   (the only `gc.freeze()` in the codebase is the startup one, QapBot.py ~line 121; the nightly
   `run_nightly_maintenance_routine()` at QapBot.py ~lines 2381–2462 never re-freezes). Add
   `gc.collect()` (full, all generations — clean out accumulated cyclic garbage first) followed
   by `gc.freeze()` at the END of the nightly maintenance routine, mirroring the startup
   sequence. Respect the cookbook's caveat (Pitfall 21): frozen objects are excluded from cycle
   collection forever, which is why the cadence is per-NIGHT (after a full collect), never
   per-cycle. Log a `[GC-FREEZE]` line with the frozen-object count, same format as startup.
   Update Pitfall 21 in `qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md` in the same change: document
   the nightly re-freeze AND the chosen `gc.set_threshold` values from sub-step 1.
3. Do NOT disable automatic GC and do NOT freeze inside the update cycle.

**Verify**: no unit-testable behavior; validate on DEV by watching `[GC-AUTO]` frequency/duration
across a few cycles before/after (log-diff is the acceptance criterion; note findings in the
changelog entry).

**Status (2026-08-17, Batch 6): sub-step 1 implemented; sub-step 2 was already done.** Another
correction to the plan's own audit (same pattern as Step 9's `/whois` finding): verified
2026-08-17 by reading `QapBot.py` directly (not just searching `COPILOT_PITFALLS_COOKBOOK.md`,
which is presumably what the "does NOT exist yet" claim was based on) — the nightly re-freeze
**already exists**, added 2026-08-08 ("Issue 3", `run_nightly_maintenance_routine()`'s Step 5:
`gc.unfreeze()` + full `gc.collect()` + `gc.freeze()`, logged as `[NIGHTLY-MAINTENANCE] GC
refresh: ...` rather than a `[GC-FREEZE]`-prefixed line) — just never written up in the cookbook.
Left the existing mechanism and its log tag as-is (working, tested-in-prod-by-time code); added
`gc.set_threshold(700, 10, 20)` at startup (the one genuinely-missing piece) right before the
existing startup freeze. Documented both as two new "Follow-up" paragraphs under Pitfall 21
(cookbook) instead of editing Pitfall 21's original incident writeup. No unit tests (per this
step's own note) — DEV `[GC-AUTO]` frequency/duration validation still pending, same as the
Step 8 hold-duration verification: needs live observation across real cycles, noted here rather
than blocking the implementation on it.

### Step 11: Move player-name substring search into SQLite (longer-term)

**Files**: `qapbot/db_manager.py` (new table + query helpers), `qapbot/cache_manager.py`
(`search_player_names` delegates; `player_name_index` load/retention decision), `QapBot.py`
(migration hook in `initialize_database()`), consumers (`QBhelperfunctions.py` /whois, guest
search).

**Design** (implement behind a working fallback; this is the largest step — do it last):
1. New table `player_name_search(player_tag TEXT PRIMARY KEY, name TEXT, name_lower TEXT)` with
   an index on `name_lower` — populated from the same source that fills `player_name_index`
   today. For substring (`LIKE '%needle%'`) an index doesn't help; therefore use **FTS5**:
   `CREATE VIRTUAL TABLE IF NOT EXISTS player_name_fts USING fts5(player_tag UNPREFIXED, name)`
   with trigram tokenizer (`tokenize='trigram'`) — SQLite ≥ 3.34 supports trigram FTS5 and makes
   substring queries index-backed. **Feasibility gate first**: check the PROD SQLite build
   supports FTS5+trigram (the `[DB-INIT] mmap_size capped by SQLite build` line proves it's not a
   fully stock build — probe with `pytest`-style runtime check AND ask the project owner to
   confirm the PROD binary before migrating; if unsupported, fall back to the `name_lower` table
   + bounded `LIKE` scan in SQL, which still beats Python-side iteration on memory but not
   necessarily CPU).
2. Migration is idempotent (Rule 12): `CREATE ... IF NOT EXISTS` + a one-time backfill guarded by
   a row-count comparison against `player_name_index`'s source table, placed in the existing
   migration block in `db_manager.py`.
3. Writers: everywhere `player_name_index` is updated incrementally (grep `coc_cache.py` and
   `war_notifications.py` for index writes) also upsert the FTS row — batched inside the existing
   war-save transactions, NOT row-by-row autocommit.
4. Readers: `search_player_names` becomes a thin sync DB query (`to_thread` from async callers,
   per Step 9.3) with `LIMIT 25`. Guest-search `#` tag mode switches from index iteration to
   `WHERE player_tag LIKE ?||'%' LIMIT 12` on the plain table (tag prefix IS index-friendly on
   the PK — no FTS needed for tags).
5. **Retention decision**: keep `player_name_index` in RAM initially (dual-write, read from DB)
   for one release as a safety net; drop the in-memory index (≈1 GB RSS saving, the actual P2
   payoff) only after DEV+PROD burn-in confirms parity. Wire a config flag
   (`CONFIG`-driven, default keep) so the drop is a config change, not a code change.

**Tests**: migration idempotency (double-init), backfill correctness, search parity between the
in-memory and DB implementations on a seeded fixture, tag-prefix search cap, and the
`run_tests.ps1` full suite.

---

## Pre-implementation verification notes (docs/backlog sweep, 2026-08-17)

A full sweep of `.github/copilot-instructions.md`, `qapbot/docs/*.md`, `activity/README.md`,
`CWL_ROSTER_PLANNING_PLAN.md`, `backlog.txt`, and the touched modules was done before green-lighting
this plan. Findings already folded into the steps above; recorded here so the implementer doesn't
re-derive them:

- **Backlog check (playbook "Before Writing Code" step 6)**: no backlog entries must be folded
  into this plan. Closest matches — `compute_roster_stats_sync` sequential-aggregates (only fold
  if Step 11 happens to edit that function) and `executemany()` for bulk user writes (different
  functions than we touch). The DM-send-guard backlog entry ("remove the PROD feature toggle
  after PROD testing") is unrelated to this plan — flag it to the project owner separately, do
  not act on it here.
- **`asyncio.Condition` has no precedent in the codebase** (only `asyncio.Event`, 5 instances in
  `QBcore.py`). Step 8 introduces the first one — fine, but don't go looking for an existing
  pattern to copy; the Events in QBcore.py show the naming/registration style to follow.
- **`[CYCLE-CLEANUP]` hook** (Step 7's cache clear): QapBot.py ~lines 2312–2330 in
  `periodic_main()` — already does `clear_expired` + `gc.collect(1)` + `malloc_trim`; add the
  player-stats cache clear there.
- **No FTS5/SQLite-version runtime probe exists anywhere yet** — Step 11's feasibility gate is
  net-new code (`sqlite3` `pragma compile_options` / trial `CREATE VIRTUAL TABLE` in a temp DB).
- **aiohttp server config**: `web_bridge.py` has no timeout middleware; aiohttp does not kill
  in-flight handlers by default, so Step 8's hold needs no bridge config change — but MUST be
  verified end-to-end through the proxy chain on DEV first (see Step 8's hold-duration
  verification sub-step).

---

## Suggested execution order & batching

| Batch | Steps | Rationale |
|-------|-------|-----------|
| 1 | 1, 2, 4 | Pure backend, kills the meltdown mechanics; deployable alone |
| 2 | 3, 6, 6a | Threading sweep for all bridge reads, plus the hover-tooltip debounce found during Batch 2 review (frontend-only, no backend dependency — folded in rather than deferred to Batch 3 since it's this small) |
| 3 | 5 | Frontend guard rails; deploy Pages DEV→PROD |
| 4 | 7, 8 | Caching layer + event-driven long-poll (touch the same handlers) |
| 5 | 9 | cache_manager index shape change (isolated, riskier — own batch) |
| 6 | 10 | GC tuning, validated by DEV log observation |
| 7 | 11 | FTS migration, feature-flagged, after everything else is stable |

Each batch: run `.\run_tests.ps1`, add the `changelog.txt` entry (top, dated, with test count),
update the docs listed in the step, and note "needs bot restart on DEV/PROD" for backend batches
or the Pages deploy URL for frontend batches.

## Acceptance criteria for the whole plan

1. A `#`+1-char or 1-char guest search returns instantly with an empty result and no backend scan.
2. No guest search can block the event loop: no `[DISCORD-WS] Gateway disconnected` and no
   `[GC-AUTO]` pause > ~2s attributable to a search, verified on DEV with a deliberate
   worst-case query while a cycle runs.
3. `too many SQL variables` can no longer occur from any bridge path (chunked IN()).
4. Enrollment board steady-state (no writes) triggers zero payload rebuilds (parked wait
   requests only, log/spy-verified); a write reaches an open board in under ~2s.
5. Hovering the same player twice within 15 min computes stats once.
6. Full test suite passes via `.\run_tests.ps1`; frontend `npm run typecheck && npm run build`
   clean.
