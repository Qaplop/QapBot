from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

"""
Tracker #45: "My Accounts" only showed the first ~25 linked accounts (Discord's Select option
ceiling, hardcoded as user_players[:25]), so a user with more accounts than that couldn't reach
— let alone unlink — anything past the cutoff. No cap exists on how many accounts a user may
link, so this is a real scenario, not a theoretical one.

Covers:
  - AccountManagementView._build_player_select(): pages at _ACCOUNTS_PAGE_SIZE, no pagination
    row when everything fits on one page, pagination row (Prev/indicator/Next) appears and its
    buttons enable/disable correctly across pages when it doesn't.
  - AccountManagementView page-turn handlers: advance/retreat current_page and clear the
    current selection (so a stale selected_player_tag from another page can't drive an action).
  - AccountActionView: "Link new account" is a standalone button (not a Select option burning
    a slot), and its unverified-player Select paginates the same way.
  - Tracker #0056: AccountManagementView's re-entrancy guard against a second click landing
    while a prior one (esp. the CoC-API-fetching Refresh) is still mid-flight.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.discord.workflow_harness import FakeCache, identity_t, make_interaction


def _players(count: int, prefix: str = "#P") -> list[dict]:
    return [
        {"player_tag": f"{prefix}{i}", "player_name": f"Player{i}", "verified": False, "is_primary": False}
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# AccountManagementView
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_single_page_has_no_pagination_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {"players": _players(10)}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")

    assert view.prev_button is None
    assert view.next_button is None
    assert len(view.player_select.options) == 10


@pytest.mark.discord
@pytest.mark.asyncio
async def test_60_accounts_paginates_at_25_per_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact shape of tracker #45: a user with far more than 25 accounts."""
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {"players": _players(60)}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")

    assert len(view.player_select.options) == 25
    assert view.prev_button is not None
    assert view.next_button is not None
    assert view.prev_button.disabled is True  # already on first page
    assert view.next_button.disabled is False
    # Every account tag must be reachable across pages — nothing may be permanently hidden.
    total_pages = -(-60 // 25)
    assert total_pages == 3


@pytest.mark.discord
@pytest.mark.asyncio
async def test_page_next_and_prev_walk_through_all_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {"players": _players(60)}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")
    assert view.prev_button is not None and view.next_button is not None
    interaction = make_interaction(user_id=123)

    first_page_tags = {o.value for o in view.player_select.options}

    await view._on_page_next(interaction)
    assert view.current_page == 1
    second_page_tags = {o.value for o in view.player_select.options}
    assert len(second_page_tags) == 25
    assert first_page_tags.isdisjoint(second_page_tags)
    assert view.prev_button.disabled is False
    assert view.next_button.disabled is False

    await view._on_page_next(interaction)
    assert view.current_page == 2
    third_page_tags = {o.value for o in view.player_select.options}
    assert len(third_page_tags) == 10  # 60 - 25 - 25
    assert view.next_button.disabled is True  # last page reached
    assert view.prev_button.disabled is False

    # All 60 tags are reachable across the three pages combined.
    assert first_page_tags | second_page_tags | third_page_tags == {p["player_tag"] for p in _players(60)}

    # Next is a no-op past the last page's index guard — but Prev must walk all the way back.
    await view._on_page_prev(interaction)
    await view._on_page_prev(interaction)
    assert view.current_page == 0
    assert {o.value for o in view.player_select.options} == first_page_tags
    assert view.prev_button.disabled is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_page_turn_clears_stale_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A selection made on page 1 must not silently drive an action after paging away."""
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {"players": _players(30)}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")
    interaction = make_interaction(user_id=123)

    view.selected_player_tag = "#P0"
    view.verify_button.disabled = False
    view.primary_button.disabled = False
    view.unlink_button.disabled = False

    await view._on_page_next(interaction)

    assert view.selected_player_tag is None
    assert view.verify_button.disabled is True
    assert view.primary_button.disabled is True
    assert view.unlink_button.disabled is True


@pytest.mark.discord
@pytest.mark.asyncio
async def test_pagination_note_appears_in_overview_text(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {"players": _players(30)}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")
    content = view._build_message_content()

    # _build_message_content() locally re-imports t() from qapbot.i18n (not the module-level
    # `ui.t` this test monkeypatches), so the real translation renders here.
    assert "1" in content and "25" in content and "30" in content


# ---------------------------------------------------------------------------
# AccountActionView
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_action_view_link_new_is_a_button_not_a_select_option(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.ui_registration as ui

    monkeypatch.setattr(ui, "t", identity_t)
    interaction = make_interaction()

    view = ui.AccountActionView(_players(30), interaction.guild.id, interaction)

    option_values = {o.value for o in view.select.options}
    assert "link_new" not in option_values
    assert len(view.select.options) == 25  # full page budget, no slot lost to "link new"

    link_buttons = [
        item for item in view.children
        if getattr(item, "custom_id", None) == "account_action_link_new"
    ]
    assert len(link_buttons) == 1


@pytest.mark.discord
@pytest.mark.asyncio
async def test_action_view_select_paginates_unverified_players(monkeypatch: pytest.MonkeyPatch) -> None:
    import qapbot.ui_registration as ui

    monkeypatch.setattr(ui, "t", identity_t)
    interaction = make_interaction()

    view = ui.AccountActionView(_players(30), interaction.guild.id, interaction)

    assert view.next_button is not None and view.prev_button is not None
    assert view.prev_button.disabled is True

    await view._on_page_next(interaction)

    assert view.current_page == 1
    assert len(view.select.options) == 5  # 30 - 25
    assert view.next_button.disabled is True


# ---------------------------------------------------------------------------
# Tracker #0056: re-entrancy guard. Reported crash ("Cannot read property 'label' of
# undefined" on the reporter's Discord client) happened on the reporter's FIRST invocation of
# this view right after the #45 pagination fix deployed, with a retry immediately afterward
# working fine — consistent with a rapid second click racing a first click's still-in-flight
# message edit (this view has multiple new same-message-editing handlers post-#45: page-turn,
# selection, and especially Refresh, which does a real CoC API fetch before its own edit).
# AccountManagementView._busy now guards every state-mutating handler against exactly this.
# ---------------------------------------------------------------------------

@pytest.mark.discord
@pytest.mark.asyncio
async def test_refresh_click_guards_against_reentrant_second_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second click (here: Next page) landing while Refresh's CoC API fetch is still in
    flight must be dropped, not run a second overlapping edit against the same message."""
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {"players": _players(5)}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    release = asyncio.Event()
    fetch_started = asyncio.Event()

    async def _slow_get_player(tag, force_fresh=False):
        fetch_started.set()
        await release.wait()
        return None

    cache.get_player = AsyncMock(side_effect=_slow_get_player)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")
    refresh_interaction = make_interaction(user_id=123)
    page_interaction = make_interaction(user_id=123)

    task1 = asyncio.create_task(view._on_refresh_click(refresh_interaction))
    await asyncio.wait_for(fetch_started.wait(), timeout=5)
    assert view._busy is True  # Refresh is now blocked mid-fetch, holding the guard

    original_page = view.current_page
    await view._on_page_next(page_interaction)

    # Dropped: current_page unchanged, and the second interaction only got a bare defer, never
    # reached edit_message (which would have raced Refresh's own eventual response).
    assert view.current_page == original_page
    page_interaction.response.defer.assert_awaited()
    page_interaction.response.edit_message.assert_not_called()

    release.set()
    await task1
    assert view._busy is False  # guard released once Refresh actually finishes


@pytest.mark.discord
@pytest.mark.asyncio
async def test_page_next_guards_against_reentrant_second_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guard, exercised on the page-turn handler itself (the button tracker #0056's
    reporter — ~60 accounts, 3 pages — had every reason to click quickly through)."""
    import qapbot.ui_registration as ui

    cache = FakeCache()
    cache.user_accounts["123"] = {"players": _players(60)}
    monkeypatch.setattr(ui, "CACHE", cache)
    monkeypatch.setattr(ui, "t", identity_t)

    view = ui.AccountManagementView(user_id="123", guild_id=1, display_name="Alice")

    # Simulate a first click still "in flight" by holding the guard directly, the same state
    # _guard_reentrant() would leave mid-handler between its own two awaits.
    assert view._guard_reentrant() is True

    interaction = make_interaction(user_id=123)
    original_page = view.current_page
    await view._on_page_next(interaction)

    assert view.current_page == original_page  # dropped
    interaction.response.defer.assert_awaited()
    interaction.response.edit_message.assert_not_called()
