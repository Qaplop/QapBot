"""#0099 and #0101: phases that had counts but no durations.

Both tickets say the same thing in different words — "measure before assuming it's slow".
Neither phase could be measured at all, because neither logged a duration. These pin the
fields the eventual analysis will parse, so a rename or a dropped field fails loudly here
instead of silently producing an unanalysable log.
"""
# pyright: reportPrivateUsage=false, reportMissingParameterType=false, reportUnknownMemberType=false
from __future__ import annotations

import inspect


class TestRoleSyncTiming:
    """#0099 — [ROLE-SYNC] logged '15 synced, 0 errors' with no duration."""

    def _src(self) -> str:
        import qapbot.guild_role_manager as grm
        return inspect.getsource(grm.sync_all_roles_for_guild)

    def test_completion_line_reports_elapsed(self):
        src = self._src()
        assert "elapsed=" in src, "[ROLE-SYNC] completion line lost its duration"
        assert "role sync complete" in src

    def test_timer_starts_after_the_disabled_guild_early_return(self):
        """A guild with the role system switched off returns before doing any work. If the
        timer started above that return, those guilds would contribute 0.000s samples and
        drag the measured mean toward zero — the opposite of the ticket's question."""
        src = self._src()
        early_return = src.index("role_system_enabled:\n        return")
        timer = src.index("_rs_t0 = time.monotonic()")
        assert timer > early_return, (
            "the role-sync timer moved above the disabled-guild early return; disabled "
            "guilds would now be counted as instant syncs"
        )


class TestLeaderboardPostingTiming:
    """#0101 — the posting phase had no timing, so its suspected cost (Discord API
    delete-then-repost latency) could only be guessed at."""

    def _src(self) -> str:
        import QBhelperfunctions
        return inspect.getsource(QBhelperfunctions.post_leaderboard_to_discord)

    def test_logs_elapsed_with_size_context(self):
        src = self._src()
        assert "[LB-TIMING]" in src
        assert "elapsed=" in src
        # Without these a slow post can't be told apart from a merely large one.
        assert "chars=" in src, "no payload size logged — can't separate slow from large"
        assert "msgs=" in src, "no message count logged — splitting drives the API call count"

    def test_times_the_discord_call_not_the_whole_function(self):
        """The ticket suspects Discord API latency specifically. Timing the whole function
        would fold in local hash/format work and blunt the signal."""
        src = self._src()
        timer = src.index("_lb_t0 = time.monotonic()")
        post = src.index("_split_and_post_leaderboard_helper")
        hashing = src.index("calculate_content_hash")
        assert hashing < timer < post, (
            "the leaderboard timer no longer brackets just the Discord post — it should "
            "start after the local hashing work and before the API call"
        )
