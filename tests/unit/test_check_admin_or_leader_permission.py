"""Tests for check_admin_or_leader_permission() (QBdiscocmdshelper.py) — the permission check
behind CWL Management's "Manage Assignment" screen (CWL_ROSTER_PLANNING_PLAN.md "Manage
Enrollment", 2026-08-10): admin, configured bot admin, OR a current holder of the guild's
Leader/Co-Leader Discord role (one shared pair of roles per guild, not per-clan).
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")


def _make_member(roles=None, is_admin: bool = False):
    member = MagicMock(spec=discord.Member)
    member.id = 111
    member.guild_permissions.administrator = is_admin
    member.roles = roles or []
    return member


def _make_role(role_id: int):
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    return role


@pytest.mark.discord
@pytest.mark.asyncio
async def test_guild_admin_passes(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_admin_or_leader_permission

    mock_interaction.user = _make_member(is_admin=True)
    result = await check_admin_or_leader_permission(mock_interaction, "999", {})
    assert result is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_configured_bot_admin_passes_regardless_of_roles(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_admin_or_leader_permission

    mock_interaction.user = _make_member(is_admin=False)
    mock_interaction.user.id = 555
    result = await check_admin_or_leader_permission(mock_interaction, "555", {})
    assert result is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_leader_role_holder_passes(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_admin_or_leader_permission

    mock_interaction.user = _make_member(roles=[_make_role(1001)], is_admin=False)
    guild_config = {"coc_role_leader_id": "1001", "coc_role_coleader_id": "1002"}

    assert await check_admin_or_leader_permission(mock_interaction, "999", guild_config) is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_coleader_role_holder_passes(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_admin_or_leader_permission

    mock_interaction.user = _make_member(roles=[_make_role(1002)], is_admin=False)
    guild_config = {"coc_role_leader_id": "1001", "coc_role_coleader_id": "1002"}

    assert await check_admin_or_leader_permission(mock_interaction, "999", guild_config) is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_regular_member_without_role_fails(mock_interaction):
    from qapbot.QBdiscocmdshelper import check_admin_or_leader_permission

    mock_interaction.user = _make_member(roles=[_make_role(5555)], is_admin=False)  # unrelated role
    guild_config = {"coc_role_leader_id": "1001", "coc_role_coleader_id": "1002"}

    assert await check_admin_or_leader_permission(mock_interaction, "999", guild_config) is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_no_leader_roles_configured_fails_cleanly_for_non_admin(mock_interaction):
    """Guild has never set up the CoC-rank auto-role feature — must not crash, just deny."""
    from qapbot.QBdiscocmdshelper import check_admin_or_leader_permission

    mock_interaction.user = _make_member(roles=[_make_role(1)], is_admin=False)
    assert await check_admin_or_leader_permission(mock_interaction, "999", {}) is False


@pytest.mark.discord
@pytest.mark.asyncio
async def test_dm_context_with_no_matching_admin_fails_closed(mock_interaction):
    """interaction.user is not a discord.Member at all (DM invocation, no resolved_guild_id) —
    the role-holder check has nothing to check against and must fail closed, not raise."""
    from qapbot.QBdiscocmdshelper import check_admin_or_leader_permission

    mock_interaction.user = MagicMock()  # plain User, not a Member — isinstance check fails
    mock_interaction.guild = None
    guild_config = {"coc_role_leader_id": "1001"}

    assert await check_admin_or_leader_permission(mock_interaction, "999", guild_config) is False


# ---------------------------------------------------------------------------
# _check_cwl_admin_or_leader_permission (ui_cwl_roster.py) — the "Manage Assignment" launch
# gate. Deliberately NOT in tests/discord/test_ui_cwl_roster.py: that file's autouse
# _bypass_cwl_admin_check fixture forces check_admin_permissions() to always return True for
# every test there, which would silently defeat any test of the leader-role-holder path.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_cwl_admin_or_leader_permission_allows_leader(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import _check_cwl_admin_or_leader_permission

    mock_interaction.guild.id = 9401
    mock_interaction.user = _make_member(roles=[_make_role(1001)])
    CACHE.server_config["9401"] = {"coc_role_leader_id": "1001"}

    assert await _check_cwl_admin_or_leader_permission(mock_interaction) is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_cwl_admin_or_leader_permission_rejects_regular_member(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import _check_cwl_admin_or_leader_permission

    mock_interaction.guild.id = 9402
    mock_interaction.user = _make_member(roles=[])
    CACHE.server_config["9402"] = {"coc_role_leader_id": "1001"}

    result = await _check_cwl_admin_or_leader_permission(mock_interaction)

    assert result is False
    # mock_interaction.response.is_done() is a real sync MagicMock returning False (conftest.py),
    # matching discord.py's actual sync is_done() — so the rejection deterministically goes via
    # response.send_message(), not followup.send().
    mock_interaction.response.send_message.assert_awaited_once()
    mock_interaction.followup.send.assert_not_awaited()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_check_cwl_admin_or_leader_permission_allows_admin_without_leader_role(mock_interaction):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_cwl_roster import _check_cwl_admin_or_leader_permission

    mock_interaction.guild.id = 9403
    mock_interaction.user = _make_member(is_admin=True)
    CACHE.server_config["9403"] = {}

    assert await _check_cwl_admin_or_leader_permission(mock_interaction) is True
