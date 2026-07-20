"""Integration tests for CoC/clan role DB schema and CRUD methods.

Verifies:
  - guild_config table has the new boolean columns
  - save/get_guild_config round-trips the new fields correctly
  - save_guild_clan_role + delete_guild_clan_role work
  - clan_roles are included in get_guild_config result
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from qapbot.db_manager import WarHistoryDB


@pytest.fixture
async def db(tmp_path: Path):
    from qapbot.db_manager import WarHistoryDB

    db_path = tmp_path / "qapbot_role_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    try:
        yield manager
    finally:
        await manager.close()


GUILD_ID = "987654321"


@pytest.mark.integration
class TestGuildConfigRoleColumns:
    """guild_config table stores new role feature columns."""

    async def test_save_and_get_coc_role_enabled(self, db: "WarHistoryDB"):
        config: dict[str, Any] = {
            "language": "en",
            "coc_role_enabled": True,
            "clan_role_enabled": False,
            "coc_role_member_id": "111",
            "coc_role_elder_id": "222",
            "coc_role_coleader_id": "333",
            "coc_role_leader_id": "444",
        }
        await db.save_guild_config(GUILD_ID, config)
        loaded = await db.get_guild_config(GUILD_ID)

        assert loaded is not None
        assert loaded["coc_role_enabled"] is True
        assert loaded["clan_role_enabled"] is False
        assert loaded["coc_role_member_id"] == "111"
        assert loaded["coc_role_elder_id"] == "222"
        assert loaded["coc_role_coleader_id"] == "333"
        assert loaded["coc_role_leader_id"] == "444"

    async def test_defaults_to_false_when_not_set(self, db: "WarHistoryDB"):
        config = {"language": "en"}
        await db.save_guild_config(GUILD_ID, config)
        loaded = await db.get_guild_config(GUILD_ID)

        assert loaded is not None
        assert loaded["coc_role_enabled"] is False
        assert loaded["clan_role_enabled"] is False
        assert loaded["coc_role_member_id"] is None
        assert loaded["coc_role_leader_id"] is None

    async def test_update_role_ids(self, db: "WarHistoryDB"):
        await db.save_guild_config(GUILD_ID, {"coc_role_member_id": "10"})
        await db.save_guild_config(GUILD_ID, {"coc_role_member_id": "20"})
        loaded = await db.get_guild_config(GUILD_ID)

        assert loaded is not None
        assert loaded["coc_role_member_id"] == "20"


@pytest.mark.integration
class TestGuildClanRolesCRUD:
    """save_guild_clan_role / delete_guild_clan_role + get_guild_config returns clan_roles."""

    async def test_save_and_retrieve_clan_role(self, db: "WarHistoryDB"):
        await db.save_guild_config(GUILD_ID, {"language": "en"})
        await db.save_guild_clan_role(GUILD_ID, "#CLAN1", "5001")

        loaded = await db.get_guild_config(GUILD_ID)
        assert loaded is not None
        assert loaded["clan_roles"] == {"#CLAN1": "5001"}

    async def test_upsert_updates_existing_role(self, db: "WarHistoryDB"):
        await db.save_guild_config(GUILD_ID, {"language": "en"})
        await db.save_guild_clan_role(GUILD_ID, "#CLAN1", "5001")
        await db.save_guild_clan_role(GUILD_ID, "#CLAN1", "9999")

        loaded = await db.get_guild_config(GUILD_ID)
        assert loaded is not None
        assert loaded["clan_roles"]["#CLAN1"] == "9999"

    async def test_multiple_clans(self, db: "WarHistoryDB"):
        await db.save_guild_config(GUILD_ID, {"language": "en"})
        await db.save_guild_clan_role(GUILD_ID, "#A", "1")
        await db.save_guild_clan_role(GUILD_ID, "#B", "2")
        await db.save_guild_clan_role(GUILD_ID, "#C", "3")

        loaded = await db.get_guild_config(GUILD_ID)
        assert loaded is not None
        assert len(loaded["clan_roles"]) == 3
        assert loaded["clan_roles"]["#B"] == "2"

    async def test_delete_removes_entry(self, db: "WarHistoryDB"):
        await db.save_guild_config(GUILD_ID, {"language": "en"})
        await db.save_guild_clan_role(GUILD_ID, "#CLAN1", "5001")
        await db.save_guild_clan_role(GUILD_ID, "#CLAN2", "5002")

        await db.delete_guild_clan_role(GUILD_ID, "#CLAN1")
        loaded = await db.get_guild_config(GUILD_ID)

        assert loaded is not None
        assert "#CLAN1" not in loaded["clan_roles"]
        assert "#CLAN2" in loaded["clan_roles"]

    async def test_delete_nonexistent_is_noop(self, db: "WarHistoryDB"):
        await db.save_guild_config(GUILD_ID, {"language": "en"})
        # Should not raise
        await db.delete_guild_clan_role(GUILD_ID, "#DOESNOTEXIST")

    async def test_clan_roles_empty_when_none_saved(self, db: "WarHistoryDB"):
        await db.save_guild_config(GUILD_ID, {"language": "en"})
        loaded = await db.get_guild_config(GUILD_ID)
        assert loaded is not None
        assert loaded["clan_roles"] == {}

    async def test_cascade_delete_on_guild_config_removal(self, db: "WarHistoryDB"):
        """Deleting guild_config should cascade-delete guild_clan_roles."""
        await db.save_guild_config(GUILD_ID, {"language": "en"})
        await db.save_guild_clan_role(GUILD_ID, "#CLAN1", "5001")

        async with db._write_lock:  # type: ignore[reportPrivateUsage]
            await db.conn.execute(  # type: ignore[union-attr]
                "DELETE FROM guild_config WHERE guild_id = ?", (GUILD_ID,)
            )
            await db.conn.commit()  # type: ignore[union-attr]

        cursor = await db.conn.execute(  # type: ignore[union-attr]
            "SELECT COUNT(*) AS cnt FROM guild_clan_roles WHERE guild_id = ?", (GUILD_ID,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["cnt"] == 0
