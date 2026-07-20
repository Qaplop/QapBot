"""Live DEV smoke helpers.

These tests are intentionally coarse and will auto-skip unless DEV credentials
and the DEV test channel are configured.
"""

from __future__ import annotations

import os
import asyncio

import pytest

from tests.smoke_live._review import maybe_wait_for_dev_review


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"Missing env var: {name}")
    return value


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_can_send_and_read_back_message(review_timeout_seconds: int):
    import discord

    _required_env("DISCORD_TOKEN_DEV")
    guild_id = int(_required_env("DISCORD_GUILD_ID"))
    channel_id = int(_required_env("DEV_PLAYERREGISTRATION_CHANNEL_ID"))

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)

    sent_id: int | None = None
    review_seconds = review_timeout_seconds
    event_error: BaseException | None = None

    @client.event
    async def on_ready():
        nonlocal sent_id, event_error
        try:
            guild = client.get_guild(guild_id)
            if guild is None:
                guild = await client.fetch_guild(guild_id)

            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)

            test_text = "[smoke_live] message round-trip"
            msg = await channel.send(test_text)  # type: ignore[attr-defined]
            sent_id = msg.id
            fetched = await channel.fetch_message(msg.id)  # type: ignore[attr-defined]
            assert fetched.content == test_text

            verdict = await maybe_wait_for_dev_review(
                channel=channel,
                message=msg,
                timeout_seconds=review_seconds,
            )
            if verdict is False:
                await msg.delete()
                raise AssertionError("Developer marked smoke message as NOT OK (❌)")
            await msg.delete()
        except BaseException as exc:
            event_error = exc
        finally:
            await client.close()

    try:
        await asyncio.wait_for(
            client.start(os.environ["DISCORD_TOKEN_DEV"], reconnect=False),
            timeout=70 + review_seconds,
        )
    finally:
        if not client.is_closed():
            await client.close()
    if event_error is not None:
        raise event_error
    assert sent_id is not None
