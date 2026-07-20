"""DEV live smoke: send a leaderboard message and assert read-back via Discord API.

This test is intentionally content-light: it validates the end-to-end Discord
message lifecycle (send -> fetch -> delete) using a leaderboard-shaped payload.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.smoke_live._review import maybe_wait_for_dev_review


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"Missing env var: {name}")
    return value


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_leaderboard_posts_and_deletes_message(review_timeout_seconds: int):
    import discord

    from qapbot.formatting import render_leaderboard

    _required_env("DISCORD_TOKEN_DEV")
    _required_env("DISCORD_GUILD_ID")
    _required_env("DEV_PLAYERREGISTRATION_CHANNEL_ID")

    channel_id = int(_required_env("DEV_PLAYERREGISTRATION_CHANNEL_ID"))

    leaderboard_text = render_leaderboard(
        clan_tag="#SMOKE",
        clan_name="SmokeClan",
        month_label="",
        war_info_line="",
        stats_by_player={
            "#P1": {"Player": "Alice", "TH_lvl": 15, "Stars": 6, "Attacks": 2, "Defensive_Stars": 2},
            "#P2": {"Player": "Bob", "TH_lvl": 14, "Stars": 3, "Attacks": 2, "Defensive_Stars": 4},
        },
        mode="attack",
        style="discord",
    )
    assert "Leaderboard" in leaderboard_text

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

            msg = await channel.send(leaderboard_text)  # type: ignore[attr-defined]
            fetched = await channel.fetch_message(msg.id)  # type: ignore[attr-defined]
            assert "SmokeClan" in fetched.content
            assert "Attack Leaderboard" in fetched.content

            verdict = await maybe_wait_for_dev_review(
                channel=channel,
                message=msg,
                timeout_seconds=review_seconds,
            )
            if verdict is False:
                await msg.delete()
                raise AssertionError("Developer marked leaderboard as NOT OK (❌)")
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
