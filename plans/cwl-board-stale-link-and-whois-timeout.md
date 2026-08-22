# Implementation plan: stale `cwl_signups.discord_id` (grey "Not Linked" board tiles) + `/whois` timeouts

## Context

Two independent bugs reported 2026-08-22 from live PROD:

1. **B.A.B.A renders grey ("Not Linked") on the CWL enrollment board** even though `/whois` shows it
   correctly linked to `.zuurn` (Odin | StayCalm, `543518173421371392`).
2. **`/whois` logs an `asyncio.TimeoutError`** (via `CancelledError`) for `#YCGCC9P20` and takes 30s
   to complete.

They share no root cause. Both were verified against the copied-PROD DB (`data/qapbot.db`, the
2026-08-22 pre-deploy backup) and the PROD logs for 2026-08-18 → 2026-08-22.

### A note that invalidated the first line of investigation

`user_players.added_at` is **not** a "when was this linked" timestamp. `save_user()` →
`_replace_user_players_rows()` (`db_manager.py:8445`) does `DELETE FROM user_players WHERE
discord_id = ?` followed by a full re-insert, so `added_at` resets to *now* on **every** persist of
that user — including the ones the routine clan-poll cycle triggers via
`_apply_member_field_updates` → `persist_user(uid)`.

That made all 82 of `.zuurn`'s rows share `added_at = 2026-08-21 22:41:49`, which reads exactly like
a bulk ownership hijack and is nothing of the sort — it is one ordinary `persist_user` call during
the 00:41 local clan poll. **Do not use `added_at` as evidence of when a link was created.** The
real link events are in the log (`USER ACTION: .zuurn skipped verification for player ...`), and
they confirm `.zuurn` linked these accounts himself on 08-18 and 08-21. `user_players` is correct
and current; nothing is corrupted there.

Also note the log timestamps are local (UTC+2) while `added_at` uses SQLite `datetime('now')` (UTC).
`22:41:49` in the DB is `00:41:49` local the following day.

---

## Bug 1 — the board's `discord_id` is a frozen enrollment-time snapshot

### Root cause (verified)

`_build_enrollment_payload` in `qapbot/web_bridge.py` seeds `players_by_tag` from
`cwl_signups` **first** (`web_bridge.py:483-493`):

```python
for signup in db.get_cwl_signups_for_event_sync(event["id"]):
    players_by_tag[signup["player_tag"]] = {
        ...
        "discord_id": signup["discord_id"],
```

and only then falls back to the live `user_players` source, explicitly skipping any tag it already
has (`web_bridge.py:513`): `if member["player_tag"] not in players_by_tag:`.

`cwl_signups` is written once, at Start Enrollment, and never refreshed. For guild
`1224425088913248467` (event 18) the signup rows were created **2026-08-18 07:57** (126 rows) and
2026-08-19 18:57 (3 rows). `.zuurn` linked B.A.B.A at **2026-08-18 14:26** — six and a half hours
*after* the snapshot:

```
2026-08-18 14:26:20 [INFO] USER ACTION: .zuurn skipped verification for player B.A.B.A (#2RPLRVUG9)
```

So `cwl_signups.discord_id` is `NULL` for `#2RPLRVUG9` while `user_players` has the real link, and
the board faithfully renders the stale `NULL` as grey via
`activity/client/src/enrollmentBoard.ts:794` (`if (player.discord_id == null)`).

**The giveaway is that `discord_id` is the only field left frozen.** The same function already
re-resolves every other snapshot-prone field against live data at the end of the loop
(`web_bridge.py:645-678`): `th_level` has a three-step fallback chain, `current_clan_tag` has a
player-scoped fallback, `cwl_permanent_optout` has a default, and `is_guest` is *overridden
outright* whenever a live current clan is known. `discord_id` was simply never given the same
treatment.

### Scope in the live data

| event | signup `NULL` but live-linked | signup owner ≠ live owner | signup set but now unlinked | total |
|---|---|---|---|---|
| 18 (`CoC \| Stay`) | 17 | 8 | 2 | 129 |
| 27 | 1 | 0 | 0 | 107 |
| 28 | 0 | 0 | 0 | 30 |

The 17 `NULL`-but-linked rows are the grey tiles the report is about (B.A.B.A, InClimber, Am.Glizzy,
Nova Star, Robin, …). The 8 owner-mismatches are the same staleness in its other direction —
accounts that belonged to `780540764198666280` / `605029020727377930` / `328230192491528203` at
snapshot time and have since been (legitimately) re-linked to `.zuurn`.

### The same staleness also mis-routes DMs — verified, not hypothetical

`resolve_cwl_pool_dm_targets_sync` (`QBdiscocmdshelper_cwl.py:2470-2510`) merges four sources into
one pool using **first-non-None-wins**:

```python
entry["discord_id"] = entry["discord_id"] or discord_id
```

with the order: (1) live `get_current_clan_members_sync`, (2) `cwl_signups`, (3) shared-clan
players, (4) `get_player_links_sync(unknown_tags)`.

Source 1 correctly wins for any player currently in a pool clan. But source 4 — the live
`user_players` authority, added precisely to fix missing links — runs **last**, so the `or` means it
can never *correct* a stale value that source 2 already supplied. A player with a signup row who is
in no pool clan keeps the stale owner permanently.

That case exists right now. `#29JQV2YCL` (OneEyeGhoul) has `current_clan_tag IS NULL`, so
`get_current_clan_members_sync` does not return it; its signup row names
`605029020727377930` (Phover) while `user_players` names `543518173421371392` (`.zuurn`). A
"Notify New Pool Members" run today would **DM Phover about `.zuurn`'s account.**

Finally, the staleness is self-perpetuating: `CwlSignupResponseButton`'s callback re-writes the same
stale value back on every response (`ui_cwl_roster.py:1369`, `signup.get("discord_id")`), and its
ownership guard at `ui_cwl_roster.py:1349` compares the clicking user against that stale snapshot —
so the *true current owner* can be told "not your signup".

### Fix

**1a. `web_bridge.py` — re-resolve `discord_id` live, mirroring the existing `is_guest` override.**

Batch-resolve once via `db.get_player_links_sync(list(players_by_tag.keys()))` — it is already
chunked (`_chunked_in_query_sync`), already applies the verified-wins + UNASSIGNED tiebreak from
tracker #0013, and already maps `'UNASSIGNED'` → `None`. In the existing
`for player_tag, player in players_by_tag.items():` loop (`web_bridge.py:646`), set:

```python
link = links_by_tag.get(player_tag)
if link is not None:
    player["discord_id"] = link["discord_id"]
```

Override, don't fall back — `user_players` is the authority for "is this account linked", exactly as
the surrounding code treats a live `current_clan_tag` as the authority for `is_guest`. Leave the
snapshot value in place only when the tag has no `user_players` row at all (a guest tag added by
search that was never linked), matching how `cwl_permanent_optout` degrades.

Note `optout_by_tag` (`web_bridge.py:511`) is currently only populated from
`get_current_clan_members_sync`, so it has the identical clan-scoped blind spot; `get_player_links_sync`
returns `cwl_permanent_optout` too, so the same one call can close both. Fold that in — it is the same
line of code and the same bug class.

**1b. `QBdiscocmdshelper_cwl.py` — make the live link authoritative in `_merge`.**

The minimal correct change is to let source 4 override rather than defer. Either give `_merge` an
`authoritative: bool = False` parameter that assigns instead of `or`-ing, or simply drop the source-4
results into `pool` directly after the merge loop. Do **not** just reorder the sources — source 1 is
also live and must keep winning over source 2, and `player_name` should retain its current
first-wins behaviour (the signup's recorded name is the one the DM text should use).

**1c. `ui_cwl_roster.py` — stop trusting the snapshot for the ownership check.**

At `ui_cwl_roster.py:1349`, resolve the live owner (`db.get_player_links_sync([self.player_tag])`,
wrapped in `asyncio.to_thread` — Pitfall 26; there are already two `to_thread` reads immediately
above it to piggyback on) and accept the click if the user matches **either** the live owner or the
snapshot owner. Accepting both is deliberate: the DM was genuinely delivered to the snapshot owner,
so their button must keep working, while the current owner must not be locked out. Then persist the
**live** id in the `upsert_cwl_signup_sync` call at line 1369 so the row self-heals.

This is an account-protection surface (Cardinal Rule 2) — the guard must still reject a third party
who is neither owner. Do not relax it to "any linked user".

**1d. One-off backfill — deliberately NOT included.**

A `UPDATE cwl_signups SET discord_id = (live owner)` migration is tempting and should be skipped.
Once 1a/1b/1c land, every read path resolves live and the stale column stops mattering; a backfill
would rewrite rows for three live events mid-season to fix data that is no longer read. Revisit only
if a read path is found that genuinely cannot resolve live.

### Tests

- `tests/discord/test_web_bridge.py` — a player with a `cwl_signups` row carrying `discord_id=None`
  plus a live `user_players` link renders with the live `discord_id` (the B.A.B.A case); a player
  whose signup names a *different* owner renders the live owner; a signup-only tag with no
  `user_players` row keeps `discord_id=None` and does not crash. Note `test_web_bridge.py:4021`
  already asserts on `signup["discord_id"]` — check whether that expectation needs updating.
- `tests/unit/` (or wherever `resolve_cwl_pool_dm_targets_sync` is covered) — a pooled player with
  **no `current_clan_tag`** and a stale signup `discord_id` resolves to the live owner. This is the
  OneEyeGhoul case and the current code fails it.
- `ui_cwl_roster` button callback — live owner accepted, snapshot owner accepted, unrelated third
  user rejected; and the upsert writes the live id.

### Docs

Add a `COPILOT_PITFALLS_COOKBOOK.md` pitfall for the general lesson, which is the actually
transferable part: **`cwl_signups` / `cwl_shared_clan_players` are enrollment-time snapshots, not
live state. Any field that can change after Start Enrollment — `discord_id` above all — must be
re-resolved from `user_players` on read.** Cross-reference the existing `th_level` /
`current_clan_tag` / `is_guest` fallback chains in `_build_enrollment_payload` as the pattern to
copy. Also record the `added_at`-is-not-a-link-timestamp trap from the Context section above — it
cost this investigation a full wrong hypothesis.

---

## Bug 2 — `/whois` fetches every account sequentially with an 8s per-account timeout

### Root cause (verified)

`_whois_logic` (`QBdiscordcmds.py:4721-4810`) loops over the user's accounts and awaits one CoC API
call per account, in series:

```python
for player in players:
    ...
    player_obj = await asyncio.wait_for(CACHE.get_player(player_tag), timeout=8.0)
```

`CACHE.get_player` (`cache_manager.py:1424`) has **no cache** despite living in `cache_manager.py` —
every call is a live API round-trip through `coc_retry`.

`.zuurn` has 82 linked accounts, so one `/whois` is 82 serialised API calls. The reported run took
30s wall clock (09:54:06 → 09:54:36) and the log shows individual calls at 2.18s and 4.46s
(`[COC-API-SLOW]`) because the background clan-poll cycle is saturating the API at the same time.
One call exceeded 8s, `wait_for` cancelled it, and the `CancelledError` surfaced as `TimeoutError`.

Three distinct defects here:

- **Serial fetching.** The dominant cost, and unnecessary — `ui_registration.py:1336` already
  establishes the parallel pattern in this codebase (`asyncio.gather(*[CACHE.get_player(tag) ...],
  return_exceptions=True)`).
- **An 8s timeout that is too tight under load.** The API demonstrably takes >4s per call while the
  poll cycle runs, and the timeout is per-account rather than for the operation as a whole.
- **ERROR-level logging with a full traceback for a routine timeout** (`QBdiscordcmds.py:4808`).
  This is the exact log-flooding pattern Pitfall 24 was written about — a user with many accounts
  during a busy cycle can emit a stack trace per account.

The user impact is a slow command plus one `❌ Could not fetch player data` row; the interaction
itself survives because the caller defers.

### Fix

**2a. Parallelise the fetch, bounded.**

Hoist the API calls out of the formatting loop. Fetch all tags up front with
`asyncio.gather(..., return_exceptions=True)` behind an `asyncio.Semaphore` (10 is consistent with
`QBdiscocmdshelper_admin_command.py:1281` and `QapBot.py:633`; unbounded like
`ui_registration.py:1336` is defensible but 82 simultaneous calls would spike a rate limiter shared
with the poll cycle). Then run the existing formatting loop over the zipped results, unchanged.

Keep the loop body's structure and comments intact (Comment Preservation) — this is a restructure of
*where* the await happens, not a rewrite of the embed building.

**2b. Replace the per-account timeout with one operation-wide budget.**

Wrap the whole `gather` in a single `asyncio.wait_for` (~30s is ample once calls run in parallel)
rather than 8s per account. Accounts that fail still need to render their `❌` row, so on overall
timeout fall back to rendering every not-yet-resolved tag from the cached
`CACHE.user_accounts[...]` data the poll cycle already keeps fresh (`player_name`, `th_level`,
`current_clan_tag`) — a degraded row beats an error row, and that data is already in memory.

**2c. Downgrade the log line.**

`QBdiscordcmds.py:4808` → `logging.warning(...)` without `exc_info=True` for `TimeoutError` /
`CancelledError`; keep ERROR + traceback for genuinely unexpected exceptions. Match the
`[MY-ACCOUNTS-REFRESH]` precedent at `ui_registration.py:1343`.

**2d. Out of scope, worth noting.**

A short-TTL player cache in `cache_manager.get_player()` would help `/whois` and the other nine call
sites at once, but it is a broader change with its own invalidation questions (registration flows
depend on `get_player` being live). Not part of this fix; add to `backlog.txt` instead.

### Tests

- `_whois_logic` with N accounts issues N calls **concurrently** (assert via a mock that records
  overlap, or that total elapsed ≪ N × per-call delay) and renders all N rows.
- A single failing/timing-out tag still produces its fallback row and does not abort the others.
- The overall-timeout path renders cached-data rows rather than raising.
- Assert no ERROR-level record is emitted for a plain timeout (`caplog`).

### Docs

Note in `RATE_LIMITING_IMPLEMENTATION.md` that `CACHE.get_player()` is uncached and that any
per-account loop over it must be bounded-parallel, never serial — with `/whois` as the worked
example of what 82 serial calls costs.

---

## Sequencing

Bug 1 and Bug 2 are independent; either can go first. Suggested order:

1. **1a** (`web_bridge.py`) — smallest change, fixes the actual user-visible report, low risk.
2. **1b** (`QBdiscocmdshelper_cwl.py`) — the DM mis-routing. Highest real severity of anything here:
   it sends one member's CWL prompt to a different member.
3. **1c** (`ui_cwl_roster.py`) — account-protection surface, needs the most care.
4. **2a–2c** (`QBdiscordcmds.py`) — self-contained.

Each its own commit + `changelog.txt` entry at the top of the file + tests, per the changelog
convention.

## Verification

- `.\run_tests.ps1` after each step — must stay green, report the real pass count in the changelog.
- Re-run the read-only DB checks used to build this plan against `data/qapbot.db`:
  - event 18 should have 17 `NULL`-but-linked and 8 mismatched signup rows **before** the fix (the
    DB is unchanged by 1a–1c; the assertion is that the *payload* now reports the live owner).
  - `#2RPLRVUG9` (B.A.B.A) resolves to `543518173421371392` in the payload.
  - `#29JQV2YCL` (OneEyeGhoul) resolves to `543518173421371392`, **not** `605029020727377930`, in
    `resolve_cwl_pool_dm_targets_sync`.
- For Bug 2, run `/whois` against `.zuurn` (82 accounts) on DEV and confirm it completes in a few
  seconds with no ERROR-level log records.

## Tracker items

Neither bug is filed — the highest existing item is #14. File both (the grey-tile/stale-snapshot bug
and the `/whois` timeout) as their own tracker items, and post a manual test case for each before
marking them implemented (Cardinal Rule 15). Once fully implemented and committed,
`git mv plans/cwl-board-stale-link-and-whois-timeout.md plans/implemented/`.
