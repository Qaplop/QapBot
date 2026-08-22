# Tracker #0014 — Admin-settable enrollment status on the Manage Enrollment board

Feature request (project owner, 2026-08-21): guild admins must be able to change the
enrollment status of any player in the CWL player pool, via a right-click context menu on a
player tile in the Manage Enrollment board (the Discord Activity).

## Spec (verbatim requirements, condensed)

- Right-click a player tile → menu option "Set enrollment status".
- Choices: **Confirmed**, **Declined**, **Pending (sends DM again!)**.
- Setting **Pending** must delete the existing invitation DM (if any) and send a fresh
  invitation DM to the account's current owner.
- Conflict rule: **last action wins**, in both directions —
  - admin sets a status, player later clicks their DM button → the player's answer wins;
  - player answers first, admin later sets a status → the admin's value wins.

## Existing building blocks (Cardinal Rule 4 — reuse, don't duplicate)

| Need | Existing function |
|---|---|
| Global season truth + cross-guild fan-out | `propagate_cwl_player_response()` (`QBdiscocmdshelper_cwl.py`) |
| Local per-guild signup row write | `db.upsert_cwl_signup_sync()` |
| DM message deletion (best-effort) | `cleanup_stale_cwl_enrollment_dms()` (`QBdiscocmdshelper_cwl.py`) |
| Re-send an enrollment DM (seeds the signup row a button needs — Pitfall 38) | `_send_cwl_enrollment_dm_batch()` |
| Old DM's message/channel ids | `cwl_player_season_status.dm_sent_via_message_id/_channel_id` |
| Board live refresh | `bump_enrollment_version()` |
| Hub message refresh | `refresh_cwl_management_hub_message()` |

## Why "last action wins" needs no new conflict machinery

Both the player's DM button (`CwlSignupResponseButton.callback`) and the new admin action write
the **same** global row (`cwl_player_season_status`) through the **same** function
(`propagate_cwl_player_response`), which does a plain upsert with no ordering guard. Whichever
write happens later is therefore the persisted one — that IS "last action wins", already.

The only thing that could break it is the admin action *retracting the player's DM* for
confirmed/declined (the player could then no longer overwrite). So: **only the Pending action
deletes the DM**; Confirmed/Declined deliberately leave the DM in place so the player's button
still works and can still win.

`set_cwl_player_response_status_sync`'s own docstring already anticipates this exact caller
("e.g. an admin manually confirming a player on the board who was never actually DMed"), so this
is the intended path, not a new architecture.

## Backend — `POST /api/cwl/enrollment/status` (`qapbot/web_bridge.py`)

Body: `{guild_id, discord_user_id, player_tag, status}`, `status ∈ {confirmed, declined, pending}`.

Gate: `_resolve_admin_or_leader()` — same gate as `POST /api/cwl/enrollment/assign`, the board's
other write action. (The board itself is opened behind
`_check_cwl_admin_or_leader_permission`, so an admin-only gate here would let a leader open the
board but silently fail on this one action.)

Flow:
1. Resolve season/event; `409` if no event for the selected season.
2. `409` if the event is `draft` or `cancelled` — nothing has been enrolled yet / the event is
   dead. (Same guard `notify_new_cwl_pool_members()` already applies before DMing.)
3. Resolve the player's **live** owner from `user_players` (`db.get_player_links_sync`), falling
   back to the existing signup row's `dmed_discord_id` — never write the stale snapshot back
   (Pitfall 37, and the same rule `CwlSignupResponseButton.callback` follows).
4. `confirmed` / `declined`:
   - `upsert_cwl_signup_sync(..., source="admin_status", status=..., responded_at=now)` — creates
     the local row if the player had none (a pooled family member never seeded).
   - `propagate_cwl_player_response(...)` → global truth + every other guild's mirror.
5. `pending`:
   - Read `get_cwl_player_season_status_sync()` for `dm_sent_via_message_id/_channel_id`; if
     present, retract that DM via `cleanup_stale_cwl_enrollment_dms()`.
   - `upsert_cwl_signup_sync(..., status="pending", responded_at=None)` locally.
   - `propagate_cwl_player_response(..., "pending", None, ...)` → global + mirrors.
   - **New** `db.clear_cwl_player_dm_sent_sync(player_tag, season)` — resets the `dm_sent*` half
     of the row. Required: `_send_cwl_enrollment_dm_batch()`'s global dedup would otherwise skip
     the re-send outright (`skipped_already_dm_globally`). Touches only the `dm_sent*` columns,
     never `status` — the table's two-write-method split (see its `CREATE TABLE` comment) is
     preserved by adding a third method on the dm_sent side rather than widening the status one.
   - Re-send via `_send_cwl_enrollment_dm_batch(event_id, guild_id, season, [target])` — never a
     hand-rolled send (Pitfall 38: the batch seeds the `cwl_signups` row the button needs).
   - Report the outcome back so the admin sees *why* no DM went out when it didn't
     (`dm_sent` / `unlinked` / `blocked` / `failed` / `dm_guard`).
6. `bump_enrollment_version()` for this guild and every guild the propagation touched;
   `refresh_cwl_management_hub_message()` best-effort.

Response: `{"ok": true, "status": "...", "dm": {...}|null}`.

## Frontend (`activity/client/src/enrollmentBoard.ts`, `main.ts`, `types.ts`, `index.html`)

- Generalize the existing single-item context menu into a small builder taking a list of
  `{label, onClick}` entries plus an optional submenu, instead of hardcoding one button.
- The menu now opens on **every** player card, not just guests:
  - "Set enrollment status ▸" → Confirmed / Declined / Pending (re-sends DM) — always present;
  - "Remove guest player" — still only for `is_guest` cards with the callback wired.
- New optional callback prop `onSetEnrollmentStatus?: (playerTag, status) => Promise<void>`,
  following the same optional-prop convention every other board callback already uses.
- On success: update the card's `signup_status` in `working` and re-render immediately (same
  "the POST already confirmed it server-side" reasoning as the guest-removal handler), rather
  than waiting up to ~25 s for the long poll.
- `main.ts` wires it to `POST /api/cwl/enrollment/status` and surfaces the DM outcome in the
  footer status line.
- CSS: one submenu class reusing `.player-context-menu`'s look.

## Not doing (deliberate)

- No new i18n keys for the Activity — the board's own chrome is hardcoded English throughout
  (Cardinal Rule 6 covers Discord-visible text; the re-sent DM itself still goes through `t()`
  inside `send_cwl_signup_template_dm`).
- No "withdrawn" option — legacy-only status, see `types.ts`.

## Tests

`tests/discord/test_web_bridge.py`:
1. admin sets confirmed → local row + global row both confirmed, no DM sent, old DM untouched
2. admin sets declined for a player with no prior signup row → row is created
3. admin sets pending → old DM deleted, `dm_sent` cleared, a fresh DM sent
4. pending for an unlinked player → status still reset, reported as `unlinked`, no DM
5. cross-guild fan-out: a second guild pooling the same player sees the admin's status
6. player's DM button click AFTER an admin set confirmed → declined wins (last action wins)
7. non-admin/non-leader → 403; bad status value → 400; no event → 409
