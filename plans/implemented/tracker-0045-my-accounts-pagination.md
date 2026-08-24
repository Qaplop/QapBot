# Tracker #45 — "My Accounts" screen truncates past ~25 accounts

## Context

Zuurn (reporter, ~60 linked accounts) reported that the "My Accounts" screen only shows
the first ~25-30 accounts, so he can't unlink the ones beyond that. Root cause:
`AccountManagementView._build_player_select()` (`qapbot/ui_registration.py:1004`) hard-slices
`user_players[:25]` — Discord's Select option ceiling — with no pagination and no indication
more exist. The sibling picker in the link/verify chooser, `AccountActionView.__init__`
(`ui_registration.py:601`), has the identical shape of bug at `unverified_players[:24]`.

There is no DB or code cap on how many accounts a user may link (`user_players` table,
`db_manager.py:1996`, has no CHECK/limit; `_link_player_to_user()` in `QBdiscocmdshelper.py`
never checks a count), so 60+ accounts is a real, reachable scenario, not theoretical. The
project owner has asked that every account-management function support an unbounded number
of accounts by design, and that users be able to unlink everything at once.

Not in scope: `ui_notifications.py:861` (buddy-watch select) has the same `[:25]` truncation
shape, but it's a different feature (war-notification buddy list, watching *other* players),
not reachable from this hub — flagged as a known follow-up, not fixed here.

## Changes

### 1. Paginate `AccountManagementView`'s player Select
File: `qapbot/ui_registration.py` (class at line 920)
- Add `self.current_page = 0` in `__init__`.
- `_build_player_select()`: slice `user_players[page*25:(page+1)*25]` using `self.current_page`
  (clamped to valid range) instead of `[:25]`.
- At the end of `_build_player_select()`, rebuild a Prev/Page-indicator/Next button row (row 3),
  shown only when `total_pages > 1`, mirroring the existing pattern in
  `ClanManagementView._add_pagination_buttons()` (`ui_clan_management.py:538-573`). Reuse the
  existing generic i18n keys `ui_components.clan_management.button_previous` / `button_next`.
- New `_on_page_prev` / `_on_page_next` handlers: adjust `self.current_page`, clear the current
  selection (disable Verify/Set Primary/Unlink, matching how a fresh view starts), rebuild the
  select + pagination row, `edit_message`.
- `_build_message_content()`: when paginated, append a one-line note ("Showing accounts X–Y of
  N — use ◀ ▶ below to browse") via a new i18n key so the truncation is visible, not silent.

Row budget check: row 0 = select, row 1 = 3 action buttons, row 2 = Refresh + new Unlink All
button, row 3 = pagination — 4 of 5 allowed rows, no conflict.

### 2. Paginate `AccountActionView`'s unverified-account Select
File: `qapbot/ui_registration.py` (class at line 578)
- Move "Link new account" out of the Select's options into its own button (row 1), freeing the
  full 25 option slots for unverified players (existing i18n key `button_link_new` covers the
  label already).
- Add `self.current_page = 0`, slice `unverified_players` at 25/page, add the same Prev/Next
  pagination row (row 2) when `total_pages > 1`.

### 3. Add "Unlink All" bulk action
- New `unlink_all_players(user_id: str) -> int` in `QBdiscocmdshelper.py`, placed after
  `unlink_player()` (line 2398). Mirrors `unlink_player()`'s move-to-UNASSIGNED logic but
  batches every account's in-memory mutation before **one** `CACHE.persist_user(user_id)` +
  one `CACHE.persist_user("UNASSIGNED")` call, so cost stays O(1) DB writes regardless of
  account count (vs. O(n) if this just looped `unlink_player()`).
- New `UnlinkAllConfirmView` in `ui_registration.py` (near `UnlinkConfirmView`, line 1400),
  same Confirm/Cancel button shape, confirm text states the exact count and that it's
  irreversible. On confirm: call `unlink_all_players()`, run one role sync
  (`sync_roles_for_user`, matching `UnlinkConfirmView._on_confirm`'s existing post-unlink role
  sync), show the "no accounts" terminal message.
- New danger-styled "Unlink All" button in `AccountManagementView` (row 2, next to Refresh),
  wired to a new `_on_unlink_all_click` that shows `UnlinkAllConfirmView`.

### 4. i18n (en.json + de.json, `playerregistration` namespace, near existing unlink keys)
New keys: `button_unlink_all`, `unlink_all_confirm` ("...ALL {count} accounts..."),
`button_confirm_unlink_all`, `unlink_all_success`, `account_management_page_note`. Verify
translation completeness with `qapbot/scripts/check_translation_files.py` after editing.

### 5. Docs
Add a short note to `qapbot/docs/REGISTRATION_MESSAGE_WORKFLOWS.md` documenting the pagination
behavior and the deliberate no-cap-on-account-count decision, and cross-reference the
still-open `ui_notifications.py:861` truncation as a known follow-up.

## Verification
- `.\run_tests.ps1` (never a raw pytest command).
- Manual: exercise "My Accounts" and the link/verify chooser with a synthetic user who has
  30+ linked accounts (dev DB), confirm pagination buttons appear/disable correctly at the
  first/last page, confirm Unlink All removes everything and role sync fires once.
- Per Cardinal Rule 15: post at least one manual test case to tracker item #45
  (`tracker_add_testcases`) before/alongside moving it out of `open` status.
