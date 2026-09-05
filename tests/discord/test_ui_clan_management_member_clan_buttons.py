"""Tests for MemberClansConfigurationView's button rendering (2026-08-19 live bug report): a
clan family's own member clans must not ALSO get their own redundant "individual clan" toggle
button next to the family's button — the family button already grants membership for every one
of its clans. Only genuinely standalone clans (not in any displayed family) still get one, and a
clan that was already individually selected before this fix existed still keeps its button too,
so an admin can toggle it back off rather than being stuck with an un-removable legacy selection.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")


@pytest.fixture(autouse=True)
def _bypass_admin_check(monkeypatch):
    import qapbot.QBdiscocmdshelper as helper

    async def _always_admin(*args, **kwargs):
        return True

    monkeypatch.setattr(helper, "check_admin_permissions", _always_admin)


def _make_view(guild, guild_clans, current_member_clans, current_member_families, clan_families):
    from qapbot.ui_clan_management import MemberClansConfigurationView

    clan_management_view = MagicMock()
    clan_management_view.sent_message = MagicMock(guild=guild)
    clan_management_view.guild_clans = list(guild_clans)

    original_interaction = MagicMock()
    original_interaction.guild = guild

    return MemberClansConfigurationView(
        guild=guild,
        clan_management_view=clan_management_view,
        original_interaction=original_interaction,
        current_member_clans=list(current_member_clans),
        current_member_families=list(current_member_families),
        clan_families=clan_families,
        timeout=300,
    )


def _clan_button_tags(view) -> set:
    return {
        (child.custom_id or "").removeprefix("member_clan_")
        for child in view.children
        if isinstance(child, discord.ui.Button) and (child.custom_id or "").startswith("member_clan_")
    }


@pytest.mark.discord
def test_family_member_clans_get_no_individual_button(mock_interaction):
    from qapbot.cache_manager import CACHE

    CACHE.clan_name_cache = {
        "#A": {"name": "StayA"}, "#B": {"name": "StayB"}, "#STANDALONE": {"name": "Akatsuki"},
    }
    view = _make_view(
        mock_interaction.guild,
        guild_clans=["#A", "#B", "#STANDALONE"],
        current_member_clans=[],
        current_member_families=["fam1"],
        clan_families={"fam1": {"name": "Stay-Family", "clans": ["#A", "#B"]}},
    )

    tags = _clan_button_tags(view)
    assert tags == {"#STANDALONE"}  # #A/#B are covered by the Stay-Family button, no duplicate

    family_buttons = [
        c for c in view.children if isinstance(c, discord.ui.Button) and (c.custom_id or "") == "family_fam1"
    ]
    assert len(family_buttons) == 1


@pytest.mark.discord
def test_previously_individually_selected_family_clan_keeps_its_button(mock_interaction):
    """Legacy state from before this fix (a family-member clan sitting in member_clans on its
    own, family not selected) must not become permanently stuck — it still needs a button so the
    admin can toggle it back off."""
    from qapbot.cache_manager import CACHE

    CACHE.clan_name_cache = {"#A": {"name": "StayA"}, "#B": {"name": "StayB"}}
    view = _make_view(
        mock_interaction.guild,
        guild_clans=["#A", "#B"],
        current_member_clans=["#A"],  # legacy individual selection, family NOT selected
        current_member_families=[],
        clan_families={"fam1": {"name": "Stay-Family", "clans": ["#A", "#B"]}},
    )

    tags = _clan_button_tags(view)
    assert tags == {"#A"}  # #A keeps its button (removable); #B still gets none (never selected)


@pytest.mark.discord
def test_family_not_shown_still_lets_its_clans_be_individually_toggled(mock_interaction):
    """A family beyond the 5-family button cap has no button of its own here — its clans must
    still be individually selectable, since the family toggle isn't reachable in this view."""
    from qapbot.cache_manager import CACHE

    CACHE.clan_name_cache = {"#A": {"name": "StayA"}}
    many_families = {f"fam{i}": {"name": f"Family{i}", "clans": [f"#F{i}"]} for i in range(5)}
    clan_families = dict(many_families)
    clan_families["fam_extra"] = {"name": "Extra Family", "clans": ["#A"]}

    view = _make_view(
        mock_interaction.guild,
        guild_clans=["#A"],
        current_member_clans=[],
        current_member_families=[],
        clan_families=clan_families,
    )

    tags = _clan_button_tags(view)
    assert "#A" in tags  # fam_extra didn't make the 5-family cap, so #A still needs its button
