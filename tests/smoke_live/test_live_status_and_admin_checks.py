"""DEV live smoke: exercise non-interactive command handlers.

Discord slash commands cannot be executed by a bot via API like a user.
Instead, we call the underlying handler functions and wire them to a real
DEV channel using a minimal interaction shim.

Admin checks that scan filesystem/logs are gated behind a CLI opt-in flag to
avoid accidental I/O or long runtimes during quick iteration.
"""

from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import pytest

from tests.smoke_live._review import maybe_wait_for_dev_review


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"Missing env var: {name}")
    return value


class _FakeResponse:
    async def defer(self, *, thinking: bool = False, ephemeral: bool = False):
        return None

    def is_done(self) -> bool:
        return True


@dataclass
class _FakeUser:
    id: int
    name: str = "SmokeTester"
    display_name: str = "SmokeTester"


class _StubCocClanCache:
    def get_stats(self) -> Dict[str, Any]:
        return {"size": 0}

    def get_memory_usage_mb(self) -> float:
        return 0.0


class _FakeDbManager:
    def get_global_db_statistics_sync(self, force_refresh: bool = False) -> Dict[str, int]:
        return {"clans_count": 0, "wars_count": 0, "attacks_count": 0, "players_count": 0, "players_tracked_count": 0}


class _FakeCache:
    def __init__(self):
        self.subscriptions: Dict[str, Any] = {}
        self.user_accounts: Dict[str, Any] = {}
        self.leaderboard_messages: Dict[str, Dict[str, Any]] = {}
        self.clan_families: Dict[str, Any] = {}
        self.clan_name_cache: Dict[str, Any] = {}
        self.coc_clan_cache = _StubCocClanCache()
        self.db_manager = _FakeDbManager()
        self._last_message_ids: Optional[list[int]] = None

    async def set_leaderboard_message(self, key: str, entry: Dict[str, Any]) -> None:
        self.leaderboard_messages[key] = entry
        msg_ids = [int(x) for x in (entry.get("message_ids") or "").split(",") if x.strip()]
        self._last_message_ids = msg_ids or None

    async def delete_leaderboard_message(self, key: str) -> None:
        self.leaderboard_messages.pop(key, None)

    def get_clan_name(self, clan_tag: str, default: str = "") -> str:
        value = self.clan_name_cache.get(clan_tag)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get("name") or default)
        return default


class _FakeInteraction:
    def __init__(self, *, channel: Any, guild: Any, user: _FakeUser):
        self.channel = channel
        self.guild = guild
        self.user = user
        self.response = _FakeResponse()
        self.followup = None
        self.command = None

    async def delete_original_response(self) -> None:
        return None


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_status_handler_posts_message_and_can_be_fetched(review_timeout_seconds: int):
    import discord

    _required_env("DISCORD_TOKEN_DEV")
    guild_id = int(_required_env("DISCORD_GUILD_ID"))
    channel_id = int(_required_env("DEV_PLAYERREGISTRATION_CHANNEL_ID"))

    fake_cache = _FakeCache()

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    review_seconds = review_timeout_seconds
    event_error: BaseException | None = None

    @client.event
    async def on_ready():
        nonlocal event_error
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)

            guild = client.get_guild(guild_id)
            if guild is None:
                guild = await client.fetch_guild(guild_id)

            # Patch globals inside the already-imported modules (import here, after env checks)
            import QBdiscordcmds
            import qapbot.QBdiscocmdshelper as helper

            QBdiscordcmds.CACHE = fake_cache  # type: ignore[assignment]
            helper.CACHE = fake_cache  # type: ignore[assignment]

            # Avoid depending on a fully started bot instance
            class _BotStub:
                start_time = datetime.now()
                last_sync = None

            QBdiscordcmds.QBcore.bot = _BotStub()  # type: ignore[assignment]
            QBdiscordcmds.GLOBAL_GUILD_ID = guild_id  # type: ignore[assignment]
            QBdiscordcmds.get_simple_discord_stats = lambda: {
                "total_calls": 0,
                "success_rate": 0,
                "rate_limits": 0,
                "api_errors": 0,
            }

            interaction = _FakeInteraction(
                channel=channel,
                guild=guild,
                user=_FakeUser(id=0),
            )

            await QBdiscordcmds.status.callback(interaction)  # type: ignore[arg-type]

            assert fake_cache._last_message_ids, "status() did not record any message IDs"
            msg = await channel.fetch_message(fake_cache._last_message_ids[-1])  # type: ignore[attr-defined]
            assert "QapBot Status" in msg.content

            verdict = await maybe_wait_for_dev_review(
                channel=channel,
                message=msg,
                timeout_seconds=review_seconds,
            )
            if verdict is False:
                await msg.delete()
                raise AssertionError("Developer marked status output as NOT OK (❌)")
            await msg.delete()
        except BaseException as exc:
            event_error = exc
        finally:
            await client.close()

    try:
        await asyncio.wait_for(
            client.start(os.environ["DISCORD_TOKEN_DEV"], reconnect=False),
            timeout=100 + review_seconds,
        )
    finally:
        if not client.is_closed():
            await client.close()
    if event_error is not None:
        raise event_error


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_admin_check_skeleton_is_opt_in():
    """Skeleton for /admin CHECK_LOGS and CHECK_DATA.

This is intentionally a skeleton: it does not execute any filesystem scans.
"""

    # Placeholder: implemented in a later step once the live admin checks are scoped
    # to DEV-only + read-only paths and have stable assertions.
    assert True
