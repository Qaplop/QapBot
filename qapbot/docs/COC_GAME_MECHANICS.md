# Clash of Clans — Fundamental Game Mechanics

Reference for understanding bot behavior and log analysis.
Do NOT use markdown links inside this file (plain text paths only per project style).

---

## Clan War (Regular / Random)

- Two clans are matched by the CoC backend based on strength.
- **States**: `notInWar` → `preparation` (23h) → `inWar` (24h) → `warEnded`
- When a war ends, the clan returns to `notInWar` immediately.
- There is no mandatory rest period — a new war can be searched for instantly.
- `warEnded` state is visible only briefly (~a few minutes); the bot must catch
  it within one 5-min cycle or detect it retroactively from stored war data.
- Regular wars do NOT have a `war_tag`. Once the war transitions out of `warEnded`
  there is no way to re-fetch it from the API. The bot must finalise it from the
  temp file while the data is still on disk.
- **Data completeness risk**: If the bot misses the `warEnded` window (bot
  downtime, private warlog, or a new war starting before the next cycle), the war
  may be finalised from a temp file that was last written in `inWar` state. In
  that case missed attacks (attacks not used by war end) are NOT recorded, because
  the attack count can only be determined from the final `warEnded` snapshot.
- War data is only fetchable via the public API if the clan's warlog is public
  (`warlog_is_public=True`). Private-warlog clans can only be tracked during CWL
  (where the group API endpoint exposes data regardless of warlog setting).
- **Friendly wars**: Same `preparation`/`inWar` states but `war_type="friendly"`.
  They are never saved to war history. The bot filters them via `skip_friendly`.

---

## Clan War League (CWL)

### Season timing

- CWL season runs once per month, roughly the first 10 days.
- The season opens at the start of the month. Clans have **approximately 48 hours**
  to register ("sign up") for participation.
- Registration is not synchronised across clans. Each clan registers independently
  during the 48h window.
- The CoC backend matches groups of **exactly 8 clans** of equal CWL league tier
  as soon as 8 clans of the same tier have registered together.
- A group starts its own timeline **as soon as it is filled**, not at a fixed
  global clock. This means group start times are spread across the first 48h window
  and groups are NOT synchronised with each other.

### Group structure and round schedule

- Each group has **8 clans** and plays **7 rounds** (round-robin: every clan
  fights every other clan exactly once).
- Rounds run consecutively with no pause between them:
  - Round N: 24h preparation phase, 24h war day.
  - When round N war day ends, round N+1 preparation has already been running in
    parallel for 24h, so round N+1 war day begins immediately.
- Total group duration from round 1 prep start to round 7 war end: nominally 8 days
  (1 day prep + 7 × 1 day war), but can be longer — see Maintenance Downtime below.
- Within a season, active clans are NOT all in the same round at the same time.
  Groups that registered earlier are ahead.

### Participation curve over the month

- **Hours 0–48**: Rising number of active groups as registration fills them.
  New files and API activity ramp up continuously.
- **Hours 48 – ~(season_end – 48)**: Constant plateau of active clans.
  All registered groups are running simultaneously. This is the peak load period.
- **Last ~48 hours of season**: Declining number of active groups as later-
  registered groups (which started later) finish their 7 rounds. Activity tapers off.

### States and API endpoints

- During CWL, each clan's "current war" endpoint returns the **current round's war**.
  Between rounds there is no gap — the next round starts immediately.
- CWL war data is accessible via two endpoints:
  1. `get_current_war(clan_tag)` — returns the clan's active CWL war (same as
     regular war endpoint, but returns a CWL war during the season).
  2. `get_league_war(war_tag)` — fetches a specific round by `war_tag`. Required
     for orphaned-war recovery when the clan has already flipped to the next round.
     **Important**: CWL `war_tag`s are assigned during the season but the endpoint
     only becomes accessible **a few days after the CWL season ends**. During the
     active season the bot must catch each round's data from `get_current_war()`
     before the round flips, or recover it via the in-memory/temp snapshot. Regular
     (random) wars have NO `war_tag` at all.
- **CWL wars are always finalised with `warEnded` status.** Because the bot can
  re-fetch any round via `get_league_war(war_tag)` (post-season) or via the
  orphan-recovery path that calls `get_league_war()` once the round has ended,
  the finalised record always reflects the complete end-of-war state. Missed
  attacks are therefore always correctly recorded for CWL wars.
- A clan's war transitions from round N to round N+1 instantly at the round
  boundary. There is **no `warEnded` pause** between CWL rounds — `warEnded` is
  visible for only a brief moment (seconds to low minutes) before the new round
  begins.
- The bot tracks this via orphan detection: when the file's opponent tag no longer
  matches the current war's opponent, the old file is an orphan and must be
  finalised via `get_league_war(old_war_tag)`.

### File count oscillation during CWL

- Passive clans (track_war_updates=True, no active subscriptions) get their temp
  war file updated once per cycle. At round boundaries, the old file is archived
  and a new file is created for the new opponent.
- Because groups are NOT synchronised, round transitions are staggered across the
  48h registration window. This produces a sustained but distributed archival rate
  rather than a single spike.
- The exception: if a large batch of groups registered around the same time (e.g.
  a surge of signups on CWL day 1 morning), those groups all flip rounds simultaneously,
  causing a temporary spike in `passive_processed` (400–900/cycle) and a matching
  dip in the total temp file count as old files are archived faster than new ones
  are created (the new opponent files are written on the NEXT cycle after the smart
  timestamp fires).
- This is expected behavior — **not a bug**. The temp file count will recover
  within 1–2 cycles after the round transition wave passes.

### Server maintenance and CWL timing

- When Supercell takes CoC servers down for maintenance, any war that is currently
  active is **paused for exactly the duration of the downtime**. Attack windows
  cannot be used during maintenance.
- After maintenance ends, the war resumes and its end time is pushed forward by the
  downtime duration so each clan still has a full 24h war day.
- This applies per-round: if maintenance hits during round 3, only round 3 is
  prolonged. Earlier completed rounds are unaffected.
- The **total CWL season duration is extended by the sum of all maintenance
  downtimes** that occurred during active rounds. A season nominally ending on
  day 10 may therefore run until day 11 or later.
- Impact on the bot: `end_time` values in fetched war data reflect the adjusted
  (prolonged) end time, so smart-timestamp scheduling remains correct. However
  the bot may see clans stay `inWar` longer than the expected 24h window, which
  is normal and not an anomaly.

### `_league_war_cache` (in-memory)

- Stores fetched league war objects keyed by war_tag to avoid redundant API calls
  within a single cycle when multiple orphaned clans share the same war.
- Bounded: entries expire via a TTL (`_LEAGUE_WAR_CACHE_TTL`) and a hard size cap
  (`_LEAGUE_WAR_CACHE_MAX_ENTRIES`, oldest evicted first), enforced once per cycle in
  `qapbot/cache_manager.py`. The companion `_league_group_cache` is bounded the same way
  (`_LEAGUE_GROUP_CACHE_TTL` / `_LEAGUE_GROUP_CACHE_MAX_ENTRIES`).

---

## Inactive Clan Polling (22h cycle)

- Clans with `track_war_updates=True` but no active Discord subscriptions are
  polled approximately every 22h, not every 5-min cycle.
- The smart backdate mechanism intentionally sets the next-poll timestamp to
  **8 minutes before the war ends** so the bot catches the `warEnded` state.
- This means a clan's `last_checked_via_api` can appear 31–35h old in logs
  (22h poll gap + smart backdate extension + any cycle jitter). This is expected.
- **Design intent**: the 8-minute pre-end window is specifically chosen to
  maximise data completeness for inactive clans. Because regular wars have no
  `war_tag` and can only be finalised from the temp file, the smart backdate
  ensures the bot polls as late as possible within the `warEnded` window —
  capturing the most complete attack data — while still leaving a small buffer
  before the next war could start. If a clan starts a new war immediately after
  the previous one ends, this late poll is the only chance to record the final
  missed-attack count from the old war.

---

## Passive Clans (enemy-only tracking)

- Clans with `track_war_updates=False` are tracked via Phase-3B only.
- They are never polled via the CoC API in Phase 1 (active updates).
- Their temp files are created/updated only when they appear as an opponent in a
  tracked war. Phase-3B processes these from the active clan's war data.
- Appear in logs as `passive_processed` and `passive_skipped` in CYCLE-SUMMARY.

---

## War Log Visibility

- `warlog_is_public=True`: War results visible on the clan's public profile.
  Bot can fetch war data via `get_clan_war()`.
- `warlog_is_public=False` (private warlog): War results hidden. Bot cannot
  fetch regular war data. CWL data is still accessible via the league group endpoint
  regardless of warlog setting.
- Private warlog clans are counted separately in /status:
  "Private War Logs: N (still tracked for CWL)"
