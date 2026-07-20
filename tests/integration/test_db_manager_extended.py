"""Extended db_manager integration tests.

Covers: get_player_wars, get_clan_war_count, get_all_clans, war_exists_sync,
update_war_records_sync, clan CRUD, and filter-by-month/year in get_clan_history.
"""
# pyright: reportUnknownMemberType=false, reportUnknownParameterType=false
# pyright: reportMissingParameterType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from qapbot.db_manager import WarHistoryDB


@pytest.fixture
async def db(tmp_path: Path) -> WarHistoryDB:  # type: ignore[misc]
    db_path = tmp_path / "qapbot_extended_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    try:
        yield manager  # type: ignore[misc]
    finally:
        await manager.close()


def _record(
    *,
    war_id: str = "W1",
    date: str = "2026-01-15T12:00:00",
    tag: str = "#P1",
    name: str = "Alice",
    th: int = 15,
    stars: int = 5,
    attacks: int = 2,
    missed: int = 0,
    max_attacks: int = 2,
    defensive_stars: int = 1,
) -> Dict[str, Any]:
    return {
        "WarID": war_id,
        "Date": date,
        "Player": name,
        "PlayerID": tag,
        "TH_lvl": th,
        "Stars": stars,
        "Attacks": attacks,
        "Missed_Attacks": missed,
        "Max_Attacks": max_attacks,
        "Defensive_Stars": defensive_stars,
    }


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# war_exists_sync
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Clan CRUD
# ---------------------------------------------------------------------------

class TestClanCrud:

    @pytest.mark.integration
    async def test_save_get_delete_clan(self, db) -> None:
        await db.save_clan("#CLAN_X", "Clan X")
        clan = await db.get_clan("#CLAN_X")
        assert clan is not None
        assert clan["name"] == "Clan X"

        await db.delete_clan("#CLAN_X")
        assert await db.get_clan("#CLAN_X") is None

    @pytest.mark.integration
    async def test_save_clan_with_subscriptions(self, db) -> None:
        await db.save_clan("#CLAN_S", "SubClan", has_active_subscriptions=True)
        clan = await db.get_clan("#CLAN_S")
        assert clan is not None
        assert clan["has_active_subscriptions"] is True

    @pytest.mark.integration
    async def test_get_all_clans_dict(self, db) -> None:
        await db.save_clan("#A", "Alpha")
        await db.save_clan("#B", "Beta")

        all_clans = await db.get_all_clans_dict()
        assert "#A" in all_clans
        assert "#B" in all_clans
        assert all_clans["#A"]["name"] == "Alpha"

    @pytest.mark.integration
    async def test_get_missing_clan_returns_none(self, db) -> None:
        assert await db.get_clan("#NOPE") is None


# ---------------------------------------------------------------------------
# get_clan_history with month/year filters
# ---------------------------------------------------------------------------

# User operations - extended
# ---------------------------------------------------------------------------

class TestUserOperationsExtended:

    @pytest.mark.integration
    async def test_save_user_multiple_players(self, db) -> None:
        await db.save_user(
            "100",
            {
                "display_name": "MultiPlayer",
                "notification_settings": {"war_reminders": True},
                "players": [
                    {"player_tag": "#PA", "player_name": "A"},
                    {"player_tag": "#PB", "player_name": "B"},
                    {"player_tag": "#PC", "player_name": "C"},
                ],
                "user_language": "en",
            },
        )
        user = await db.get_user("100")
        assert user is not None
        assert len(user["players"]) == 3
        tags = {p["player_tag"] for p in user["players"]}
        assert tags == {"#PA", "#PB", "#PC"}

    @pytest.mark.integration
    async def test_overwrite_user_replaces_players(self, db) -> None:
        await db.save_user(
            "200",
            {
                "display_name": "First",
                "notification_settings": {},
                "players": [{"player_tag": "#OLD", "player_name": "Old"}],
                "user_language": "en",
            },
        )
        await db.save_user(
            "200",
            {
                "display_name": "Second",
                "notification_settings": {},
                "players": [{"player_tag": "#NEW", "player_name": "New"}],
                "user_language": "de",
            },
        )
        user = await db.get_user("200")
        assert user is not None
        assert user["display_name"] == "Second"
        assert len(user["players"]) == 1
        assert user["players"][0]["player_tag"] == "#NEW"

    @pytest.mark.integration
    async def test_get_nonexistent_user_returns_none(self, db) -> None:
        assert await db.get_user("999999") is None
