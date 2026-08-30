"""Tracker #0086 / #0087 — the CWL coordinator guild role and the coordinator mention line.

#0086: guild_role_manager.sync_cwl_coordinator_role() keeps an EXISTING, admin-linked role's
membership matching cwl_clan_coordinators (never creating or deleting the role itself).

#0087: _send_channel_war_notification() @-mentions a clan's CWL coordinators in the already
existing channel war notification, CWL wars only.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest


def _member(user_id: int, roles: List[Any] | None = None) -> MagicMock:
    m = MagicMock()
    m.id = user_id
    m.display_name = f"User{user_id}"
    m.roles = roles if roles is not None else []
    return m


def _guild_with_role(guild_id: int, role: MagicMock, members: Dict[int, MagicMock]) -> MagicMock:
    guild = MagicMock()
    guild.id = guild_id
    guild.get_role = MagicMock(return_value=role)
    guild.get_member = MagicMock(side_effect=lambda uid: members.get(uid))
    return guild


# ---------------------------------------------------------------------------
# #0086 — sync_cwl_coordinator_role
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_coordinator_role_is_added_to_configured_coordinators(monkeypatch):
    from qapbot.cache_manager import CACHE
    import qapbot.guild_role_manager as grm

    role = MagicMock()
    role.name = "CWL Coordinator"
    role.members = []
    members = {111: _member(111), 222: _member(222)}
    guild = _guild_with_role(9601, role, members)

    CACHE.server_config["9601"] = {
        "cwl_coordinator_role_id": "555",
        "cwl_clan_coordinators": {"#CLAN1": ["111"], "#CLAN2": ["222"]},
    }
    assign = AsyncMock(return_value=True)
    monkeypatch.setattr(grm, "assign_role_to_member", assign)
    monkeypatch.setattr(grm, "remove_role_from_member", AsyncMock(return_value=True))

    added, removed = await grm.sync_cwl_coordinator_role(guild)

    assert (added, removed) == (2, 0)
    assert {c.args[0].id for c in assign.await_args_list} == {111, 222}


@pytest.mark.asyncio
async def test_coordinator_role_kept_while_still_coordinator_of_another_clan(monkeypatch):
    """The rule this sync exists to get right: dropping someone as coordinator of clan A must NOT
    strip the role while they are still coordinator of clan B — the target set is the union across
    every configured clan, not a per-clan view."""
    from qapbot.cache_manager import CACHE
    import qapbot.guild_role_manager as grm

    role = MagicMock()
    role.name = "CWL Coordinator"
    still_coordinator = _member(111, roles=[role])
    role.members = [still_coordinator]
    guild = _guild_with_role(9602, role, {111: still_coordinator})

    CACHE.server_config["9602"] = {
        "cwl_coordinator_role_id": "555",
        # Removed from CLAN1, but still on CLAN2.
        "cwl_clan_coordinators": {"#CLAN1": [], "#CLAN2": ["111"]},
    }
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(grm, "assign_role_to_member", AsyncMock(return_value=True))
    monkeypatch.setattr(grm, "remove_role_from_member", remove)

    added, removed = await grm.sync_cwl_coordinator_role(guild)

    remove.assert_not_awaited()
    assert (added, removed) == (0, 0)


@pytest.mark.asyncio
async def test_coordinator_role_removed_from_a_former_coordinator(monkeypatch):
    from qapbot.cache_manager import CACHE
    import qapbot.guild_role_manager as grm

    role = MagicMock()
    role.name = "CWL Coordinator"
    former = _member(999, roles=[role])
    role.members = [former]
    guild = _guild_with_role(9603, role, {999: former})

    CACHE.server_config["9603"] = {
        "cwl_coordinator_role_id": "555",
        "cwl_clan_coordinators": {"#CLAN1": []},
    }
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(grm, "assign_role_to_member", AsyncMock(return_value=True))
    monkeypatch.setattr(grm, "remove_role_from_member", remove)

    added, removed = await grm.sync_cwl_coordinator_role(guild)

    remove.assert_awaited_once()
    assert remove.await_args.args[0].id == 999
    assert (added, removed) == (0, 1)


@pytest.mark.asyncio
async def test_coordinator_role_sync_noops_when_no_role_linked(monkeypatch):
    """No linked role is the default for every guild — must be a clean no-op, never an error and
    never a role lookup."""
    from qapbot.cache_manager import CACHE
    import qapbot.guild_role_manager as grm

    guild = MagicMock()
    guild.id = 9604
    guild.get_role = MagicMock()
    CACHE.server_config["9604"] = {"cwl_clan_coordinators": {"#CLAN1": ["111"]}}

    assert await grm.sync_cwl_coordinator_role(guild) == (0, 0)
    guild.get_role.assert_not_called()


# ---------------------------------------------------------------------------
# #0087 — coordinator mention line in the channel war notification
# ---------------------------------------------------------------------------

def _prepare_channel_notification(monkeypatch, *, is_cwl: bool, coordinators: List[str]):
    """Wire up the minimum CACHE/QBcore/CONFIG state _send_channel_war_notification needs, and
    return (module, war_data, sent) where `sent` collects the channel.send kwargs."""
    import qapbot.war_notifications as wn
    from qapbot.cache_manager import CACHE

    guild_id = "9700"
    clan_tag = "#CLAN1"

    CACHE.subscriptions = {guild_id: {"1": [{"clan_tag": clan_tag}]}}
    CACHE.server_config[guild_id] = {
        "channel_war_notifications_enabled": True,
        "war_notification_channel_id": "4242",
        "war_notification_threshold_hours": 4.0,
        "clan_custodians": {},
        "cwl_clan_coordinators": {clan_tag: coordinators},
    }

    sent: List[Dict[str, Any]] = []

    channel = MagicMock(spec=__import__("discord").TextChannel)

    async def fake_send(**kwargs):
        sent.append(kwargs)

    channel.send = fake_send

    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=channel)
    monkeypatch.setitem(__import__("sys").modules, "QBcore", MagicMock(bot=fake_bot))

    class _FakeConfig:
        is_dev_mode = False
        discord_guild_id = 0

    monkeypatch.setattr(wn, "CONFIG", _FakeConfig())
    monkeypatch.setattr(wn, "_is_channel_notification_sent", lambda *_a, **_k: False)
    monkeypatch.setattr(wn, "_record_channel_notification", AsyncMock())

    war_data = {
        "state": "in_war",
        "clan": {"tag": clan_tag, "name": "ClanA"},
        "opponent": {"tag": "#OPP1", "name": "ClanB"},
        "hours_remaining": 2.0,
        "is_cwl": is_cwl,
    }
    return wn, war_data, sent


@pytest.mark.asyncio
async def test_coordinators_are_mentioned_in_cwl_channel_notification(monkeypatch):
    wn, war_data, sent = _prepare_channel_notification(monkeypatch, is_cwl=True, coordinators=["111", "222"])

    players = [{"player_name": "Alpha", "attacks_remaining": 1}]
    count = await wn._send_channel_war_notification("#CLAN1", war_data, players)

    assert count == 1
    content = sent[0]["content"]
    assert "<@111>" in content and "<@222>" in content
    # The mentions have to actually ping, which is what makes this a usable reminder at all.
    assert sent[0]["allowed_mentions"].users is True


@pytest.mark.asyncio
async def test_coordinators_are_not_mentioned_for_a_regular_war(monkeypatch):
    """Coordinators are a CWL-only role, and the donation nudge ("before the next war day") is
    meaningless for a regular war, which has no next day."""
    wn, war_data, sent = _prepare_channel_notification(monkeypatch, is_cwl=False, coordinators=["111"])

    players = [{"player_name": "Alpha", "attacks_remaining": 1}]
    await wn._send_channel_war_notification("#CLAN1", war_data, players)

    assert sent[0]["content"] is None


@pytest.mark.asyncio
async def test_no_coordinator_line_when_clan_has_no_coordinators(monkeypatch):
    wn, war_data, sent = _prepare_channel_notification(monkeypatch, is_cwl=True, coordinators=[])

    players = [{"player_name": "Alpha", "attacks_remaining": 1}]
    await wn._send_channel_war_notification("#CLAN1", war_data, players)

    assert sent[0]["content"] is None
