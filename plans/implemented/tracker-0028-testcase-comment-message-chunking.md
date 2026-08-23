# Tracker #0028 — comment/testcase POST returns 500 past 2000 characters

**Status: implemented 2026-08-23. Needs a PROD bot restart to take effect — the tracker MCP
tools point at `bridge-prod.qapbot.uk`, so this repo's fix has no effect on the running bot
until it restarts with the new code.**

## How this was found

Filed after hitting the exact failure three times in one session (tickets #14, #17, #19's test
cases). The 500 was masked well enough that test cases for those three items were reported as
"posted" when in fact the DB rows existed but the Discord message never went out — caught only
when the project owner checked the actual `#qapbot-testing` channel and found it empty.

## Root cause

`post_test_cases()` (`qapbot/ui_tracker.py`) formatted the entire test-case list into ONE string
and sent it as a single `channel.send(content, view=view)` with no length guard. Two moderately
detailed cases already ran to 1221 characters; a typical 8-case set overflowed Discord's 2000-char
cap by roughly 2.4x. `channel.send()` raised `discord.HTTPException` (error 50035), which
propagated uncaught through `post_test_cases()` and `handle_post_tracker_testcases()` — surfacing
to the MCP/bridge caller as a bare `text/plain` HTTP 500 ("Attempt to decode JSON with unexpected
mimetype"). By that point `set_tracker_testcases()` (the DB write, a few lines earlier in the same
function) had already committed successfully — so the caller saw a failure while the write had
actually landed, and the Discord post had silently never happened at all.

`post_comment()` had the identical defect for a single over-length comment.

## Fix

New chunking helpers in `qapbot/ui_tracker.py`:

- `_chunk_lines_for_discord(lines, limit=2000)` — packs lines into the fewest possible messages,
  each ≤ limit, never splitting a line across two messages except as a last-resort hard-slice
  when one line alone exceeds the limit.
- `_format_testcase_lines()` — the test-case formatter, now returning lines instead of one joined
  string (`_format_testcase_message()` kept as a thin `"\n".join(...)` wrapper).
- `_post_or_refresh_testcase_message()` — shared by `post_test_cases()` and
  `_refresh_testcase_message()`: edits in place when the content fits in one message and there's
  no tracked overflow (unchanged behavior for the common case), otherwise deletes every
  previously-tracked message and reposts fresh, in order, with the interactive view on the last
  chunk only.
- `_send_testcase_chunks()` / `_delete_testcase_messages()` — the send/delete primitives the above
  is built from, also used directly by `_move_test_message_to_done_testing_channel()` (which can't
  use the shared helper since its old and new messages live in different channels).
- `post_comment()` now chunks via the same helper, splitting on `text.split("\n")` first.

New DB column `tracker_items.test_overflow_message_ids` (comma-joined) tracks any leading chunks;
`test_message_id` stays the last chunk — the one carrying buttons and the one
`get_tracker_item_by_test_message_id()` resolves for the 👍-reaction shortcut.

`web_bridge.py`'s two handlers additionally catch `discord.HTTPException` and return a JSON 502
instead of letting a genuine Discord-side failure (outage, permissions) propagate as the same
mimetype-confusion 500 — defense in depth beyond the chunking fix itself.

## Tests

14 tests in `tests/discord/test_ui_tracker_items.py` (pure chunking-function tests; small-set
edit-in-place regression guard; overflow send with view-only-on-last; growing/shrinking across the
one-message boundary; `_refresh_testcase_message()` re-chunk persistence; the Done-channel move
with overflow; `post_comment()` chunking). 2 tests in `tests/integration/test_web_bridge_tracker.py`
for the bridge-level JSON-error behavior. Full suite: 2550 passed, 4 deselected.

## Follow-up once PROD restarts

Re-run `tracker_add_testcases` for #0014, #0017, #0019 — their DB rows are already correct
(`set_tracker_testcases` replaces the full set), only the actual Discord post needs to happen.
