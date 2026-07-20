from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock


class FakeCache:
    def __init__(self) -> None:
        self.user_accounts: Dict[str, Dict[str, Any]] = {}
        self.clan_name_cache: Dict[str, Dict[str, Any]] = {}
        self.clan_families: Dict[str, Dict[str, Any]] = {}
        self.server_config: Dict[str, Dict[str, Any]] = {}
        self.temp_war_stats: Dict[str, Dict[str, Any]] = {}
        self.clan_history: Dict[str, list[Dict[str, Any]]] = {}
        # Stub for coc_clan_cache used by ClanManagementView._on_refresh
        self.coc_clan_cache = SimpleNamespace(invalidate=lambda tag: None, get_clan=AsyncMock(return_value=None))

        self.persist_user = AsyncMock()
        self.set_user_account = AsyncMock(side_effect=self._set_user_account)
        self.get_player = AsyncMock(return_value=None)

    async def _set_user_account(self, user_id: str, user_data: Dict[str, Any]) -> None:
        self.user_accounts[user_id] = user_data

    def get_clan_name(self, clan_tag: str, default: Optional[str] = "Unknown") -> Optional[str]:
        return self.clan_name_cache.get(clan_tag, {}).get("name", default)


def make_interaction(*, user_id: int = 123456789, guild_id: int = 987654321) -> Any:
    response = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
        send_modal=AsyncMock(),
        defer=AsyncMock(),
        is_done=lambda: False,
    )
    followup = SimpleNamespace(send=AsyncMock())
    channel = SimpleNamespace(id=111222333, send=AsyncMock(), fetch_message=AsyncMock())
    user = SimpleNamespace(id=user_id, name="TestUser", display_name="TestUser", mention="@TestUser")
    guild = SimpleNamespace(id=guild_id, name="TestGuild")

    interaction = SimpleNamespace(
        user=user,
        guild=guild,
        guild_id=guild_id,
        channel=channel,
        response=response,
        followup=followup,
        locale="en",
        data={},
        delete_original_response=AsyncMock(),
        edit_original_response=AsyncMock(),
    )
    return interaction


def identity_t(key: str, **kwargs: Any) -> str:
    _ = kwargs
    return key
