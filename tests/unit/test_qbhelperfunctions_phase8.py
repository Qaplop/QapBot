from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from types import SimpleNamespace
from typing import Any, Dict, cast

import pytest

import QBhelperfunctions as hf


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str) -> Any:
        self.sent.append(content)
        return SimpleNamespace(id=len(self.sent))


def _war_data_none(_tag: str) -> None:
    return None


def _war_data_preparation(_tag: str) -> Dict[str, Any]:
    return {
        "state": "preparation",
        "clan": {"name": "A"},
        "opponent": {"name": "B"},
        "start_time": "<Timestamp time=datetime.datetime(2026, 2, 1, 10, 0, 0) seconds_until=3600>",
    }


def _war_data_in_war(_tag: str) -> Dict[str, Any]:
    return {
        "state": "in_war",
        "attacks_per_member": 2,
        "end_time": "<Timestamp time=datetime.datetime(2026, 2, 1, 10, 0, 0) seconds_until=5400>",
        "clan": {"tag": "#A", "name": "A", "stars": 5, "members": [{"attacks": [{"stars": 2}]}]},
        "opponent": {"tag": "#B", "name": "B", "stars": 6, "members": [{"attacks": [{"stars": 3}]}]},
    }


def _clan_name(_tag: str, default: str = "UNKNOWN") -> str:
    _ = default
    return "MyClan"


def _empty_calc(*_args: Any, **_kwargs: Any) -> Dict[str, Dict[str, Any]]:
    return {}


def _war_info_stub(_tag: str) -> str:
    return "war-info"


def _history_rows(_tag: str, month: int | None, _year: int | None, _cwl_season: str | None = None) -> list[dict[str, str]]:
    return [{"WarID": f"W{month}"}]


def _max_possible_stars(_war_data: Dict[str, Any], _tag: str) -> int:
    return 1


@pytest.mark.smoke
def test_generate_war_info_text_family_tag_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf.CACHE, "clan_families", {"#FAM": {"name": "Family"}}, raising=False)

    out = hf.generate_war_info_text("#FAM")

    assert out == ""


@pytest.mark.smoke
def test_generate_war_info_text_no_war_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf.CACHE, "clan_families", {}, raising=False)
    monkeypatch.setattr(hf.CACHE, "get_current_war_data", _war_data_none, raising=False)

    out = hf.generate_war_info_text("#CLAN")

    assert "No current war information" in out


@pytest.mark.smoke
def test_generate_war_info_text_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf.CACHE, "clan_families", {}, raising=False)
    monkeypatch.setattr(hf.CACHE, "get_current_war_data", _war_data_preparation, raising=False)

    out = hf.generate_war_info_text("#CLAN")

    assert "War preparation" in out
    assert "War starts in:" in out


@pytest.mark.smoke
def test_generate_war_info_text_in_war_probability_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf.CACHE, "clan_families", {}, raising=False)
    monkeypatch.setattr(hf.CACHE, "get_current_war_data", _war_data_in_war, raising=False)

    import QBwarsim

    monkeypatch.setattr(QBwarsim, "calculate_max_possible_stars", _max_possible_stars)

    def _boom(*_args: Any, **_kwargs: Any) -> tuple[int, int, int, int]:
        raise RuntimeError("sim fail")

    monkeypatch.setattr(QBwarsim, "calculate_win_probability", _boom)

    out = hf.generate_war_info_text("#CLAN")

    assert "War in progress" in out
    assert "Stars Max" in out


@pytest.mark.smoke
def test_generate_leaderboard_text_unknown_mode() -> None:
    out = hf.generate_leaderboard_text("#CLAN", month=1, year=2026, mode="doesnotexist")

    assert "Unknown leaderboard mode" in out


@pytest.mark.smoke
def test_generate_leaderboard_text_currentwar_without_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf.CACHE, "clan_families", {}, raising=False)
    monkeypatch.setattr(hf.CACHE, "get_clan_name", _clan_name, raising=False)
    monkeypatch.setattr(hf, "calculate_leaderboard", _empty_calc)
    monkeypatch.setattr(hf, "generate_war_info_text", _war_info_stub)

    out = hf.generate_leaderboard_text("#CLAN", mode="currentwar")

    assert "No active war at the moment" in out
    assert "MyClan" in out
    assert "Current War" in out


@pytest.mark.smoke
def test_generate_leaderboard_text_multimonth_aggregates_and_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf.CACHE, "clan_families", {}, raising=False)
    monkeypatch.setattr(hf.CACHE, "get_clan_name", _clan_name, raising=False)

    def _calc(_clan_tag: str, month: int, _year: int, **_kwargs: Any) -> Dict[str, Dict[str, Any]]:
        if month == 1:
            return {
                "#P1": {
                    "Player": "A",
                    "PlayerID": "#P1",
                    "TH_lvl": 15,
                    "Stars": 2,
                    "Attacks": 1,
                    "Missed_Attacks": 1,
                    "Defensive_Stars": 1,
                    "Wars_Count": 1,
                    "Def_Stars_per_War": 1.0,
                }
            }
        return {
            "#P1": {
                "Player": "A",
                "PlayerID": "#P1",
                "TH_lvl": 16,
                "Stars": 3,
                "Attacks": 2,
                "Missed_Attacks": 0,
                "Defensive_Stars": 2,
                "Wars_Count": 1,
                "Def_Stars_per_War": 2.0,
            }
        }

    monkeypatch.setattr(hf, "calculate_leaderboard", _calc)
    monkeypatch.setattr(
        hf,
        "_load_history_filtered",
        _history_rows,
    )

    def _render(
        clan_tag: str,
        clan_name: str,
        month_label: str,
        war_info_line: str,
        stats_by_player: Dict[str, Dict[str, Any]],
        mode: str,
        style: str = "discord",
        **_kwargs: Any,
    ) -> str:
        _ = (clan_tag, war_info_line, mode, style)
        player = stats_by_player["#P1"]
        return f"{clan_name}|{month_label}|TH={player['TH_lvl']}|Stars={player['Stars']}|Wars={player['Wars_Count']}"

    monkeypatch.setattr(hf, "render_leaderboard", _render)

    out = hf.generate_leaderboard_text("#CLAN", month=[1, 2], year=2026, mode="attack")

    assert "MyClan" in out
    assert "01-02/2026" in out
    assert "TH=16" in out
    assert "Stars=5" in out
    assert "Wars=2" in out


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_delete_leaderboard_messages_for_context_builds_mode_string(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    async def _fake_delete(channel_id: str, predicate: Any, context: str) -> None:
        captured["channel_id"] = channel_id
        captured["context"] = context
        sample = {
            "mode": "attack_cwl_02_2026",
            "clan_tag": "#CLAN",
            "channel_id": "123",
        }
        captured["matched"] = bool(predicate("k", sample))

    import qapbot.QBdiscocmdshelper as helper

    monkeypatch.setattr(helper, "_delete_messages_by_filter", _fake_delete)

    await hf.delete_leaderboard_messages_for_context("#CLAN", "123", "attack", "02", 2026, cwl_only=True)

    assert captured["channel_id"] == "123"
    assert captured["matched"] is True
    assert "attack_cwl_02_2026" in captured["context"]


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_split_and_post_leaderboard_helper_short_message(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _FakeChannel()

    async def _discord_retry(op: Any, _name: str) -> Any:
        return await op()

    monkeypatch.setattr(hf, "discord_retry", _discord_retry)

    ids = await hf._split_and_post_leaderboard_helper(cast(Any, channel), "short text")

    assert ids == [1]
    assert len(channel.sent) == 1
    assert channel.sent[0].startswith("```")


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_split_and_post_leaderboard_helper_truncates_overlong_player_line(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _FakeChannel()

    async def _discord_retry(op: Any, _name: str) -> Any:
        return await op()

    monkeypatch.setattr(hf, "discord_retry", _discord_retry)

    text = "\n".join(
        [
            "Header",
            "Player Stars Attacks",
            "-------------------",
            "X" * 2600,
        ]
    )

    ids = await hf._split_and_post_leaderboard_helper(cast(Any, channel), text)

    assert len(ids) >= 2
    assert any("..." in payload for payload in channel.sent)


# ---------------------------------------------------------------------------
# Sentinel (plain + code block combined) paths
# ---------------------------------------------------------------------------

def _make_sentinel_text(plain: str, code: str, prefix: str = "") -> str:
    """Build a sentinel-delimited text string as generate_war_info_text would."""
    body = f"{hf._PLAIN_SENTINEL_START}{plain}{hf._PLAIN_SENTINEL_END}\n{code}"
    return (prefix + "\n\n" + body) if prefix else body


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sentinel_combined_single_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short plain + code: must produce exactly one message containing both parts."""
    channel = _FakeChannel()

    async def _discord_retry(op: Any, _name: str) -> Any:
        return await op()

    monkeypatch.setattr(hf, "discord_retry", _discord_retry)

    plain = "Rosters:\nClanA: 🏯×5\nClanB: 🏯×5\n\n🛡️ War in progress | War ends in: 2h 30m"
    code = "Clan       Stars\n---------- -----\nClanA      10\nClanB       8"
    text = _make_sentinel_text(plain, code, prefix="⭐ ClanA - Current War:")

    ids = await hf._split_and_post_leaderboard_helper(cast(Any, channel), text)

    assert len(ids) == 1, f"Expected 1 message, got {len(ids)}: {channel.sent}"
    msg = channel.sent[0]
    # Plain content must appear before the code block
    assert "Rosters:" in msg
    assert "🛡️ War in progress" in msg
    assert "```" in msg
    assert "ClanA      10" in msg
    # Must NOT start with ``` (plain text comes first)
    assert not msg.startswith("```")


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sentinel_combined_prefix_absorbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The header prefix before the sentinel must appear in the same message."""
    channel = _FakeChannel()

    async def _discord_retry(op: Any, _name: str) -> Any:
        return await op()

    monkeypatch.setattr(hf, "discord_retry", _discord_retry)

    plain = "Rosters:\nClanA: 🏯×5"
    code = "Clan  Stars\n----- -----\nA     5"
    text = _make_sentinel_text(plain, code, prefix="⭐ ClanA - Current War:")

    ids = await hf._split_and_post_leaderboard_helper(cast(Any, channel), text)

    assert len(ids) == 1
    assert "⭐ ClanA - Current War:" in channel.sent[0]


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sentinel_combined_too_long_falls_back_to_two_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """When plain+code exceeds 2000 chars, they must be posted as separate messages."""
    channel = _FakeChannel()

    async def _discord_retry(op: Any, _name: str) -> Any:
        return await op()

    monkeypatch.setattr(hf, "discord_retry", _discord_retry)
    monkeypatch.setattr(hf, "DISCORD_MESSAGE_MAX_LENGTH", 50)  # force split

    plain = "Rosters:\n" + "ClanA: 🏯×5\n" * 5
    code = "Player  Stars Attacks\n" + "PlayerX  10  5\n" * 5
    text = _make_sentinel_text(plain, code)

    ids = await hf._split_and_post_leaderboard_helper(cast(Any, channel), text)

    # Must be at least 2 messages (plain chunk(s) + code block)
    assert len(ids) >= 2
    # First message(s) must contain the plain roster content, not start with ```
    assert any("Rosters:" in m for m in channel.sent)
    # At least one message must be a code block for the table
    assert any(m.startswith("```") for m in channel.sent)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sentinel_plain_only_no_code_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain sentinel with no following code block posts plain text only (no ``` wrap)."""
    channel = _FakeChannel()

    async def _discord_retry(op: Any, _name: str) -> Any:
        return await op()

    monkeypatch.setattr(hf, "discord_retry", _discord_retry)

    # Sentinel with no code after it (preparation state: no stats table yet)
    text = f"{hf._PLAIN_SENTINEL_START}Rosters:\nClanA: 🏯×5\n\n🛡️ War starts in: 1h 0m{hf._PLAIN_SENTINEL_END}"

    ids = await hf._split_and_post_leaderboard_helper(cast(Any, channel), text)

    assert len(ids) == 1
    assert "Rosters:" in channel.sent[0]
    # Plain message: must NOT be wrapped in a code block
    assert not channel.sent[0].startswith("```")
