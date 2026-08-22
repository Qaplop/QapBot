# Implementation plan: tracker #0016 — dead CWL sign-up DM buttons

## Context

Tracker item #0016, filed by `.zuurn` 2026-08-22 10:57 local: *"When I tried to click the opt-out
buttons for the CWL sign-up, this error message appeared. It worked for some accounts, but for
most of them, it kept happening."*

Part of #0016 was the ownership-guard bug already fixed in `ddc602e` / `7e8ef2f` (the guard
compared the clicker against a stale `cwl_signups` snapshot). **This is a second, independent
defect with the same symptom**, still live after those fixes — `.zuurn` and Lucas
(`1080877065231933533`) both still have non-working buttons.

## Root cause (verified in code and live data)

`CwlSignupResponseButton.callback` (`ui_cwl_roster.py`) resolves the click by
`(event_id, player_tag)` from the button's `custom_id`, then:

```python
signup = await asyncio.to_thread(db.get_cwl_signup_sync, self.event_id, self.player_tag)
if event is None or signup is None:
    ... t('cwl.template.no_longer_valid')   # "⚠️ This sign-up is no longer valid
    return                                   #    (the season may have been deleted)."
```

So a DM whose `(event_id, player_tag)` has **no `cwl_signups` row** is a dead button — and the
error text misleadingly blames a deleted season.

There is an asymmetry between the two paths that send those DMs:

| path | creates `cwl_signups` rows? | sends DMs? |
|---|---|---|
| `start_cwl_enrollment` (`QBdiscocmdshelper_cwl.py` 2345→2362, then 2469) | **yes** — `bulk_create_cwl_signups_sync` | yes |
| `notify_new_cwl_pool_members` ("Notify New Pool Members" button, `web_bridge.py:716`) | **no** | yes |

Both share `resolve_cwl_pool_dm_targets_sync` and `_send_cwl_enrollment_dm_batch`, which is why
this went unnoticed — the *pool* logic is shared, the *row seeding* is not.

The notify button's entire purpose is to reach pool members added **after** Start Enrollment ran
(its dedup is "only the not-yet-contacted ones"). Those are by definition the players with no
signup row. **Every DM that button sends to a genuinely-new pool member produces a dead button** —
systematic, not an edge case.

### Live evidence

Event 18 (`CoC | Stay`, season 2026-09, status `signup_open`) has 129 signup rows, all created
`2026-08-18 07:57` (Start Enrollment) plus 3 `auto_seeded` on 08-19. The two later DM batches
(`2026-08-21T17:42–17:44Z` and `2026-08-22T10:40Z`) created **none**.

Result: **27 sent DMs with dead buttons**, across 4 Discord users —
`543518173421371392` (.zuurn) 21, `1080877065231933533` (Lucas) 3, `353561938808274955` 2,
`592068674873983016` 1.

Corroborating detail: all 27 are still `status='pending'` globally. Nobody responded — because
nobody *could*.

All 27 still hold `dm_sent_via_message_id` / `dm_sent_via_channel_id`, i.e. the DM is still sitting
in the recipient's inbox with a live-looking button.

## Fix

### Part 1 — forward fix: never send a DM without a row for its button to land on

Put the invariant with the **sending**, not with one caller: `_send_cwl_enrollment_dm_batch`
(`QBdiscocmdshelper_cwl.py`) ensures a `cwl_signups` row exists for every player it is about to
DM. `start_cwl_enrollment` already creates them, so it becomes a no-op there
(`bulk_create_cwl_signups_sync` is `ON CONFLICT(event_id, player_tag) DO NOTHING`); the notify
path gets its missing rows.

Create rows only for targets that actually pass the batch's own guards (`skipped_already_dm_globally`,
the `cwl_dm_restrict_to_admin` guard) — a player we deliberately skip must not gain a board entry
as a side effect. One bulk existence check plus one bulk insert, not a per-player round trip.

Seed `status` via `_seed_status_from_global_sync` (2026-08-22) so a player who already answered
another guild's DM doesn't get a contradicting `pending`, and set `dmed_discord_id` from the
target's live-resolved recipient. `source='template_confirm'`, matching what Start Enrollment
already writes for its own seeded rows.

### Part 2 — repair the 27 DMs already sitting in inboxes

The forward fix alone does **not** help them: the batch's global `dm_sent` dedup skips anyone
already DMed, so they would never be revisited.

Because the button resolves `(event_id, player_tag)` **at click time**, creating the missing row
repairs the existing DM in place — no re-send, no new message, the button in the user's inbox
simply starts working.

New idempotent startup step (Cardinal Rule 12, same pattern as
`_cleanup_stray_unassigned_duplicates` from tracker #0013):
`repair_cwl_signups_for_sent_dms_sync()` — for every `cwl_player_season_status` row with
`dm_sent=1` whose event still exists **and is still `signup_open`**, insert the missing
`cwl_signups` row. Restricted to `signup_open` deliberately: that is the only status where the
button is actionable, so it is exactly the "broken link" population — and it avoids retroactively
adding players to a finalized event's historical board.

Log the repaired count; a no-op on every subsequent start once Part 1 is in.

## Tests

- A DM target with no signup row gets one created before the DM is sent, and the created row
  carries the live recipient + the global status (not a hardcoded `pending`).
- A target skipped by the dedup / DM guard does **not** get a row created.
- `start_cwl_enrollment` is unaffected — no duplicate rows, existing rows not clobbered.
- Repair step: creates exactly the missing rows for `signup_open` events, leaves finalized /
  cancelled events alone, is a no-op on a second run, and never overwrites an existing row.
- End-to-end: a signup row created by the repair makes `CwlSignupResponseButton.callback` succeed
  where it previously returned `no_longer_valid` — the actual user-visible symptom.

## Verification

- `.\run_tests.ps1` green, real pass count in the changelog.
- Re-run the live query against the copied PROD DB after deploy; it must return 0:
  ```sql
  SELECT COUNT(*) FROM cwl_player_season_status g
  JOIN cwl_events e ON e.id = g.dm_sent_via_event_id AND e.status = 'signup_open'
  LEFT JOIN cwl_signups s ON s.event_id = g.dm_sent_via_event_id AND s.player_tag = g.player_tag
  WHERE g.dm_sent = 1 AND s.player_tag IS NULL;
  ```
- Ask `.zuurn` / Lucas to click a previously-dead button and confirm it now records a response.

## Raised question — ANSWERED: intentional, no change

All 27 affected players sit in clans with `participating=0` for event 18, so they were DMed even
though their own clan isn't checked in this season. Flagged as a possible defect; **the project
owner confirmed 2026-08-22 that this is the intended design.** The DM asks whether the *player*
wants to play, not whether their clan is checked in, because a lead must be able to pull any
family member into any participating clan's roster.

Documented so it doesn't get re-reported: `CWL_ENROLLMENT_PLAYER_POOL_REDESIGN_PLAN.md` rule b
("Confirmed intentional" subsection) and `resolve_cwl_pool_dm_targets_sync`'s docstring.

## Follow-up filed separately: ownership change should re-route an unanswered DM

`#LLV0Y9PQ` (JERRY) was DMed to `.zuurn` and has since been unlinked. Per the owner's decision
the repair keeps that button working for `.zuurn` (he did receive the DM, and the ownership guard
deliberately accepts the recorded recipient). The better long-term behaviour — when an account
changes owner, delete an **unanswered** DM and re-send it to the new owner, while leaving any
already confirmed/declined DM untouched — is filed as its own tracker item rather than folded in
here.

## Tracker

#0016. Post a manual test case before moving it to `implemented` (Cardinal Rule 15). Note in the
item that it covered **two** distinct defects: the ownership guard (fixed `ddc602e`/`7e8ef2f`) and
this one.
