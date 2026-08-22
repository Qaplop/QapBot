# Tracker #0019 — Ownership change should re-route an unanswered CWL sign-up DM

## The behaviour the project owner specified (already settled, 2026-08-22)

- DM still **UNANSWERED** (`cwl_player_season_status.status = 'pending'`): delete the old owner's
  DM message and re-send it to the new owner.
- DM already **ANSWERED** (confirmed/declined): leave it completely untouched. The response is a
  real historical fact and must not be retracted or re-asked.

## The open design question, and how it is resolved

The ticket left one question to the implementer: *what triggers the re-route?* — the link/unlink
path, a periodic sweep, or an idempotent startup pass.

**Chosen: a periodic sweep**, run once per update cycle from `main()` (`QapBot.py`), placed after
the `is_discord_available()` guard because it does Discord DM I/O.

- The link/unlink path was **rejected**: the ticket itself flags it as touching account-protection
  code (Cardinal Rule 2), and it would put Discord DM round-trips (a delete plus a send, each
  retryable) inside the synchronous user-facing linking flow.
- A startup-only pass was **rejected as insufficient on its own**: the bot runs for days or weeks,
  and an enrollment window is only days long — a re-route that waits for the next restart would
  routinely miss the whole window. The periodic sweep subsumes it anyway (the first cycle runs
  shortly after boot), so it needs no separate startup call site.
- The sweep is idempotent by construction: once re-routed, `dmed_discord_id` matches the live
  owner, so the row stops matching the detection query. Same shape as the existing
  `_repair_cwl_signups_for_sent_dms()` precedent.

### Sub-decision made on existing precedent — cheap to overrule

**The old owner is NOT told their DM was withdrawn.** Precedent: `cleanup_stale_cwl_enrollment_dms()`
(Delete Season) already deletes enrollment DMs silently, and its docstring records that as the
project owner's stated preference ("more confusing than just removing the message outright"). A
notice would also be odd on its face — it would tell someone about an account they no longer own.

### The JERRY case is handled by construction

The ticket names a live case: `#LLV0Y9PQ` was DMed to `.zuurn` and has since been **unlinked**,
and the owner confirmed that button should keep working for `.zuurn` for now. Unlinking is not an
ownership *change* — it is ownership *removal*, with nobody to re-route to. The detection below
therefore requires a **different, real** current owner: a tag whose live `discord_id` is None or
`UNASSIGNED` is skipped entirely, leaving that DM exactly as it is.

## Detection

Candidates (`db.find_cwl_enrollment_dms_needing_reroute_sync()`), deliberately narrow:

```
cwl_player_season_status g
  JOIN cwl_events e ON e.id = g.dm_sent_via_event_id AND e.status = 'signup_open'
WHERE g.status = 'pending'      -- answered DMs are never touched (owner's spec)
  AND g.dm_sent = 1
  AND g.dmed_discord_id IS NOT NULL
```

`signup_open` scoping matches `_repair_cwl_signups_for_sent_dms()`: it is the only event status
where the button is actionable at all, so it is exactly the population where a mis-routed DM can
still do harm.

The ownership comparison is **not** done in SQL. `user_players` can hold several rows per
`player_tag` and the verified-wins/UNASSIGNED-last dedup already lives in
`get_player_links_sync()`; re-deriving it in a second query is exactly the near-duplicate Cardinal
Rule 4 forbids. The candidate set is tiny (open events only), so the sweep resolves live owners
through that existing helper and filters in Python.

## Action per detected row

1. **Retract** the old DM via the existing `cleanup_stale_cwl_enrollment_dms()` — best-effort,
   never fatal. Done **first**, so the old owner loses the ability to answer even if the re-send
   later fails.
2. **Clear** the global dm_sent record via `clear_cwl_player_dm_sent_sync()` (added for tracker
   #0014). Without it `_send_cwl_enrollment_dm_batch()`'s global dedup counts the player as
   already contacted this season and sends nothing.
3. **Re-point** `cwl_signups.dmed_discord_id` at the new owner via a new narrow setter. This is
   what actually closes the hole for a legacy row whose `dm_sent_via_message_id` is NULL (rows
   predating 2026-08-19) and whose message therefore cannot be deleted: the sign-up button's guard
   is `{signup.dmed_discord_id, live_discord_id}` (see `CwlSignupResponseButton.callback`,
   widened by #0016), so once the snapshot names the new owner the old owner's surviving DM
   correctly rejects them with `not_your_signup`.
4. **Re-send** through `_send_cwl_enrollment_dm_batch()` — never a hand-rolled send (Pitfall 38:
   the batch seeds the `cwl_signups` row the button needs). It also re-stamps
   `cwl_player_season_status.dmed_discord_id` to the new owner on success via
   `mark_cwl_player_dm_sent_sync()`.

If the re-send fails (new owner's DMs closed, transient error), the row is left `status='pending'`
with `dm_sent=0`, which is exactly the state "Notify New Pool Members" already picks up — so it
recovers on its own rather than stranding the player.

A per-cycle cap (`_MAX_DM_REROUTES_PER_CYCLE = 25`) bounds the blast radius: a mass re-link, or a
bug in the detection, can never turn into an unbounded DM burst in one cycle. The remainder is
simply picked up by the next cycle.

## Files

- `qapbot/db_manager.py` — `find_cwl_enrollment_dms_needing_reroute_sync()`,
  `set_cwl_signup_dmed_discord_id_sync()`
- `qapbot/QBdiscocmdshelper_cwl.py` — `reroute_cwl_enrollment_dms_after_ownership_change()`
- `QapBot.py` — one call in `main()`, after the Discord-health guard
- `qapbot/docs/REGISTRATION_MESSAGE_WORKFLOWS.md` — document the sweep

## Tests

1. pending + ownership changed → old DM deleted, new owner DMed, both `dmed_discord_id` columns
   re-pointed
2. confirmed → completely untouched (no delete, no send)
3. declined → completely untouched
4. unlinked (the JERRY case) → untouched, old recipient keeps their working button
5. account in the UNASSIGNED pool → treated as unlinked, untouched
6. ownership unchanged → no-op
7. event not `signup_open` → no-op
8. legacy row with no `dm_sent_via_message_id` → still re-pointed and re-sent (nothing to delete)
9. idempotence: running the sweep twice in a row re-routes exactly once
10. per-cycle cap honoured
