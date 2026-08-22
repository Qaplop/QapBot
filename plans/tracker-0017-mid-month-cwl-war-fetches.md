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

## Fix options — needs the project owner's call

### A. Make the existing guard actually work (recommended)

Add a periodic sweep that computes season completion for groups still at `cwl_ended=0` and marks
the finished ones — i.e. run the `all_ended` computation that `update_cwl_group_stats()` already
performs, on a schedule, instead of only when someone happens to look.

- **Pro:** fixes the root cause, and fixes it for the two *other* consumers of the same flag too
  (see "Blast radius" below).
- **Con / why I did not just do it:** `cwl_ended=1` **freezes** a group's standings and
  `league_rank` (`update_cwl_group_stats()`'s short-circuit). Marking a group ended too early
  permanently freezes a season mid-play at the wrong numbers. The sweep therefore needs a
  deliberate "when is it safe to declare a season over" policy — and that is a judgment call about
  correctness of historical data, not a mechanical refactor.
- Open sub-questions for the owner: where does it run (nightly maintenance? its own cadence?);
  how many groups per run (25k groups × the per-group `war_summary` query is not free); and does
  the completion test stay `all clans have ≥ n−1 ended wars`, or additionally require the season
  key to be older than some cutoff as a belt-and-braces guard against freezing early?

### B. Negative-result caching in `_find_active_cwl_war_for_clan()`

Remember "walked this clan's group, found nothing active" for some TTL and skip the walk.

- **Pro:** local, low blast radius, no schema or semantic change.
- **Con:** to actually help it needs a TTL considerably longer than the existing 2 h league-group
  TTL, which directly trades against how fast a genuinely-new CWL (including a bonus CWL) is
  detected. That trade-off is the owner's to set. It also leaves the root cause in place.

### C. Derive "season over" from `war_summary` instead of the flag

Pre-guard the walk by checking whether the clan's latest CWL season already has its full round set
recorded as ended.

- **Pro:** no flag semantics touched at all, so no risk of freezing standings.
- **Con:** duplicates the completion computation that already lives in `update_cwl_group_stats()`
  — a near-duplicate helper is exactly what Cardinal Rule 4 exists to prevent. Adds a DB query to
  the Phase-1 hot path for every notInWar clan.

**My recommendation is A**, with the completion test additionally requiring the season key to be
at least a few days old before the flag is written — that keeps the freeze-too-early risk near
zero while removing the whole class of redundant walk. But A changes when historical standings
freeze, so it should not ship without an explicit decision.

## What was already done

Findings written into `qapbot/docs/CLAN_AND_WAR_CYCLE_ARCHITECTURE.md` (new section under the
Phase-1 pipeline, before "Error Handling") so the next person reading a log full of mid-month
`get_league_war()` lines does not have to re-derive any of this. No behavioural change shipped.
