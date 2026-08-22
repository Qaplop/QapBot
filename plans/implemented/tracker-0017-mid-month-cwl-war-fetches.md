# Tracker #0017 — Odd CWL war fetching mid month

**Status: investigation complete, fix decision deferred to the project owner.**

Reported 2026-08-22 with a PROD log screenshot showing a dozen
`[COC-API-SLOW] get_league_war(#…)` lines at 12:12–12:14, immediately before
`[PHASE-1] All API fetches completed`. Asked: *why* do these run mid-month, and are they
redundant?

## Answer to the question as asked

**Yes, those specific fetches were redundant — but the mechanism producing them is not.**

Every war tag in the screenshot is a **finished** war from the regular **2026-08** CWL, rounds
1–5, spread over exactly two league groups (`0adb471904b74684`, `42cb90d823005922`). The
2026-08 CWL's last real war ended `2026-08-11T06:06` (`war_summary`), so at 12:12 on 2026-08-22
the bot was re-downloading wars that had been over for eleven days.

They came from `_find_active_cwl_war_for_clan()` (`QBhelperfunctions.py`, log label
`CWL-NOTINWAR-FALLBACK`), reached from `fetch_clan_war_data()` inside Phase 1 — which is why they
appear interleaved with the Phase-1 fetch block rather than as a separate job.

## Why the fallback runs at all (it is NOT dead code)

`fetch_clan_war_data()` calls it whenever `/currentwar` returns `notInWar`, for two real reasons:

1. A **public-warlog clan genuinely reports `notInWar` during CWL** — only
   `/clanwarleagues/wars/{tag}` returns the real state. Without the fallback the bot would miss
   CWL entirely for those clans.
2. CoC runs a genuine **mid-month bonus CWL**. `normalize_cwl_season()` (`qapbot/constants.py`)
   explicitly handles day-of-month 7–24 as a bonus event, snapped to that week's ISO Monday.
   2026-08-22 falls squarely inside that window, so "it's mid-month, therefore skip" would be an
   actively wrong guard.

So the fallback firing on 2026-08-22 is legitimate. What is wrong is what it *does* once it fires
for a season that is already over.

## The actual defect

For a finished season the walk can never succeed: it iterates the league group's rounds
newest-first and calls `get_league_war()` on every tag until it finds one in
`preparation`/`inWar`. Nothing matches, so it walks the entire group's tag list (up to 7 rounds ×
4 wars in an 8-clan group) and returns `None`. **That negative result is never recorded**, so the
identical walk repeats as soon as the caches lapse (`get_league_group` max_age 7200 s subscribed /
300 s otherwise; `get_league_war` max_age 600 s).

The guard that is supposed to prevent exactly this is
`is_latest_cwl_season_ended_sync(clan_tag)` → `cwl_league_groups.cwl_ended`.

**That flag is almost never set.** `cwl_ended=1` has exactly one writer —
`update_cwl_group_stats()` → `update_cwl_group_stats_batch(..., set_cwl_ended=all_ended)` — and it
only runs on demand:

- `/cwlinfo` rendering for that clan (`QBdiscordcmds.py`), and
- the subscription loop, only for clans holding a `cwlgroup` PNG subscription (`QapBot.py`).

There is **no periodic sweep**; `nightly_db_maintenance()` has no CWL step at all. So the flag is
set only for the handful of groups somebody actually looks at.

### Measured on the 2026-08-22 DB snapshot

| | 2026-07 | 2026-08 |
|---|---|---|
| distinct league groups | 26,478 | 25,049 |
| rows with `cwl_ended=1` | 40 | 8 |
| clans whose latest season row is still `cwl_ended=0` | 52,869 | 200,292 |

Of those, **136,707 are `track_war_updates=1`** — genuinely in the poll rotation. The eight
`cwl_ended=1` rows for 2026-08 are one single group: the one belonging to the subscribed clan that
has a `cwlgroup` subscription, which proves the write path works and simply never runs for anyone
else.

The previous month is in the same state, so this never self-corrects. A clan's stale
`cwl_ended=0` only stops mattering when the *next* season's rows push it out of
`ORDER BY cwl_season DESC LIMIT 1`.

### Why the observed volume is small but the cost is not

Only ~12 calls appear in the 90-second window, because the TTLs above throttle the walk. But look
at the durations: **19 s, 25 s, 27 s, 31 s, 31 s, 32 s, 54 s, 65 s**. These are
`[COC-API-SLOW]`-flagged — they are sitting in the rate limiter behind everything else the cycle
wants to do. The 22 h-cadence clans each contribute one group-walk per day, and there are 136,707
of them in the un-flagged state.

## Resolution — option A, shipped 2026-08-22

The project owner chose **option A** (make the guard actually work), with two directions: run the
sweep inside the update cycles rather than nightly maintenance, and apply the time-based condition
alongside the completeness test.

### What shipped

`sweep_cwl_ended_flags()` (`QBhelperfunctions.py`), called once per cycle from `main()` —
deliberately **before** the Discord-health early-return, since it is pure DB work that must keep
running during an outage.

Marks a group on **either** condition:
1. `cwl_group_all_rounds_ended()` — extracted from `update_cwl_group_stats()` so the sweep and the
   on-demand path cannot disagree about what "finished" means (Cardinal Rule 4).
2. `cwl_season_window_closed()` (`qapbot/constants.py`) — `now >= season_start + 14 days`, handling
   both `YYYY-MM` and `YYYY-MM-DD` season keys. An unparseable key is never treated as closed.

**Why condition 2 was necessary** (found while sizing the batches): sampling 1,500 groups per
season showed only ~55% can EVER satisfy condition 1 — the other ~45% contain clans, typically 6-8
of 8, with no recorded wars at all, because they are group-mates harvested from a subscribed clan's
group whose own wars nobody fetches. Condition 1 alone would have fixed a bit over half the
problem, and left the redundant walks running for exactly the clans causing them.

14 days is grounded in `war_summary`: regular CWL ran day 2 → 10-11 across 2026-05..08; the one
bonus season (`2026-06-15`) ran key+2 → key+11. The only later rows in ~1M CWL war rows were 5
stragglers tagged `2026-07` landing 2026-08-04..06.

### The standings-freeze risk was removed structurally, not mitigated

This was the reason the fix was not shipped unilaterally. The sweep writes the flag **only**, via
the new `mark_cwl_groups_ended_sync()` — never `group_rank`/`total_stars`/`total_destruction`.
`update_cwl_group_stats()`'s freeze short-circuit requires `cwl_ended` **AND** non-NULL stored
stats, so a group the sweep marks without stats still recomputes its standings live on the next
render. `test_sweep_never_writes_standings` pins this down.

### Batching

5 sequential batches of 500 per cycle (project owner's spec), each its own `asyncio.to_thread()`
hop with an await between — not concurrent, which would put several sqlite writers on one file for
work that is index-seek bound anyway.

Measured on the real PROD-sized DB (dev hardware):

| | 200 groups | 500 groups |
|---|---|---|
| war-stats, 2-query form (before) | 0.25 s | 0.52 s |
| war-stats, merged 1-query (after) | 0.16 s | 0.41 s |

`get_cwl_group_war_stats()` was collapsed to a single query — the two it ran differed only in the
aggregate over an identical WHERE/GROUP BY — with `get_cwl_group_war_stats_sync()` as its
deliberately-identical sync twin. The per-group aggregate is also **skipped entirely** when
condition 2 already decides the outcome, so burning down the historical backlog costs ~0.04 s/cycle.

### Keyset cursor, and why it must rotate

`find_unended_cwl_groups_page_sync()` pages by `(league_group_id, cwl_season)` to match
`idx_cwl_league_groups_id`'s own column order. The natural-looking `(cwl_season, league_group_id)`
ordering degrades to a full SCAN + TEMP B-TREE — 1.28 s vs 0.036 s for five 500-group pages, 35x.
Keyset also keeps a deep cursor as cheap as a shallow one, which OFFSET would not.

The cursor rotates and wraps on a short page. Required, not cosmetic: with ~45% of groups unable to
satisfy condition 1, a "first N unended groups" query would re-check that residue every cycle and
never reach the rest.

### Verification

Dry-run against a writable throwaway slice of the real PROD-copy data (400 groups / 3,200 clan
rows / 28k war rows): all 400 marked in 0.05 s across 5 batches, rotation wrapped correctly, and
zero standings written. 26 new tests in `tests/integration/test_cwl_ended_sweep.py`; full suite
2522 passed, 4 deselected.

Also verified in passing that `main` vs `history` `war_summary` have the same column **set** with
a different physical order (diverging at position 14, `result` vs `war_tag`) — the harmless
Cardinal Rule 1 case, harmless precisely because these queries name columns explicitly.

### Follow-up — done 2026-08-23

`get_cwl_player_season_status_dm_refs_for_event_sync()` (`db_manager.py`) read its rows with
positional indices (`row[0]`, `row[1]`, ...), against Cardinal Rule 14. Fixed on the project
owner's instruction, along with the identical `{r[0]: r[1]}` in `get_guild_config()`'s
`guild_clan_roles` load, found in the same audit.

Worth recording what the audit actually established, since the intuitive answer was wrong: with an
**explicit** SELECT list, `row[N]` indexes the projection, not physical storage, so an
`ALTER TABLE` reorder cannot break it — confirmed empirically by reverting the fix and watching a
reorder test still pass. Rule 14's headline incident was a `SELECT *` case. The real exposure here
is *edit* time: adding or reordering one column in that SELECT shifts every index onto the wrong
same-typed id, silently, and the only consumer feeds those values straight to
`bot.fetch_user()` / `channel.fetch_message()` to delete a Discord message.

Because the code is correct until the edit, no behavioural test can catch it in advance — so the
guard that earns its place is structural: `test_no_positional_row_access_remains_in_the_reader`,
which is the one test that fails against the old code. `tests/unit/
test_cwl_dm_refs_column_order_immunity.py` (5 tests). Rule 14 in
`.github/copilot-instructions.md` gained a note covering this distinction.
