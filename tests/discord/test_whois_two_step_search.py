"""Tests for /whois's two-step name search (2026-08-18, PLAYER_NAME_INDEX_RETIREMENT_PLAN.md
Steps 1-3): a guild-first, always-complete in-memory pass over CACHE (_build_guild_player_name_
matches), followed by a capped global FTS5 fallback (db_manager.search_player_names_full_sync)
for everyone else. Replaces the old single uncapped scan over CACHE.player_name_index plus a
separate post-search reorder step.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import QBdiscordcmds  # noqa: E402


# ---------------------------------------------------------------------------
# _build_guild_player_name_matches — the new, directly-testable core
# ---------------------------------------------------------------------------

class TestBuildGuildPlayerNameMatches:
    def _set_guild_clans(self, monkeypatch, tags):
        monkeypatch.setattr(
            "qapbot.QBdiscocmdshelper.get_guild_clans_including_member_config",
            lambda guild_id: list(tags),
        )

    def test_no_guild_id_returns_empty(self, monkeypatch):
        """DM invocation (interaction.guild_id is None) — no guild context to search."""
        assert QBdiscordcmds._build_guild_player_name_matches(None, "alice") == []

    def test_no_configured_clans_returns_empty(self, monkeypatch):
        self._set_guild_clans(monkeypatch, [])
        assert QBdiscordcmds._build_guild_player_name_matches(123, "alice") == []

    def test_matches_via_source_1_user_accounts(self, monkeypatch):
        self._set_guild_clans(monkeypatch, ["#CLAN1"])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {
            "u1": {"players": [{"player_tag": "#P1", "player_name": "AliceWonder", "current_clan_tag": "#CLAN1"}]},
        })
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {})
        assert QBdiscordcmds._build_guild_player_name_matches(123, "alice") == [
            {"player_tag": "#P1", "player_name": "AliceWonder"}
        ]

    def test_matches_via_source_3_clan_cache(self, monkeypatch):
        """Source 3 (coc_clan_cache) is the freshest name source and needs no registration."""
        self._set_guild_clans(monkeypatch, ["#CLAN1"])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {})
        member = MagicMock()
        member.tag = "#P2"
        member.name = "BobBuilder"
        clan_obj = MagicMock()
        clan_obj.members = [member]
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {"#CLAN1": {"data": clan_obj}})
        assert QBdiscordcmds._build_guild_player_name_matches(123, "bob") == [
            {"player_tag": "#P2", "player_name": "BobBuilder"}
        ]

    def test_source_2_only_tag_with_no_name_is_silently_skipped(self, monkeypatch):
        """temp_war_stats only carries tag->clan (its keys), no name. A tag found ONLY there
        (not source 1/3) has no name to match against — must be silently skipped, not crash
        (2026-08-18, PLAYER_NAME_INDEX_RETIREMENT_PLAN.md Step 5 — this used to fall back to
        CACHE.player_name_index, which no longer exists)."""
        self._set_guild_clans(monkeypatch, ["#CLAN1"])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {"#CLAN1": {"#P3": {}}})
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {})
        assert QBdiscordcmds._build_guild_player_name_matches(123, "carol") == []

    def test_source_2_tag_also_found_in_source_1_still_matches(self, monkeypatch):
        """A tag present in BOTH temp_war_stats (clan association only) and user_accounts (name)
        must still match normally — the two sources combine via the shared tag_to_clan dict."""
        self._set_guild_clans(monkeypatch, ["#CLAN1"])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {
            "u1": {"players": [{"player_tag": "#P3", "player_name": "CarolCrafter"}]},
        })
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {"#CLAN1": {"#P3": {}}})
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {})
        assert QBdiscordcmds._build_guild_player_name_matches(123, "carol") == [
            {"player_tag": "#P3", "player_name": "CarolCrafter"}
        ]

    def test_excludes_non_guild_clan_members(self, monkeypatch):
        """A player whose current_clan_tag isn't one of the guild's own clans must not match,
        even if their name matches the needle."""
        self._set_guild_clans(monkeypatch, ["#CLAN1"])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {
            "u1": {"players": [{"player_tag": "#OUTSIDE", "player_name": "AliceOutside", "current_clan_tag": "#OTHERCLAN"}]},
        })
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {})
        assert QBdiscordcmds._build_guild_player_name_matches(123, "alice") == []

    def test_uncapped_regardless_of_scale(self, monkeypatch):
        """The whole point of the redesign: this pass has no cap of any kind, unlike the global
        FTS5 fallback's hard_cap — proves completeness at a scale well past a typical cap."""
        self._set_guild_clans(monkeypatch, ["#CLAN1"])
        members = []
        for i in range(6000):
            m = MagicMock()
            m.tag = f"#G{i:05d}"
            m.name = f"GuildPlayer{i:05d}"
            members.append(m)
        clan_obj = MagicMock()
        clan_obj.members = members
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {"#CLAN1": {"data": clan_obj}})
        result = QBdiscordcmds._build_guild_player_name_matches(123, "guildplayer")
        assert len(result) == 6000

    def test_sorted_alphabetically(self, monkeypatch):
        self._set_guild_clans(monkeypatch, ["#CLAN1"])
        members = []
        for tag, name in (("#Z1", "ZebraName"), ("#A1", "AppleName"), ("#M1", "MangoName")):
            m = MagicMock()
            m.tag = tag
            m.name = name
            members.append(m)
        clan_obj = MagicMock()
        clan_obj.members = members
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {"#CLAN1": {"data": clan_obj}})
        result = QBdiscordcmds._build_guild_player_name_matches(123, "name")
        assert [m["player_name"] for m in result] == ["AppleName", "MangoName", "ZebraName"]


# ---------------------------------------------------------------------------
# whois_slash's name-search branch — integration-level behavior
# ---------------------------------------------------------------------------

@pytest.fixture
def _no_guild_clans(monkeypatch):
    """Default: no guild clans configured, so the guild pass always contributes nothing and
    every test starts from a clean, predictable baseline unless it opts into guild matches."""
    monkeypatch.setattr(
        "qapbot.QBdiscocmdshelper.get_guild_clans_including_member_config", lambda guild_id: []
    )


class TestWhoisSlashTwoStepSearch:
    @pytest.mark.asyncio
    async def test_short_query_no_guild_match_sends_too_short(self, mock_interaction, monkeypatch, _no_guild_clans):
        mock_interaction.guild_id = 987654321
        db = MagicMock()
        db.search_player_names_full_sync = MagicMock(return_value=[])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "db_manager", db)

        await QBdiscordcmds.whois_slash.callback(mock_interaction, user=None, player="Al")  # type: ignore[arg-type]

        mock_interaction.followup.send.assert_awaited_once()
        sent_text = mock_interaction.followup.send.await_args.args[0]
        assert "Al" in sent_text
        db.search_player_names_full_sync.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_query_with_guild_match_returns_result_not_too_short(self, mock_interaction, monkeypatch):
        """A guild admin searching a 2-char query that matches their own small roster must get
        a working result, not the too-short message — the guild pass has no length floor."""
        mock_interaction.guild_id = 987654321
        monkeypatch.setattr(
            "qapbot.QBdiscocmdshelper.get_guild_clans_including_member_config",
            lambda guild_id: ["#CLAN1"],
        )
        member = MagicMock()
        member.tag = "#P1"
        member.name = "Albert"
        clan_obj = MagicMock()
        clan_obj.members = [member]
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {"#CLAN1": {"data": clan_obj}})
        db = MagicMock()
        db.search_player_names_full_sync = MagicMock(return_value=[])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "db_manager", db)
        report_mock = AsyncMock()
        monkeypatch.setattr(QBdiscordcmds, "_player_report_logic", report_mock)

        await QBdiscordcmds.whois_slash.callback(mock_interaction, user=None, player="Al")  # type: ignore[arg-type]

        db.search_player_names_full_sync.assert_not_called()
        # Single match auto-resolves via _player_report_logic — proves a result was found,
        # not the too-short message (which would skip this call entirely).
        report_mock.assert_awaited_once_with(mock_interaction, "#P1")

    @pytest.mark.asyncio
    async def test_dm_no_guild_context_short_query_sends_too_short(self, mock_interaction, monkeypatch):
        mock_interaction.guild_id = None
        db = MagicMock()
        db.search_player_names_full_sync = MagicMock(return_value=[])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "db_manager", db)

        await QBdiscordcmds.whois_slash.callback(mock_interaction, user=None, player="Al")  # type: ignore[arg-type]

        mock_interaction.followup.send.assert_awaited_once()
        db.search_player_names_full_sync.assert_not_called()

    @pytest.mark.asyncio
    async def test_three_char_query_runs_global_fallback(self, mock_interaction, monkeypatch, _no_guild_clans):
        mock_interaction.guild_id = 987654321
        db = MagicMock()
        db.search_player_names_full_sync = MagicMock(return_value=[])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "db_manager", db)

        await QBdiscordcmds.whois_slash.callback(mock_interaction, user=None, player="Ali")  # type: ignore[arg-type]

        db.search_player_names_full_sync.assert_called_once_with("ali")

    @pytest.mark.asyncio
    async def test_guild_match_never_truncated_by_global_hard_cap(self, mock_interaction, monkeypatch):
        """The actual point of the whole redesign: a guild member must appear in the results
        even when the global fallback's own cap is already exhausted by other, unrelated
        matches — proven by returning a full (simulated-capped) global list that does NOT
        include the guild member's tag, then asserting the guild member is still present."""
        mock_interaction.guild_id = 987654321
        monkeypatch.setattr(
            "qapbot.QBdiscocmdshelper.get_guild_clans_including_member_config",
            lambda guild_id: ["#CLAN1"],
        )
        member = MagicMock()
        member.tag = "#GUILDMATCH"
        member.name = "ZZZLateAlphabet"
        clan_obj = MagicMock()
        clan_obj.members = [member]
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {"#CLAN1": {"data": clan_obj}})
        # Simulate the global reader's cap already being exhausted by unrelated matches —
        # the guild member's tag is deliberately absent, as it would be in a real truncation.
        db = MagicMock()
        # 30 unrelated matches — deliberately > 25, so a flat alphabetical sort of all 31 would
        # push "ZZZLateAlphabet" (the guild member) past the [:25] slice on its own merits.
        # It must survive anyway, purely because guild_matches is concatenated FIRST.
        db.search_player_names_full_sync = MagicMock(return_value=[
            {"player_tag": f"#OTHER{i:04d}", "player_name": f"Alphabet{i:04d}"} for i in range(30)
        ])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "db_manager", db)
        captured: dict = {}

        def _capture_view(*, options, **kwargs):
            captured["options"] = options
            return MagicMock()

        monkeypatch.setattr("qapbot.ui_common.GenericSelectView", _capture_view)

        await QBdiscordcmds.whois_slash.callback(mock_interaction, user=None, player="alphabet")  # type: ignore[arg-type]

        option_tags = [opt.value for opt in captured["options"]]
        assert "#GUILDMATCH" in option_tags
        assert option_tags[0] == "#GUILDMATCH"  # survives [:25] because it's concatenated first

    @pytest.mark.asyncio
    async def test_no_duplicate_tags_between_guild_and_global(self, mock_interaction, monkeypatch):
        """A player who is both a guild member AND present in the global FTS5 result set must
        appear exactly once, from the guild pass, not duplicated by the global fallback."""
        mock_interaction.guild_id = 987654321
        monkeypatch.setattr(
            "qapbot.QBdiscocmdshelper.get_guild_clans_including_member_config",
            lambda guild_id: ["#CLAN1"],
        )
        member = MagicMock()
        member.tag = "#SHARED"
        member.name = "SharedPlayer"
        clan_obj = MagicMock()
        clan_obj.members = [member]
        monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE, "temp_war_stats", {})
        monkeypatch.setattr(QBdiscordcmds.CACHE.coc_clan_cache, "cache", {"#CLAN1": {"data": clan_obj}})
        db = MagicMock()
        db.search_player_names_full_sync = MagicMock(return_value=[
            {"player_tag": "#SHARED", "player_name": "SharedPlayer"},
            {"player_tag": "#OTHER1", "player_name": "SharedPlayerToo"},
        ])
        monkeypatch.setattr(QBdiscordcmds.CACHE, "db_manager", db)

        guild_matches = QBdiscordcmds._build_guild_player_name_matches(987654321, "sharedplayer")
        guild_tags = {m["player_tag"] for m in guild_matches}
        global_raw = db.search_player_names_full_sync("sharedplayer")
        global_matches = [m for m in global_raw if m["player_tag"] not in guild_tags]
        all_matches = guild_matches + global_matches
        all_tags = [m["player_tag"] for m in all_matches]
        assert all_tags.count("#SHARED") == 1
        assert "#OTHER1" in all_tags
