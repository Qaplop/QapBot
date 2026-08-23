"""Tests for the bug/feature tracker's item lifecycle (BUG_FEATURE_TRACKER_PLAN.md Phases
3-5): attachment sanitizing, embed rendering, the modal/draft flow, status transitions, the
test-case sign-off loop (including the 👍-reaction shortcut), and the upload-window mechanism.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.db_manager import WarHistoryDB
from qapbot.ui_tracker import (
    PRIORITY_VALUES,
    TRACKER_SETTING_BUG_CHANNEL,
    TRACKER_SETTING_DONE_TESTING_CHANNEL,
    TRACKER_SETTING_ENABLED,
    TRACKER_SETTING_IMPLEMENTED_CHANNEL,
    TRACKER_SETTING_TEST_CHANNEL,
    ConfirmForceMoveView,
    ConfirmItemDoneView,
    TrackerDraftView,
    TrackerItemButton,
    TrackerItemModal,
    TrackerStatusSelectView,
    TrackerTestPassButton,
    _sanitize_attachment_filename,
    apply_pending_requestor_access,
    apply_status_change,
    build_tracker_embed,
    create_tracker_item_for_agent,
    finalize_testcases_move,
    get_thread_messages,
    handle_tracker_test_reaction,
    handle_tracker_upload_message,
    mark_environment_passed_and_refresh,
    mark_testing_failed,
    post_comment,
    post_test_cases,
    start_tracker_item,
    _chunk_lines_for_discord,
    _register_upload_window,
    _upload_windows,
)

ADMIN_ID = "555000"


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "qapbot_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    try:
        yield manager
    finally:
        await manager.close()


@pytest.fixture(autouse=True)
def _wire_cache_db(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "tracker_settings", {})
    return db


@pytest.fixture(autouse=True)
def _configure_admin(monkeypatch):
    from qapbot.config import CONFIG
    import dataclasses
    # tracker_enabled=True: the ambient test CONFIG defaults to DEV-like settings (is_dev_mode
    # True, tracker_enabled False) absent real PROD env vars -- this whole file exercises tracker
    # behavior, so it needs the same override test_cwl_signup_snapshot_staleness.py's _allow_dms
    # fixture makes for cwl_dm_restrict_to_admin. Needed since 2026-08-22's live bug report fix:
    # handle_tracker_test_reaction() now gates on tracker_enabled (see its own docstring).
    monkeypatch.setattr(
        "qapbot.config.CONFIG", dataclasses.replace(CONFIG, server_admin=ADMIN_ID, tracker_enabled=True)
    )
    # ui_tracker imports CONFIG inside its functions (`from qapbot.config import CONFIG`),
    # which re-reads the module attribute each call, so the patch above is picked up live.


def _fake_message(message_id=999):
    message = AsyncMock()
    message.id = message_id
    message.jump_url = f"https://discord.com/channels/1/2/{message_id}"
    message.edit = AsyncMock()
    message.create_thread = AsyncMock(return_value=AsyncMock(id=777, send=AsyncMock()))
    return message


def _fake_channel(send_message=None, fetch_message=None):
    channel = AsyncMock()
    channel.send = AsyncMock(return_value=send_message or _fake_message())
    channel.fetch_message = AsyncMock(return_value=fetch_message)
    return channel


def _wire_bot(monkeypatch, channel=None, user=None):
    import QBcore
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=user)
    monkeypatch.setattr(QBcore, "bot", bot)
    return bot


def _wire_bot_multi(monkeypatch, channels_by_id, user=None):
    """Like _wire_bot, but resolves a different channel object per channel id — needed to
    exercise a move (source channel != destination channel)."""
    import QBcore
    bot = MagicMock()
    bot.get_channel = MagicMock(side_effect=lambda cid: channels_by_id.get(int(cid)))
    async def _fetch_channel(cid):
        return channels_by_id.get(int(cid))
    bot.fetch_channel = AsyncMock(side_effect=_fetch_channel)
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=user)
    monkeypatch.setattr(QBcore, "bot", bot)
    return bot


async def _make_item(db, item_type="bug", **overrides):
    kwargs = dict(item_type=item_type, title="t", description="d", reporter_id="111", reporter_name="A")
    kwargs.update(overrides)
    item_number = await db.create_tracker_item(**kwargs)
    return item_number


# -- create_tracker_item_for_agent (tracker item #0015) --------------------

async def test_create_tracker_item_for_agent_persists_and_posts(monkeypatch, db):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_BUG_CHANNEL] = "1"
    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)

    result = await create_tracker_item_for_agent(
        item_type="bug", title="Odd fetch", description="desc", reporter_name="claude"
    )

    assert result["item"]["title"] == "Odd fetch"
    assert result["item"]["reporter_id"] == "agent:claude"
    assert result["item"]["reporter_name"] == "claude"
    assert result["jump_url"]
    channel.send.assert_awaited_once()


async def test_create_tracker_item_for_agent_feature_ignores_environment(monkeypatch, db):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_BUG_CHANNEL] = "1"
    _wire_bot(monkeypatch, channel=_fake_channel())

    result = await create_tracker_item_for_agent(
        item_type="feature", title="T", description="D", environment="PROD"
    )
    assert result["item"]["environment"] is None


async def test_create_tracker_item_for_agent_rejects_bad_item_type(monkeypatch, db):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_BUG_CHANNEL] = "1"
    with pytest.raises(ValueError):
        await create_tracker_item_for_agent(item_type="epic", title="T", description="D")


async def test_create_tracker_item_for_agent_requires_configured_channel(monkeypatch, db):
    with pytest.raises(ValueError):
        await create_tracker_item_for_agent(item_type="bug", title="T", description="D")


async def test_create_tracker_item_for_agent_respects_disabled_flag(monkeypatch, db):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_BUG_CHANNEL] = "1"
    CACHE.tracker_settings[TRACKER_SETTING_ENABLED] = "0"
    with pytest.raises(ValueError):
        await create_tracker_item_for_agent(item_type="bug", title="T", description="D")


async def test_create_tracker_item_for_agent_caps_overlong_title(monkeypatch, db):
    """tracker item #0037: title has no client-side cap on this (non-Discord-modal) path, so an
    over-length title must be truncated at creation -- otherwise the item becomes permanently
    un-editable (opening Edit fails Discord's own max_length validation on the modal's default)."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_TITLE_MAX_LENGTH
    CACHE.tracker_settings[TRACKER_SETTING_BUG_CHANNEL] = "1"
    _wire_bot(monkeypatch, channel=_fake_channel())

    result = await create_tracker_item_for_agent(
        item_type="bug", title="x" * (TRACKER_TITLE_MAX_LENGTH + 20), description="D"
    )

    assert len(result["item"]["title"]) <= TRACKER_TITLE_MAX_LENGTH
    assert result["item"]["title"].endswith("…")


# -- attachment filename sanitizing -----------------------------------------

def test_sanitize_filename_strips_path_traversal():
    result = _sanitize_attachment_filename("../../etc/passwd", 1)
    assert "/" not in result and "\\" not in result and ".." not in result


def test_sanitize_filename_pure_dots_falls_back_to_file():
    assert _sanitize_attachment_filename("..", 2) == "2_file"
    assert _sanitize_attachment_filename("...", 3) == "3_file"


def test_sanitize_filename_truncates_long_names():
    name = "a" * 200 + ".png"
    result = _sanitize_attachment_filename(name, 1)
    assert len(result) <= 70
    assert result.startswith("1_")


def test_sanitize_filename_unicode_is_replaced():
    result = _sanitize_attachment_filename("スクリーンショット.png", 4)
    assert result.startswith("4_")
    assert all(c.isascii() for c in result)


# -- embed rendering ----------------------------------------------------

@pytest.mark.asyncio
async def test_build_tracker_embed_includes_status_and_environment(db):
    item_number = await _make_item(db, item_type="bug", environment="PROD")
    item = await db.get_tracker_item(item_number)
    embed = build_tracker_embed(item)
    assert f"#{item_number:04d}" in embed.title
    assert "Open" in embed.title
    assert "PROD" in embed.description


@pytest.mark.asyncio
async def test_build_tracker_embed_includes_priority(db):
    item_number = await _make_item(db, item_type="bug", priority="HIGH")
    item = await db.get_tracker_item(item_number)
    embed = build_tracker_embed(item)
    assert "🔴" in embed.title
    assert "High" in embed.description


@pytest.mark.asyncio
async def test_build_tracker_embed_stays_english_regardless_of_guild_language(db, monkeypatch):
    """The tracker is a developer/triage tool, not end-user-facing -- its text must read the
    same for everyone, so it must ignore the viewing guild's/reporter's configured language
    (previously status labels like "Implemented"/"Umgesetzt" showed inconsistently depending
    on which guild's language setting happened to apply -- confusing for whoever's triaging)."""
    from qapbot.cache_manager import CACHE

    item_number = await _make_item(db, item_type="bug", guild_id="987654321")
    await db.update_tracker_item(item_number, status="implemented")
    item = await db.get_tracker_item(item_number)
    monkeypatch.setattr(CACHE, "server_config", {"987654321": {"language": "de"}})

    embed = build_tracker_embed(item)

    assert "Implemented" in embed.title
    assert "Umgesetzt" not in embed.title
    assert "Gemeldet" not in embed.description  # would be the German "reported_by" phrasing


@pytest.mark.asyncio
async def test_build_tracker_embed_defaults_priority_when_missing(db):
    """A pre-migration row (or a caller that never set priority) falls back to MEDIUM rather
    than raising/blank."""
    item_number = await _make_item(db, item_type="bug")
    item = await db.get_tracker_item(item_number)
    item = dict(item)
    item["priority"] = None
    embed = build_tracker_embed(item)
    assert "🟡" in embed.title


@pytest.mark.asyncio
async def test_build_tracker_embed_truncates_long_description(db):
    long_desc = "x" * 5000
    item_number = await _make_item(db, description=long_desc)
    item = await db.get_tracker_item(item_number)
    embed = build_tracker_embed(item)
    assert len(embed.description) < 2000
    assert "full text in thread" in embed.description


# -- TrackerItemModal (type-aware fields) ------------------------------------

def test_modal_removes_environment_field_for_feature():
    modal = TrackerItemModal("feature", guild_id=None, user_id="1")
    assert modal.environment_select not in modal.children


def test_modal_keeps_environment_field_for_bug():
    modal = TrackerItemModal("bug", guild_id=None, user_id="1")
    assert modal.environment_select in modal.children


def test_modal_title_differs_by_type():
    bug_modal = TrackerItemModal("bug", guild_id=None, user_id="1")
    feature_modal = TrackerItemModal("feature", guild_id=None, user_id="1")
    assert bug_modal.title != feature_modal.title


def test_modal_clamps_overlong_initial_title_on_edit():
    """tracker item #0037: an item created before the creation-time cap existed (or via any
    other path that skipped it) can still have a >100-char title stored. Opening Edit on it must
    not construct a modal whose title field default itself exceeds max_length -- Discord rejects
    that with a 400 before the modal is even shown, permanently blocking Edit on that item."""
    from qapbot.ui_tracker import TRACKER_TITLE_MAX_LENGTH
    overlong = "x" * (TRACKER_TITLE_MAX_LENGTH + 20)

    modal = TrackerItemModal(
        "bug", guild_id=None, user_id="1", item_number=42, initial_title=overlong,
        initial_description="D",
    )

    assert len(modal.title_input.default) <= TRACKER_TITLE_MAX_LENGTH
    assert modal.initial_title == modal.title_input.default  # on_submit's change-check stays consistent


def test_modal_environment_and_priority_are_radio_groups_not_dropdowns():
    """Discord.py's plain `.Select` renders as a dropdown; `.RadioGroup` renders as actual
    radio buttons — the project owner explicitly asked for radio buttons here."""
    modal = TrackerItemModal("bug", guild_id=None, user_id="1")
    assert isinstance(modal.environment_select.component, discord.ui.RadioGroup)
    assert isinstance(modal.priority_select.component, discord.ui.RadioGroup)


def test_modal_keeps_priority_field_for_both_bug_and_feature():
    bug_modal = TrackerItemModal("bug", guild_id=None, user_id="1")
    feature_modal = TrackerItemModal("feature", guild_id=None, user_id="1")
    assert bug_modal.priority_select in bug_modal.children
    assert feature_modal.priority_select in feature_modal.children


def test_modal_priority_defaults_to_medium():
    modal = TrackerItemModal("bug", guild_id=None, user_id="1")
    options = modal.priority_select.component.options
    default_values = [o.value for o in options if o.default]
    assert default_values == ["MEDIUM"]
    assert {o.value for o in options} == set(PRIORITY_VALUES)


def test_modal_priority_honors_initial_value_on_edit():
    modal = TrackerItemModal("bug", guild_id=None, user_id="1", initial_priority="HIGH")
    options = modal.priority_select.component.options
    default_values = [o.value for o in options if o.default]
    assert default_values == ["HIGH"]


def _edit_modal(item, new_title=None, new_description=None, new_details=None):
    """Build a TrackerItemModal for editing *item* and poke the submitted text field values
    directly. discord.py's TextInput has no public setter for `.value` -- only `.default`,
    which drives the modal UI's pre-filled text, not what on_submit() reads back -- so a
    programmatic "user typed this and hit submit" simulation has to set the private `_value`
    discord.py itself populates from the real Discord submission payload."""
    modal = TrackerItemModal(
        item["item_type"], guild_id=None, user_id="1", item_number=item["item_number"],
        initial_title=item["title"], initial_description=item["description"],
        initial_details=item.get("details") or "",
    )
    modal.title_input._value = new_title if new_title is not None else item["title"]
    modal.description_input._value = new_description if new_description is not None else item["description"]
    modal.details_input._value = new_details if new_details is not None else (item.get("details") or "")
    return modal


# -- edit reposts full text to thread when it changed (tracker item #0029) --

@pytest.mark.asyncio
async def test_edit_reposts_full_text_to_thread_when_description_changed(db, monkeypatch, mock_interaction):
    thread = AsyncMock()
    _wire_bot(monkeypatch, channel=thread)

    item_number = await _make_item(db, title="Old title", description="Old description")
    await db.update_tracker_item(item_number, thread_id="777")
    item = await db.get_tracker_item(item_number)

    modal = _edit_modal(item, new_description="New, corrected description")
    await modal.on_submit(mock_interaction)

    thread.send.assert_awaited()
    sent_texts = [call.args[0] for call in thread.send.await_args_list]
    assert any("Edited" in text or "Bearbeitet" in text for text in sent_texts)
    assert any("New, corrected description" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_edit_does_not_repost_thread_when_text_is_unchanged(db, monkeypatch, mock_interaction):
    """A priority-/environment-only edit (title/description/details untouched) must not spam
    the thread with a redundant "full text" copy."""
    thread = AsyncMock()
    _wire_bot(monkeypatch, channel=thread)

    item_number = await _make_item(db, title="Same title", description="Same description")
    await db.update_tracker_item(item_number, thread_id="777")
    item = await db.get_tracker_item(item_number)

    modal = _edit_modal(item)  # no new_* args -> resubmits the same text unchanged
    await modal.on_submit(mock_interaction)

    thread.send.assert_not_awaited()


# -- TrackerDraftView preview ---------------------------------------------

def test_draft_preview_lists_pending_attachments():
    draft = TrackerDraftView(
        item_type="bug", title="T", description="D", details="", environment="",
        reporter_id="1", reporter_name="A", guild_id=None, channel_id=1, user_id="1",
        pending_attachments=[{"original_name": "shot.png"}],
    )
    assert "shot.png" in draft.format_preview()


def test_draft_preview_no_attachments_shows_none():
    draft = TrackerDraftView(
        item_type="bug", title="T", description="D", details="", environment="",
        reporter_id="1", reporter_name="A", guild_id=None, channel_id=1, user_id="1",
    )
    assert "none" in draft.format_preview().lower()


def test_draft_preview_shows_priority():
    draft = TrackerDraftView(
        item_type="bug", title="T", description="D", details="", environment="",
        reporter_id="1", reporter_name="A", guild_id=None, channel_id=1, user_id="1",
        priority="HIGH",
    )
    assert "High" in draft.format_preview()


def test_draft_preview_defaults_priority_to_medium_when_unset():
    draft = TrackerDraftView(
        item_type="bug", title="T", description="D", details="", environment="",
        reporter_id="1", reporter_name="A", guild_id=None, channel_id=1, user_id="1",
    )
    assert draft.priority_text == "MEDIUM"


# -- submit posts to the shared bug/feature channel (tracker item #0006) -----

@pytest.mark.asyncio
@pytest.mark.parametrize("item_type", ["bug", "feature"])
async def test_on_submit_posts_to_the_shared_bug_channel(db, monkeypatch, mock_interaction, item_type):
    """Both /bug and /feature must resolve to TRACKER_SETTING_BUG_CHANNEL now that the separate
    feature channel setting is retired — only one channel is ever configured any more."""
    from qapbot.cache_manager import CACHE

    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)
    monkeypatch.setattr(CACHE, "tracker_settings", {TRACKER_SETTING_BUG_CHANNEL: "42"})

    draft = TrackerDraftView(
        item_type=item_type, title="T", description="D", details="", environment="",
        reporter_id="111", reporter_name="A", guild_id=None, channel_id=1, user_id="1",
    )
    draft.message = AsyncMock()

    await draft._on_submit(mock_interaction)

    assert draft.submitted is True
    channel.send.assert_awaited_once()


# -- double-click submit guard (tracker item #0026) -------------------------

@pytest.mark.asyncio
async def test_on_submit_second_click_does_not_create_a_second_item(db, monkeypatch, mock_interaction):
    """A user double-clicking Submit before the first click's response lands (Discord hadn't
    yet made the button vanish) must not create a second tracker item for the same report."""
    from qapbot.cache_manager import CACHE

    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)
    monkeypatch.setattr(CACHE, "tracker_settings", {TRACKER_SETTING_BUG_CHANNEL: "42"})

    draft = TrackerDraftView(
        item_type="bug", title="T", description="D", details="", environment="",
        reporter_id="111", reporter_name="A", guild_id=None, channel_id=1, user_id="1",
    )
    draft.message = AsyncMock()

    await draft._on_submit(mock_interaction)
    await draft._on_submit(mock_interaction)  # simulated re-click

    channel.send.assert_awaited_once()  # only one tracker item ever posted
    mock_interaction.response.defer.assert_awaited_once()  # the re-click's early-return path


@pytest.mark.asyncio
async def test_on_submit_disables_buttons_via_edit_message_response(db, monkeypatch, mock_interaction):
    """Buttons must vanish as fast as possible: edit_message() as the interaction response
    itself (not a separate defer-then-edit round trip), with every button disabled."""
    from qapbot.cache_manager import CACHE

    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)
    monkeypatch.setattr(CACHE, "tracker_settings", {TRACKER_SETTING_BUG_CHANNEL: "42"})

    draft = TrackerDraftView(
        item_type="bug", title="T", description="D", details="", environment="",
        reporter_id="111", reporter_name="A", guild_id=None, channel_id=1, user_id="1",
    )
    draft.message = AsyncMock()

    await draft._on_submit(mock_interaction)

    mock_interaction.response.edit_message.assert_awaited_once_with(view=draft)
    assert draft.children and all(child.disabled for child in draft.children)  # type: ignore[attr-defined]


# -- no cap on open items (removed 2026-08-22 per project owner request) ----

@pytest.mark.asyncio
async def test_start_tracker_item_opens_modal_regardless_of_existing_open_item_count(db, monkeypatch, mock_interaction):
    from qapbot.cache_manager import CACHE
    monkeypatch.setattr(CACHE, "tracker_settings", {TRACKER_SETTING_BUG_CHANNEL: "42"})

    reporter_id = str(mock_interaction.user.id)
    for _ in range(25):  # far past the old MAX_OPEN_ITEMS_PER_REPORTER=10 cap
        await _make_item(db, item_type="bug", reporter_id=reporter_id)

    await start_tracker_item(mock_interaction, "bug")

    mock_interaction.response.send_modal.assert_awaited_once()
    mock_interaction.response.send_message.assert_not_awaited()


# -- status transitions -------------------------------------------------

@pytest.mark.asyncio
async def test_apply_status_change_updates_db(db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    updated = await apply_status_change(item_number, "triaged", actor_id="1")
    assert updated["status"] == "triaged"


@pytest.mark.asyncio
async def test_status_select_edits_dropdown_message_in_place(db, monkeypatch, mock_interaction):
    """The status dropdown is a single-use picker (like ConfirmItemDoneView/ConfirmForceMoveView)
    -- picking a value must replace its own message via edit_original_response(), not leave the
    stale dropdown behind while a separate confirmation message is sent (message-clutter Pitfall
    2, qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md; 2026-08-23 fix)."""
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    view = TrackerStatusSelectView(item_number, "new", str(mock_interaction.user.id), mock_interaction.guild.id)
    mock_interaction.data = {"values": ["triaged"]}

    await view._on_select(mock_interaction)

    mock_interaction.edit_original_response.assert_awaited_once()
    assert mock_interaction.edit_original_response.call_args.kwargs["view"] is None
    mock_interaction.followup.send.assert_not_awaited()
    item = await db.get_tracker_item(item_number)
    assert item["status"] == "triaged"


@pytest.mark.asyncio
async def test_confirm_item_done_yes_edits_message_in_place(db, monkeypatch, mock_interaction):
    """Same message-clutter fix as the status dropdown: clicking Yes must replace the Yes/No
    prompt itself, not leave it behind alongside a separate confirmation message."""
    _wire_bot(monkeypatch, channel=None)
    mock_interaction.user.id = int(ADMIN_ID)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="testing")
    view = ConfirmItemDoneView(item_number, mock_interaction.guild.id)

    await view._on_yes(mock_interaction)

    mock_interaction.edit_original_response.assert_awaited_once()
    assert mock_interaction.edit_original_response.call_args.kwargs["view"] is None
    mock_interaction.followup.send.assert_not_awaited()
    item = await db.get_tracker_item(item_number)
    assert item["status"] == "done"


@pytest.mark.asyncio
async def test_confirm_item_done_no_edits_message_in_place(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="testing")
    view = ConfirmItemDoneView(item_number, mock_interaction.guild.id)

    await view._on_no(mock_interaction)

    mock_interaction.edit_original_response.assert_awaited_once()
    assert mock_interaction.edit_original_response.call_args.kwargs["view"] is None
    mock_interaction.followup.send.assert_not_awaited()
    item = await db.get_tracker_item(item_number)
    assert item["status"] == "testing"  # untouched


@pytest.mark.asyncio
async def test_confirm_force_move_yes_edits_message_in_place(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None)
    mock_interaction.user.id = int(ADMIN_ID)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="testing")
    view = ConfirmForceMoveView(item_number, mock_interaction.guild.id)

    await view._on_yes(mock_interaction)

    mock_interaction.edit_original_response.assert_awaited_once()
    mock_interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_force_move_no_edits_message_in_place(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="testing")
    view = ConfirmForceMoveView(item_number, mock_interaction.guild.id)

    await view._on_no(mock_interaction)

    mock_interaction.edit_original_response.assert_awaited_once()
    assert mock_interaction.edit_original_response.call_args.kwargs["view"] is None
    mock_interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_item_done_yes_second_click_does_not_resend_dm(db, monkeypatch, mock_interaction):
    """tracker item #0036: a rapid double-click on "Yes, mark done" sent the reporter five
    identical "now done!" DMs -- apply_status_change() isn't idempotent (it unconditionally
    re-DMs on every call), so the view itself must refuse to run the action twice."""
    fake_user = AsyncMock()
    _wire_bot(monkeypatch, channel=None, user=fake_user)
    mock_interaction.user.id = int(ADMIN_ID)
    item_number = await _make_item(db, reporter_id="222")
    await db.update_tracker_item(item_number, status="testing")
    view = ConfirmItemDoneView(item_number, mock_interaction.guild.id)

    await view._on_yes(mock_interaction)
    await view._on_yes(mock_interaction)  # simulated re-click

    fake_user.send.assert_awaited_once()  # only one "now done!" DM, not two
    mock_interaction.edit_original_response.assert_awaited_once()  # second click never re-ran the handler


@pytest.mark.asyncio
async def test_confirm_item_done_first_click_disables_and_edits_message(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None)
    mock_interaction.user.id = int(ADMIN_ID)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="testing")
    view = ConfirmItemDoneView(item_number, mock_interaction.guild.id)

    await view._on_yes(mock_interaction)

    mock_interaction.response.edit_message.assert_awaited_once_with(view=view)
    assert view.children and all(child.disabled for child in view.children)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_confirm_force_move_yes_second_click_does_not_rerun(db, monkeypatch, mock_interaction):
    """Same double-click class as above, on the "Move to Done anyway?" confirm."""
    _wire_bot(monkeypatch, channel=None)
    mock_interaction.user.id = int(ADMIN_ID)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="testing")
    view = ConfirmForceMoveView(item_number, mock_interaction.guild.id)

    await view._on_yes(mock_interaction)
    await view._on_yes(mock_interaction)  # simulated re-click

    mock_interaction.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_select_second_selection_does_not_reapply(db, monkeypatch, mock_interaction):
    """Same double-click class, on the admin status dropdown -- a second selection landing
    before the first is visibly done must not re-run apply_status_change() (which would re-send
    a reporter DM for any status in DM_NOTIFY_STATUSES)."""
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    view = TrackerStatusSelectView(item_number, "new", str(mock_interaction.user.id), mock_interaction.guild.id)
    mock_interaction.data = {"values": ["triaged"]}

    await view._on_select(mock_interaction)
    await view._on_select(mock_interaction)  # simulated re-click

    mock_interaction.edit_original_response.assert_awaited_once()
    item = await db.get_tracker_item(item_number)
    assert item["status"] == "triaged"


@pytest.mark.asyncio
async def test_grant_access_second_click_does_not_resend_invite(db, monkeypatch, mock_interaction):
    """tracker item #0036 follow-up: Grant Access is a persistent DynamicItem button (a fresh
    Python object per click, no shared view state), so its double-click guard has to be a
    persisted-state check instead -- a second click must not create a second one-time invite
    or send the reporter a second DM once access_grant_pending is already set."""
    fake_user = AsyncMock()
    _wire_bot(monkeypatch, channel=None, user=fake_user)
    item_number = await _make_item(db, reporter_id="222")
    mock_interaction.user.id = int(ADMIN_ID)
    mock_interaction.guild.get_member = MagicMock(return_value=None)
    mock_interaction.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    mock_interaction.channel.create_invite = AsyncMock(return_value=MagicMock(url="https://discord.gg/abc123"))

    button = TrackerItemButton("grantaccess", item_number)
    item = await db.get_tracker_item(item_number)
    await button._handle_grant_access(mock_interaction, item)  # first click: creates the invite

    item_after_first_click = await db.get_tracker_item(item_number)  # a second click re-fetches
    await button._handle_grant_access(mock_interaction, item_after_first_click)  # simulated re-click

    mock_interaction.channel.create_invite.assert_awaited_once()
    fake_user.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_status_change_dms_reporter_on_implemented(db, monkeypatch):
    fake_user = AsyncMock()
    _wire_bot(monkeypatch, channel=None, user=fake_user)
    item_number = await _make_item(db, reporter_id="222")
    await apply_status_change(item_number, "implemented", note="fixed in commit abc", actor_id="1")
    fake_user.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_status_change_does_not_dm_on_triaged(db, monkeypatch):
    fake_user = AsyncMock()
    _wire_bot(monkeypatch, channel=None, user=fake_user)
    item_number = await _make_item(db, reporter_id="222")
    await apply_status_change(item_number, "triaged", actor_id="1")
    fake_user.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_status_change_skips_dm_for_agent_reporter_without_warning(db, monkeypatch, caplog):
    """2026-08-23 live bug report: agent-filed items use a non-numeric reporter_id
    ("agent:<label>") by design (create_tracker_item_for_agent) -- a status change that would
    normally DM the reporter must silently no-op instead of trying int() on it and logging a
    misleading "Failed to DM reporter" warning."""
    fake_user = AsyncMock()
    bot = _wire_bot(monkeypatch, channel=None, user=fake_user)
    item_number = await _make_item(db, reporter_id="agent:claude")
    with caplog.at_level("WARNING"):
        await apply_status_change(item_number, "implemented", note="fixed in commit abc", actor_id="1")
    fake_user.send.assert_not_awaited()
    bot.fetch_user.assert_not_awaited()
    assert not any("Failed to DM reporter" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_apply_status_change_sets_implemented_note_and_timestamp(db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    updated = await apply_status_change(item_number, "implemented", note="see PR #42", actor_id="1")
    assert updated["implemented_note"] == "see PR #42"
    assert updated["implemented_at"] is not None


@pytest.mark.asyncio
async def test_apply_status_change_refreshes_posted_message(db, monkeypatch):
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_item_message(item_number, channel_id="1", message_id="999")
    await apply_status_change(item_number, "triaged", actor_id="1")
    message.edit.assert_awaited_once()


# -- move-on-done (Implemented / Done Testing channels) ----------------------

@pytest.mark.asyncio
async def test_apply_status_change_done_moves_item_only_not_test_message(db, monkeypatch):
    """Decoupled (tracker item #0015 follow-up, 2026-08-22): the item's own `done` transition
    must never touch the test-case message — that only moves on its own trigger
    (move_testcases_to_done_channel / finalize_testcases_move)."""
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_IMPLEMENTED_CHANNEL] = "50"
    CACHE.tracker_settings[TRACKER_SETTING_DONE_TESTING_CHANNEL] = "60"

    old_item_message = _fake_message(message_id=100)
    old_reports_channel = _fake_channel(fetch_message=old_item_message)
    new_item_message = _fake_message(message_id=555)
    implemented_channel = _fake_channel(send_message=new_item_message)
    implemented_channel.id = 50

    old_test_channel = _fake_channel()
    done_testing_channel = _fake_channel()
    done_testing_channel.id = 60

    _wire_bot_multi(monkeypatch, {
        10: old_reports_channel, 50: implemented_channel,
        20: old_test_channel, 60: done_testing_channel,
    })

    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.update_tracker_item(
        item_number, channel_id="10", message_id="100", thread_id="777",
        test_channel_id="20", test_message_id="200", status="testing",
    )

    updated = await apply_status_change(item_number, "done", actor_id="1")

    # Item embed moved to Implemented, buttons stripped (nothing left to do on a closed item).
    implemented_channel.send.assert_awaited_once()
    _, item_send_kwargs = implemented_channel.send.call_args
    assert "view" not in item_send_kwargs or item_send_kwargs["view"] is None
    old_item_message.delete.assert_awaited_once()
    assert updated["channel_id"] == "50"
    assert updated["message_id"] == "555"
    assert updated["thread_id"] == "777"  # never wiped by the move

    # Test-case message untouched — no automatic linkage any more.
    done_testing_channel.send.assert_not_awaited()
    assert updated["test_channel_id"] == "20"
    assert updated["test_message_id"] == "200"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["rejected", "duplicate"])
async def test_apply_status_change_rejected_and_duplicate_also_move_to_implemented_channel(
    db, monkeypatch, terminal_status,
):
    """Spec gap found live (2026-08-22, tracker #0010's own closure): rejected/duplicate are
    just as terminal as done, but apply_status_change() only moved `done` items to the
    Implemented channel — rejected/duplicate fell into the plain in-place-refresh branch and
    lingered in the working reports channel forever instead of being archived like everything
    else that's finished."""
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_IMPLEMENTED_CHANNEL] = "50"

    old_item_message = _fake_message(message_id=100)
    old_reports_channel = _fake_channel(fetch_message=old_item_message)
    new_item_message = _fake_message(message_id=555)
    implemented_channel = _fake_channel(send_message=new_item_message)
    implemented_channel.id = 50
    _wire_bot_multi(monkeypatch, {10: old_reports_channel, 50: implemented_channel})

    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, channel_id="10", message_id="100", status="open")

    updated = await apply_status_change(item_number, terminal_status, actor_id="1")

    implemented_channel.send.assert_awaited_once()
    _, item_send_kwargs = implemented_channel.send.call_args
    assert "view" not in item_send_kwargs or item_send_kwargs["view"] is None
    old_item_message.delete.assert_awaited_once()
    assert updated["channel_id"] == "50"
    assert updated["message_id"] == "555"
    assert updated["status"] == terminal_status


async def test_finalize_testcases_move_moves_test_message_independent_of_item_status(db, monkeypatch):
    """The other half of the decoupling: moving the test-case message must never touch the
    item's own status/channel fields."""
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_DONE_TESTING_CHANNEL] = "60"

    old_test_message = _fake_message(message_id=200)
    old_test_channel = _fake_channel(fetch_message=old_test_message)
    new_test_message = _fake_message(message_id=600)
    done_testing_channel = _fake_channel(send_message=new_test_message)
    done_testing_channel.id = 60
    _wire_bot_multi(monkeypatch, {20: old_test_channel, 60: done_testing_channel})

    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.mark_tracker_environment_passed(item_number, "DEV", "1")
    await db.update_tracker_item(
        item_number, test_channel_id="20", test_message_id="200", status="testing",
    )

    result = await finalize_testcases_move(item_number)

    assert result["moved"] is True
    done_testing_channel.send.assert_awaited_once()
    old_test_message.delete.assert_awaited_once()
    item = await db.get_tracker_item(item_number)
    assert item["status"] == "testing"  # unaffected
    assert item["test_channel_id"] == "60"
    assert item["test_message_id"] == "600"
    assert result["linked_item"]["item_number"] == item_number  # still testing -> eligible


@pytest.mark.asyncio
async def test_apply_status_change_done_without_implemented_channel_falls_back_to_refresh(db, monkeypatch):
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_item_message(item_number, channel_id="10", message_id="100")

    updated = await apply_status_change(item_number, "done", actor_id="1")

    message.edit.assert_awaited_once()  # in-place refresh, unchanged from before this feature
    assert updated["channel_id"] == "10"
    assert updated["message_id"] == "100"


@pytest.mark.asyncio
async def test_apply_status_change_done_skips_move_when_already_in_implemented_channel(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_IMPLEMENTED_CHANNEL] = "50"
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot_multi(monkeypatch, {50: channel})
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, channel_id="50", message_id="100")

    await apply_status_change(item_number, "done", actor_id="1")

    channel.send.assert_not_awaited()  # no duplicate post
    message.edit.assert_awaited_once()  # still refreshed in place, buttons stay stripped
    _, edit_kwargs = message.edit.call_args
    assert edit_kwargs["view"] is None


@pytest.mark.asyncio
async def test_apply_status_change_done_without_test_message_skips_test_move_cleanly(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_DONE_TESTING_CHANNEL] = "60"
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)

    updated = await apply_status_change(item_number, "done", actor_id="1")  # must not raise

    assert updated["status"] == "done"
    assert updated["test_channel_id"] is None


# -- test-case posting + sign-off loop ---------------------------------------

@pytest.mark.asyncio
async def test_post_test_cases_transitions_to_testing(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db, item_type="bug")
    item = await post_test_cases(item_number, [{"environment": "DEV", "description": "run it"}], actor_id="1")
    assert item["status"] == "testing"
    assert item["test_message_id"] is not None
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_test_cases_renders_per_case_priority(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db, item_type="bug")
    await post_test_cases(
        item_number,
        [
            {"environment": "DEV", "description": "critical path", "priority": "HIGH"},
            {"environment": "DEV", "description": "no priority given"},
        ],
        actor_id="1",
    )
    content = channel.send.call_args[0][0]
    assert "🔴" in content  # HIGH
    assert "🟡" in content  # unspecified defaults to MEDIUM


@pytest.mark.asyncio
async def test_post_test_cases_does_not_downgrade_done(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="done")
    item = await post_test_cases(item_number, [{"environment": "DEV", "description": "x"}], actor_id="1")
    assert item["status"] == "done"


# -- message chunking (2026-08-23, tracker #0028) -----------------------------
# Live incident: post_test_cases() sent the ENTIRE formatted list as one channel.send() call
# with no length guard. A typical 8-case set overflowed Discord's 2000-char cap by ~2.4x;
# channel.send() raised discord.HTTPException, which propagated all the way up as an unhandled
# exception and surfaced to the MCP/bridge caller as a bare text/plain 500 -- even though
# set_tracker_testcases() (the DB write) had already committed successfully a few lines earlier.
# The caller saw a failure; the test cases were silently never actually posted to Discord at all.

def _long_case(n: int, filler_len: int = 220) -> Dict[str, str]:
    """One test case whose rendered line alone runs to roughly `filler_len` characters -- enough
    that a handful of them reliably overflows a single 2000-char Discord message, without
    resorting to an unrealistically huge string that would obscure what's being tested."""
    return {"environment": "DEV", "description": f"case {n} " + ("x" * filler_len)}


class TestChunkLinesForDiscord:
    """_chunk_lines_for_discord() in isolation -- the pure packing logic every message-sending
    path above it (post_test_cases, _refresh_testcase_message, the Done-channel move, and
    post_comment) relies on to never exceed Discord's cap."""

    def test_short_input_produces_one_chunk(self):
        assert _chunk_lines_for_discord(["a", "b", "c"]) == ["a\nb\nc"]

    def test_empty_input_produces_one_empty_chunk_not_zero(self):
        # Discord has no concept of "send zero messages" for content that must go out.
        assert _chunk_lines_for_discord([]) == [""]

    def test_no_chunk_ever_exceeds_the_limit(self):
        lines = [f"line {i} " + ("y" * 50) for i in range(200)]
        chunks = _chunk_lines_for_discord(lines, limit=500)
        assert all(len(c) <= 500 for c in chunks)
        assert len(chunks) > 1  # the input genuinely needed splitting

    def test_rejoining_all_chunks_reproduces_the_original_lines_exactly(self):
        """The defining correctness property: chunking must be lossless. Splitting each chunk
        back on "\\n" and concatenating must recover the exact original line list -- proving no
        line was ever split mid-content and none was dropped or duplicated."""
        lines = [f"**ENV{i}**" if i % 5 == 0 else f"case description number {i}" for i in range(80)]
        chunks = _chunk_lines_for_discord(lines, limit=200)
        recovered = "\n".join(chunks).split("\n")
        assert recovered == lines

    def test_a_single_line_never_lands_split_across_two_chunks(self):
        lines = ["short", "x" * 100, "short again"]
        chunks = _chunk_lines_for_discord(lines, limit=150)
        # Each ORIGINAL line must appear whole inside exactly one chunk.
        for line in lines:
            assert sum(1 for c in chunks if line in c) == 1

    def test_a_line_longer_than_the_limit_is_hard_sliced_rather_than_raising(self):
        """The last-resort fallback -- a single test-case description (or one comment paragraph
        with no newline) that alone exceeds 2000 characters must never crash the caller."""
        huge_line = "z" * 5000
        chunks = _chunk_lines_for_discord([huge_line], limit=2000)
        assert all(len(c) <= 2000 for c in chunks)
        assert "".join(chunks) == huge_line  # every byte preserved, just split

    def test_an_oversized_line_flushes_pending_content_first(self):
        """A short line queued before an oversized one must not get swept into the oversized
        line's hard-sliced chunks -- it belongs in its own, still-readable chunk."""
        chunks = _chunk_lines_for_discord(["short line", "z" * 3000], limit=2000)
        assert chunks[0] == "short line"
        assert "".join(chunks[1:]) == "z" * 3000


@pytest.mark.asyncio
async def test_post_test_cases_small_set_still_edits_in_place_on_repost(db, monkeypatch):
    """Regression guard for the common case: a test-case list that has always fit in one message
    must keep today's edit-in-place behavior (same message, no delete+repost) after the chunking
    refactor -- this is the path every existing tracker item with a short case list takes."""
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    message = _fake_message(message_id=42)
    # send_message: what the first, fresh post() returns. fetch_message: what the second call's
    # edit-in-place fetch_message() resolves to -- both must be the SAME object for the "edited
    # in place" assertion below to mean anything.
    channel = _fake_channel(send_message=message, fetch_message=message)
    channel.id = 1  # must resolve back to a real int -- the second call re-reads test_channel_id
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)

    first = await post_test_cases(item_number, [{"environment": "DEV", "description": "x"}], actor_id="1")
    channel.send.assert_awaited_once()
    channel.send.reset_mock()

    second = await post_test_cases(
        item_number, [{"environment": "DEV", "description": "x, updated"}], actor_id="1"
    )

    message.edit.assert_awaited_once()
    channel.send.assert_not_called()
    assert first["test_message_id"] == second["test_message_id"] == "42"
    assert second["test_overflow_message_ids"] is None


@pytest.mark.asyncio
async def test_post_test_cases_overflow_sends_multiple_messages_view_only_on_last(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    # Unique, freshly-minted message per send() call, how many ever turn out to be needed —
    # the exact chunk count is an internal formatting detail this test deliberately doesn't
    # hardcode; what it checks is the STRUCTURE (view only on the last one, ids tracked in order).
    sent_messages = []

    def _next_message(*_args, **_kwargs):
        msg = _fake_message(message_id=900 + len(sent_messages))
        sent_messages.append(msg)
        return msg

    channel = _fake_channel()
    channel.send = AsyncMock(side_effect=_next_message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)

    cases = [_long_case(i) for i in range(10)]  # comfortably overflows one 2000-char message
    item = await post_test_cases(item_number, cases, actor_id="1")

    assert channel.send.await_count >= 2  # genuinely needed more than one message
    assert channel.send.await_count == len(sent_messages)
    # Every call except the last must carry no interactive view at all.
    calls = channel.send.call_args_list
    for call in calls[:-1]:
        assert call.kwargs.get("view") is None
    assert calls[-1].kwargs.get("view") is not None

    # The LAST sent message is the tracked, reactable one; the rest are the overflow list, in order.
    assert item["test_message_id"] == str(sent_messages[-1].id)
    assert item["test_overflow_message_ids"] == ",".join(str(m.id) for m in sent_messages[:-1])


@pytest.mark.asyncio
async def test_post_test_cases_growing_past_one_message_deletes_the_old_single_message(db, monkeypatch):
    """Edit-in-place must be abandoned the moment a repost needs more than one message -- a
    freshly-created overflow message would otherwise land chronologically AFTER the old message
    being edited in place, breaking top-to-bottom reading order."""
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    old_message = _fake_message(message_id=200)
    channel = _fake_channel(send_message=old_message, fetch_message=old_message)
    channel.id = 1
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)

    first = await post_test_cases(item_number, [{"environment": "DEV", "description": "x"}], actor_id="1")
    channel.send.assert_awaited_once()  # nothing existed yet to edit
    assert first["test_message_id"] == "200"
    channel.send.reset_mock()

    new_messages = [_fake_message(message_id=301), _fake_message(message_id=302)]
    channel.send = AsyncMock(side_effect=new_messages)
    cases = [_long_case(i) for i in range(10)]
    item = await post_test_cases(item_number, cases, actor_id="1")

    old_message.delete.assert_awaited_once()
    assert channel.send.await_count == len(new_messages)
    assert item["test_message_id"] == "302"
    assert item["test_overflow_message_ids"] == "301"


@pytest.mark.asyncio
async def test_post_test_cases_shrinking_back_to_one_message_deletes_all_old_overflow(db, monkeypatch):
    """The inverse of the growth case: a case list that shrinks back under the limit must clean
    up EVERY previously-tracked message (primary and overflow), not just leave the overflow ones
    orphaned in the channel."""
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    channel = _fake_channel()
    channel.id = 1
    overflow_msg = _fake_message(message_id=301)
    primary_msg = _fake_message(message_id=302)
    channel.fetch_message = AsyncMock(side_effect=lambda mid: {301: overflow_msg, 302: primary_msg}[int(mid)])
    channel.send = AsyncMock(side_effect=[overflow_msg, primary_msg])
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await post_test_cases(item_number, [_long_case(i) for i in range(10)], actor_id="1")
    assert overflow_msg.delete.await_count == 0  # sanity: nothing deleted yet on first post

    fresh_message = _fake_message(message_id=999)
    channel.send = AsyncMock(return_value=fresh_message)
    item = await post_test_cases(item_number, [{"environment": "DEV", "description": "small now"}], actor_id="1")

    overflow_msg.delete.assert_awaited_once()
    primary_msg.delete.assert_awaited_once()
    channel.send.assert_awaited_once()  # fresh single message, not an edit
    assert item["test_message_id"] == "999"
    assert item["test_overflow_message_ids"] is None


@pytest.mark.asyncio
async def test_refresh_testcase_message_persists_new_ids_after_rechunk(db, monkeypatch):
    """_refresh_testcase_message() (called after every Pass/Fail click) must persist a changed
    test_message_id/test_overflow_message_ids when a re-chunk changes them -- otherwise the next
    refresh would keep trying to edit message ids that no longer exist."""
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    old_message = _fake_message(message_id=500)
    channel = _fake_channel(send_message=old_message, fetch_message=old_message)
    channel.id = 1
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await post_test_cases(item_number, [{"environment": "DEV", "description": "x"}], actor_id="1")

    # Grow the case list directly in the DB (simulating a second tracker_add_testcases call)
    # then trigger the same refresh mark_environment_passed_and_refresh() performs internally.
    await db.set_tracker_testcases(item_number, [_long_case(i) for i in range(10)])
    new_messages = [_fake_message(message_id=701), _fake_message(message_id=702)]
    channel.send = AsyncMock(side_effect=new_messages)

    await mark_environment_passed_and_refresh(item_number, "DEV", "1")

    old_message.delete.assert_awaited_once()
    item = await db.get_tracker_item(item_number)
    assert item["test_message_id"] == "702"
    assert item["test_overflow_message_ids"] == "701"


@pytest.mark.asyncio
async def test_finalize_testcases_move_moves_every_overflow_message(db, monkeypatch):
    """The Done-channel move must delete ALL previously-tracked messages from the SOURCE channel
    (not just the primary one) and post fresh chunks to the DESTINATION channel — it can't reuse
    the live-channel edit-in-place path since old and new messages live in different channels."""
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_DONE_TESTING_CHANNEL] = "60"

    old_overflow = _fake_message(message_id=301)
    old_primary = _fake_message(message_id=302)
    old_channel = AsyncMock()
    old_channel.fetch_message = AsyncMock(
        side_effect=lambda mid: {301: old_overflow, 302: old_primary}[int(mid)]
    )
    new_messages = [_fake_message(message_id=701), _fake_message(message_id=702)]
    done_channel = AsyncMock()
    done_channel.id = 60
    done_channel.send = AsyncMock(side_effect=new_messages)
    _wire_bot_multi(monkeypatch, {20: old_channel, 60: done_channel})

    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [_long_case(i) for i in range(10)])
    await db.update_tracker_item(
        item_number,
        test_channel_id="20", test_message_id="302", test_overflow_message_ids="301",
        status="testing",
    )

    result = await finalize_testcases_move(item_number)

    assert result["moved"] is True
    old_overflow.delete.assert_awaited_once()
    old_primary.delete.assert_awaited_once()
    assert done_channel.send.await_count == 2
    # No view= on either chunk in the Done channel — nothing left to sign off there.
    for call in done_channel.send.call_args_list:
        assert call.kwargs.get("view") is None
    item = await db.get_tracker_item(item_number)
    assert item["test_channel_id"] == "60"
    assert item["test_message_id"] == "702"
    assert item["test_overflow_message_ids"] == "701"


@pytest.mark.asyncio
async def test_post_comment_chunks_an_overlong_comment_into_multiple_sends(db, monkeypatch):
    """Same root cause and fix shape as the test-case overflow above, for the discussion-thread
    comment path (post_comment / tracker_comment)."""
    thread = AsyncMock()
    _wire_bot(monkeypatch, channel=thread)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, thread_id="777")

    # Real paragraph breaks, not one giant unbroken word -- representative of an actual long
    # comment, and it keeps this test on the ordinary line-packing path rather than the
    # single-oversized-line hard-slice fallback (already covered on its own terms by
    # TestChunkLinesForDiscord.test_a_line_longer_than_the_limit_is_hard_sliced_rather_than_
    # raising, whose pieces get rejoined with "" instead of "\n" for exactly that reason).
    paragraph = "detail " + ("q" * 60)
    long_text = "\n".join(f"{paragraph} {i}" for i in range(40))  # well over 2000 chars total
    await post_comment(item_number, long_text, author_id="1")

    assert thread.send.await_count >= 2
    for call in thread.send.call_args_list:
        assert len(call.args[0]) <= 2000
    # Lossless: every chunk concatenated back together must contain the original text verbatim
    # (the t() wrapper adds its own surrounding text, so check containment, not exact equality).
    reconstructed = "\n".join(call.args[0] for call in thread.send.call_args_list)
    assert long_text in reconstructed


@pytest.mark.asyncio
async def test_mark_environment_passed_no_longer_touches_item_status(db, monkeypatch):
    """Decoupled (tracker item #0015 follow-up, 2026-08-22): completing every test case must
    never change the item's own status any more — it only reports that completion happened."""
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="testing")

    result = await mark_environment_passed_and_refresh(item_number, "DEV", "1")

    item = await db.get_tracker_item(item_number)
    assert item["status"] == "testing"  # unchanged
    assert result["just_completed"] is True
    assert result["moved"] is False  # no Done Testing channel configured in this test
    assert result["linked_item"]["item_number"] == item_number  # still testing -> eligible for the prompt


@pytest.mark.asyncio
async def test_pass_button_acks_instead_of_silence_when_other_environment_still_pending(db, monkeypatch, mock_interaction):
    """2026-08-23 live bug report: a Pass click that doesn't complete the item (another
    environment is still pending) previously gave zero feedback at all -- easy to read as "the
    button did nothing." Must now get an explicit acknowledgment, not the "mark done?" followup
    (which is only for the genuinely-completing click)."""
    _wire_bot(monkeypatch, channel=None)
    mock_interaction.user.id = int(ADMIN_ID)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [
        {"environment": "DEV", "description": "x"}, {"environment": "PROD", "description": "y"},
    ])
    await db.update_tracker_item(item_number, status="testing")
    button = TrackerTestPassButton(item_number, "DEV")

    await button.callback(mock_interaction)

    mock_interaction.followup.send.assert_awaited_once()
    args, kwargs = mock_interaction.followup.send.call_args
    assert "DEV" in args[0]
    assert kwargs.get("view") is None
    cases = await db.get_tracker_testcases(item_number)
    assert next(c for c in cases if c["environment"] == "DEV")["passed"] == 1
    assert next(c for c in cases if c["environment"] == "PROD")["passed"] == 0


@pytest.mark.asyncio
async def test_pass_button_acks_on_redundant_click_after_already_fully_passed(db, monkeypatch, mock_interaction):
    """A second Pass click on an environment that's already fully passed must also get an
    acknowledgment instead of silence -- not the "mark done?" followup, since nothing just
    completed (edge-triggered)."""
    _wire_bot(monkeypatch, channel=None)
    mock_interaction.user.id = int(ADMIN_ID)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.mark_tracker_environment_passed(item_number, "DEV", "1")
    await db.update_tracker_item(item_number, status="testing")
    button = TrackerTestPassButton(item_number, "DEV")

    await button.callback(mock_interaction)

    mock_interaction.followup.send.assert_awaited_once()
    args, _kwargs = mock_interaction.followup.send.call_args
    assert "DEV" in args[0]


@pytest.mark.asyncio
async def test_mark_environment_passed_skips_prompt_for_already_terminal_item(db, monkeypatch):
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="rejected")

    result = await mark_environment_passed_and_refresh(item_number, "DEV", "1")

    assert result["just_completed"] is True
    assert result["linked_item"] is None  # terminal status -> never offered


@pytest.mark.asyncio
async def test_mark_environment_passed_does_not_transition_with_pending_environment(db, monkeypatch):
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [
        {"environment": "DEV", "description": "x"}, {"environment": "PROD", "description": "y"},
    ])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="testing")

    await mark_environment_passed_and_refresh(item_number, "DEV", "1")

    item = await db.get_tracker_item(item_number)
    assert item["status"] == "testing"


@pytest.mark.asyncio
async def test_mark_testing_failed_reverts_and_keeps_passed_environments(db, monkeypatch):
    message = _fake_message()
    thread = AsyncMock()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [
        {"environment": "DEV", "description": "x"}, {"environment": "PROD", "description": "y"},
    ])
    await db.update_tracker_item(
        item_number, test_channel_id="1", test_message_id="999", status="testing", thread_id="777",
    )
    await db.mark_tracker_environment_passed(item_number, "DEV", "1")

    import QBcore
    QBcore.bot.get_channel = MagicMock(return_value=thread)

    await mark_testing_failed(item_number, "1")

    item = await db.get_tracker_item(item_number)
    assert item["status"] == "in_progress"
    cases = await db.get_tracker_testcases(item_number)
    dev_case = next(c for c in cases if c["environment"] == "DEV")
    prod_case = next(c for c in cases if c["environment"] == "PROD")
    assert dev_case["passed"] == 1  # already-passed environment keeps its sign-off
    assert prod_case["passed"] == 0
    thread.send.assert_awaited_once()


# -- 👍-reaction shortcut -----------------------------------------------

def _fake_payload(emoji="👍", user_id=int(ADMIN_ID), message_id=999):
    payload = MagicMock(spec=discord.RawReactionActionEvent)
    payload.emoji = MagicMock()
    payload.emoji.name = emoji
    payload.user_id = user_id
    payload.message_id = message_id
    return payload


@pytest.mark.asyncio
async def test_reaction_marks_all_pending_environments_passed(db, monkeypatch):
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [
        {"environment": "DEV", "description": "x"}, {"environment": "PROD", "description": "y"},
    ])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="testing")

    await handle_tracker_test_reaction(_fake_payload())

    testcases = await db.get_tracker_testcases(item_number)
    assert all(c["passed"] for c in testcases)
    item = await db.get_tracker_item(item_number)
    assert item["status"] == "testing"  # decoupled (tracker item #0015 follow-up) — unaffected


@pytest.mark.asyncio
async def test_reaction_is_a_noop_when_tracker_disabled(db, monkeypatch):
    """2026-08-22 live bug report: DEV reacted to a 👍 on a PROD-authored test-case message and
    tried to mark environments passed in its own (PROD-backup-seeded) copy of the same tracker
    item, then failed with a 403 editing a message it doesn't own. Raw reaction events fire for
    every bot present in a channel regardless of which bot's message was reacted to -- unlike
    component interactions, which Discord routes only to the owning application -- so this can't
    rely on "DEV just never sees a matching row" the way the DynamicItem buttons safely can.
    tracker_enabled=False (DEV's real setting: `not is_dev_mode`, qapbot/config.py) must make
    this a true no-op even when a matching item genuinely exists in this bot's own DB."""
    import dataclasses
    from qapbot.config import CONFIG as _cfg

    monkeypatch.setattr("qapbot.config.CONFIG", dataclasses.replace(_cfg, tracker_enabled=False))
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "PROD", "description": "x"}])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="testing")

    await handle_tracker_test_reaction(_fake_payload())

    cases = await db.get_tracker_testcases(item_number)
    assert cases[0]["passed"] == 0


@pytest.mark.asyncio
async def test_reaction_completion_posts_mentioned_done_confirm_prompt_to_thread(db, monkeypatch):
    """2026-08-22 live bug report: item #0015's "mark done too?" prompt sat unnoticed in the
    discussion thread after a full pass via the reaction shortcut -- a plain, unmentioned message
    doesn't highlight/notify anyone in Discord, so the reactor had no way to know a decision was
    waiting on them. The passive path (no live interaction to attach an ephemeral followup to)
    must @mention the reactor so Discord actually surfaces it."""
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "PROD", "description": "x"}])
    await db.update_tracker_item(
        item_number, test_channel_id="1", test_message_id="999", thread_id="777", status="testing",
    )

    await handle_tracker_test_reaction(_fake_payload())

    # channel.send is used for the passive done-confirm prompt (there's no Done Testing channel
    # configured here, so the test-case archive move is a no-op and never calls it).
    sent_texts = [call.args[0] for call in channel.send.await_args_list if call.args]
    assert any(f"<@{ADMIN_ID}>" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_reaction_on_already_passed_archived_item_is_a_true_noop(db, monkeypatch):
    """2026-08-22 live bug report: reacting with a redundant second 👍 on a message that was
    already fully passed AND already archived to the Done Testing channel resurrected its
    Pass/Fail/Move-to-Done buttons. Nothing is pending, so this must not touch the message at
    all -- not even a content-only edit."""
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_DONE_TESTING_CHANNEL] = "60"
    message = _fake_message(message_id=600)
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "PROD", "description": "x"}])
    await db.mark_tracker_environment_passed(item_number, "PROD", "1")
    await db.update_tracker_item(item_number, test_channel_id="60", test_message_id="600", status="testing")

    await handle_tracker_test_reaction(_fake_payload(message_id=600))

    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_testcase_message_strips_view_once_archived(db, monkeypatch):
    """_move_test_message_to_done_testing_channel() reposts the archived message with no view=
    for a reason (nothing left to sign off) -- any LATER refresh of that same message (e.g. one
    triggered by a redundant 👍, or by mark_testing_failed on a different item entirely) must not
    undo that by reattaching a fresh interactive view. Exercises _refresh_testcase_message
    directly so the assertion holds regardless of which caller invokes it."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import _refresh_testcase_message

    CACHE.tracker_settings[TRACKER_SETTING_DONE_TESTING_CHANNEL] = "60"
    message = _fake_message(message_id=600)
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "PROD", "description": "x"}])
    await db.mark_tracker_environment_passed(item_number, "PROD", "1")
    await db.update_tracker_item(item_number, test_channel_id="60", test_message_id="600", status="testing")

    await _refresh_testcase_message(item_number)

    message.edit.assert_awaited_once()
    assert message.edit.call_args.kwargs["view"] is None


@pytest.mark.asyncio
async def test_reaction_from_non_admin_non_tester_is_ignored(db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="testing")

    await handle_tracker_test_reaction(_fake_payload(user_id=999999))

    cases = await db.get_tracker_testcases(item_number)
    assert cases[0]["passed"] == 0


@pytest.mark.asyncio
async def test_reaction_from_configured_tester_is_honored(db, monkeypatch):
    """Testers (CACHE.testers, the /admin MANAGE_TESTERS allowlist) can sign off test cases the
    same as the bot admin -- not just the admin account (2026-08-23 permission change)."""
    from qapbot.cache_manager import CACHE

    monkeypatch.setattr(CACHE, "testers", {"999999"})
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="testing")

    await handle_tracker_test_reaction(_fake_payload(user_id=999999))

    cases = await db.get_tracker_testcases(item_number)
    assert cases[0]["passed"] == 1


@pytest.mark.asyncio
async def test_reaction_with_wrong_emoji_is_ignored(db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="testing")

    await handle_tracker_test_reaction(_fake_payload(emoji="👎"))

    cases = await db.get_tracker_testcases(item_number)
    assert cases[0]["passed"] == 0


@pytest.mark.asyncio
async def test_reaction_on_unknown_message_is_a_noop(db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    # No item has test_message_id=999 — should not raise.
    await handle_tracker_test_reaction(_fake_payload(message_id=42424242))


# -- get_thread_messages (2026-08-22: tracker_comment could only WRITE into the discussion ------
# -- thread; this is the read side, previously missing entirely) --------------------------------

def _fake_thread_message(author_id, author_name, content, is_bot=False):
    import datetime

    message = MagicMock()
    message.author = MagicMock(id=author_id, display_name=author_name, bot=is_bot)
    message.content = content
    message.created_at = datetime.datetime(2026, 8, 22, 10, 0, 0, tzinfo=datetime.timezone.utc)
    return message


def _fake_thread_with_history(messages):
    """AsyncMock's default mocking of `.history` would make it an awaitable returning a plain
    value, not an async iterator `async for` can consume -- Discord's real history() is the
    latter. Wired explicitly as a plain callable returning an async generator."""
    async def _history(limit=50):
        for m in messages:
            yield m

    thread = AsyncMock()
    thread.history = _history
    return thread


@pytest.mark.asyncio
async def test_get_thread_messages_reverses_to_chronological_order(db, monkeypatch):
    # Discord's history() yields newest-first; get_thread_messages must reverse it.
    newest = _fake_thread_message("1", "Qaplop", "any update?")
    oldest = _fake_thread_message("2", "QapBot", "filed as #0016", is_bot=True)
    thread = _fake_thread_with_history([newest, oldest])
    _wire_bot(monkeypatch, channel=thread)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, thread_id="777")

    messages = await get_thread_messages(item_number)

    assert [m["content"] for m in messages] == ["filed as #0016", "any update?"]
    assert messages[0]["is_bot"] is True
    assert messages[1]["author_name"] == "Qaplop"
    assert messages[1]["author_id"] == "1"


@pytest.mark.asyncio
async def test_get_thread_messages_on_item_with_no_thread_returns_empty(db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)  # no thread_id set

    assert await get_thread_messages(item_number) == []


@pytest.mark.asyncio
async def test_get_thread_messages_on_unresolvable_channel_returns_empty(db, monkeypatch):
    """Deleted/inaccessible thread -- must degrade to empty, not raise (matches post_comment's
    sibling tolerance for other "missing optional Discord object" cases in this module)."""
    import QBcore
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown channel"))
    monkeypatch.setattr(QBcore, "bot", bot)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, thread_id="777")

    assert await get_thread_messages(item_number) == []


@pytest.mark.asyncio
async def test_get_thread_messages_on_unknown_item_raises(db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    with pytest.raises(ValueError):
        await get_thread_messages(99999)


# -- upload window ---------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_upload_windows():
    _upload_windows.clear()
    yield
    _upload_windows.clear()


def _fake_upload_message(user_id=1, channel_id=2, attachments=None):
    message = AsyncMock()
    message.author = MagicMock(id=user_id, bot=False)
    message.channel = MagicMock(id=channel_id)
    message.attachments = attachments or []
    message.delete = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_upload_window_ignores_message_with_no_open_window():
    consumed = await handle_tracker_upload_message(_fake_upload_message())
    assert consumed is False


@pytest.mark.asyncio
async def test_upload_window_leaves_open_when_message_has_no_attachments():
    received = []

    async def _on_files(pending):
        received.append(pending)

    _register_upload_window(1, 2, _on_files)
    consumed = await handle_tracker_upload_message(_fake_upload_message(attachments=[]))
    assert consumed is False
    assert (1, 2) in _upload_windows  # still open for a later attempt


@pytest.mark.asyncio
async def test_upload_window_consumes_message_with_attachments():
    received = []

    async def _on_files(pending):
        received.append(pending)

    attachment = MagicMock(spec=discord.Attachment)
    attachment.size = 100
    attachment.filename = "shot.png"
    attachment.content_type = "image/png"
    attachment.read = AsyncMock(return_value=b"bytes")

    _register_upload_window(1, 2, _on_files)
    message = _fake_upload_message(attachments=[attachment])

    consumed = await handle_tracker_upload_message(message)

    assert consumed is True
    assert (1, 2) not in _upload_windows  # one-shot
    message.delete.assert_awaited_once()
    assert len(received) == 1
    assert received[0][0]["original_name"] == "shot.png"


@pytest.mark.asyncio
async def test_upload_window_expired_is_dropped():
    async def _on_files(pending):
        pass

    _register_upload_window(1, 2, _on_files)
    _upload_windows[(1, 2)].expires_at = 0  # force expiry

    attachment = MagicMock(spec=discord.Attachment)
    attachment.size = 100
    consumed = await handle_tracker_upload_message(_fake_upload_message(attachments=[attachment]))
    assert consumed is False
    assert (1, 2) not in _upload_windows


# -- grant/revoke requestor access (plans/tracker-0021-grant-requestor-access.md) ------

async def test_grant_access_denied_for_non_admin(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db, reporter_id="222")
    item = await db.get_tracker_item(item_number)
    mock_interaction.channel.set_permissions = AsyncMock()

    button = TrackerItemButton("grantaccess", item_number)
    await button._handle_grant_access(mock_interaction, item)

    mock_interaction.channel.set_permissions.assert_not_awaited()
    mock_interaction.response.send_message.assert_awaited_once()


async def test_grant_access_grants_channel_permission_for_admin(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db, reporter_id="222")
    item = await db.get_tracker_item(item_number)
    mock_interaction.user.id = int(ADMIN_ID)
    member = MagicMock()
    member.id = 222
    mock_interaction.guild.get_member = MagicMock(return_value=member)
    mock_interaction.channel.set_permissions = AsyncMock()

    button = TrackerItemButton("grantaccess", item_number)
    await button._handle_grant_access(mock_interaction, item)

    mock_interaction.channel.set_permissions.assert_awaited_once()
    args, kwargs = mock_interaction.channel.set_permissions.call_args
    assert args[0] is member
    overwrite = kwargs["overwrite"]
    assert overwrite.view_channel is True
    assert overwrite.read_message_history is True
    assert overwrite.send_messages_in_threads is True


async def test_grant_access_response_links_to_the_discussion_thread(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db, reporter_id="222", guild_id="999")
    await db.update_tracker_item(item_number, channel_id="10", message_id="100", thread_id="777")
    item = await db.get_tracker_item(item_number)
    mock_interaction.user.id = int(ADMIN_ID)
    member = MagicMock()
    member.id = 222
    mock_interaction.guild.get_member = MagicMock(return_value=member)
    mock_interaction.channel.set_permissions = AsyncMock()

    button = TrackerItemButton("grantaccess", item_number)
    await button._handle_grant_access(mock_interaction, item)

    text = mock_interaction.response.send_message.call_args[0][0]
    assert "https://discord.com/channels/999/777" in text


async def test_grant_access_response_falls_back_to_item_message_link_without_thread(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db, reporter_id="222", guild_id="999")
    await db.update_tracker_item(item_number, channel_id="10", message_id="100")
    item = await db.get_tracker_item(item_number)
    mock_interaction.user.id = int(ADMIN_ID)
    member = MagicMock()
    member.id = 222
    mock_interaction.guild.get_member = MagicMock(return_value=member)
    mock_interaction.channel.set_permissions = AsyncMock()

    button = TrackerItemButton("grantaccess", item_number)
    await button._handle_grant_access(mock_interaction, item)

    text = mock_interaction.response.send_message.call_args[0][0]
    assert "https://discord.com/channels/999/10/100" in text


async def test_grant_access_skips_agent_filed_item(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db, reporter_id="agent:tester")
    item = await db.get_tracker_item(item_number)
    mock_interaction.user.id = int(ADMIN_ID)
    mock_interaction.channel.set_permissions = AsyncMock()

    button = TrackerItemButton("grantaccess", item_number)
    await button._handle_grant_access(mock_interaction, item)

    mock_interaction.channel.set_permissions.assert_not_awaited()


async def test_grant_access_invites_non_member_reporter(db, monkeypatch, mock_interaction):
    fake_user = AsyncMock()
    _wire_bot(monkeypatch, channel=None, user=fake_user)
    item_number = await _make_item(db, reporter_id="222")
    item = await db.get_tracker_item(item_number)
    mock_interaction.user.id = int(ADMIN_ID)
    mock_interaction.guild.get_member = MagicMock(return_value=None)
    mock_interaction.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    mock_interaction.channel.set_permissions = AsyncMock()
    mock_interaction.channel.create_invite = AsyncMock(return_value=MagicMock(url="https://discord.gg/abc123"))

    button = TrackerItemButton("grantaccess", item_number)
    await button._handle_grant_access(mock_interaction, item)

    mock_interaction.channel.create_invite.assert_awaited_once()
    mock_interaction.channel.set_permissions.assert_not_awaited()
    fake_user.send.assert_awaited_once()
    dm_text = fake_user.send.call_args[0][0]
    assert "https://discord.gg/abc123" in dm_text

    updated = await db.get_tracker_item(item_number)
    assert updated["access_grant_pending"] == 1

    response_text = mock_interaction.response.send_message.call_args[0][0]
    assert "https://discord.gg/abc123" in response_text


async def test_grant_access_invite_dm_failure_still_reports_invite_url(db, monkeypatch, mock_interaction):
    fake_user = AsyncMock()
    fake_user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "blocked"))
    _wire_bot(monkeypatch, channel=None, user=fake_user)
    item_number = await _make_item(db, reporter_id="222")
    item = await db.get_tracker_item(item_number)
    mock_interaction.user.id = int(ADMIN_ID)
    mock_interaction.guild.get_member = MagicMock(return_value=None)
    mock_interaction.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    mock_interaction.channel.create_invite = AsyncMock(return_value=MagicMock(url="https://discord.gg/abc123"))

    button = TrackerItemButton("grantaccess", item_number)
    await button._handle_grant_access(mock_interaction, item)

    response_text = mock_interaction.response.send_message.call_args[0][0]
    assert "https://discord.gg/abc123" in response_text
    updated = await db.get_tracker_item(item_number)
    assert updated["access_grant_pending"] == 1


async def test_grant_access_invite_creation_failure(db, monkeypatch, mock_interaction):
    fake_user = AsyncMock()
    _wire_bot(monkeypatch, channel=None, user=fake_user)
    item_number = await _make_item(db, reporter_id="222")
    item = await db.get_tracker_item(item_number)
    mock_interaction.user.id = int(ADMIN_ID)
    mock_interaction.guild.get_member = MagicMock(return_value=None)
    mock_interaction.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    mock_interaction.channel.create_invite = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))

    button = TrackerItemButton("grantaccess", item_number)
    await button._handle_grant_access(mock_interaction, item)

    fake_user.send.assert_not_awaited()
    updated = await db.get_tracker_item(item_number)
    assert updated["access_grant_pending"] == 0


async def test_grant_access_member_not_found_when_reporter_user_unresolvable(db, monkeypatch, mock_interaction):
    _wire_bot(monkeypatch, channel=None, user=None)
    item_number = await _make_item(db, reporter_id="222")
    item = await db.get_tracker_item(item_number)
    mock_interaction.user.id = int(ADMIN_ID)
    mock_interaction.guild.get_member = MagicMock(return_value=None)
    mock_interaction.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
    mock_interaction.channel.create_invite = AsyncMock()

    button = TrackerItemButton("grantaccess", item_number)
    await button._handle_grant_access(mock_interaction, item)

    mock_interaction.channel.create_invite.assert_not_awaited()


# -- apply_pending_requestor_access (on_member_join) --------------------

async def test_apply_pending_requestor_access_grants_and_clears_flag(db, monkeypatch):
    channel = _fake_channel()
    channel.set_permissions = AsyncMock()

    item_number = await _make_item(db, reporter_id="222", guild_id="999")
    await db.update_tracker_item(
        item_number, channel_id="10", message_id="100", status="open", access_grant_pending=1
    )

    member = AsyncMock()
    member.id = 222
    member.guild = MagicMock()
    member.guild.id = 999
    member.guild.get_channel = MagicMock(return_value=channel)

    await apply_pending_requestor_access(member)

    channel.set_permissions.assert_awaited_once()
    args, kwargs = channel.set_permissions.call_args
    assert args[0] is member
    assert kwargs["overwrite"].view_channel is True
    member.send.assert_awaited_once()

    updated = await db.get_tracker_item(item_number)
    assert updated["access_grant_pending"] == 0


async def test_apply_pending_requestor_access_skips_terminal_item(db, monkeypatch):
    channel = _fake_channel()
    channel.set_permissions = AsyncMock()

    item_number = await _make_item(db, reporter_id="222", guild_id="999")
    await db.update_tracker_item(
        item_number, channel_id="10", message_id="100", status="done", access_grant_pending=1
    )

    member = AsyncMock()
    member.id = 222
    member.guild = MagicMock()
    member.guild.id = 999
    member.guild.get_channel = MagicMock(return_value=channel)

    await apply_pending_requestor_access(member)

    channel.set_permissions.assert_not_awaited()


async def test_apply_pending_requestor_access_noop_when_tracker_disabled(db, monkeypatch):
    from qapbot.config import CONFIG
    import dataclasses
    monkeypatch.setattr("qapbot.config.CONFIG", dataclasses.replace(CONFIG, tracker_enabled=False))

    channel = _fake_channel()
    channel.set_permissions = AsyncMock()

    item_number = await _make_item(db, reporter_id="222", guild_id="999")
    await db.update_tracker_item(
        item_number, channel_id="10", message_id="100", status="open", access_grant_pending=1
    )

    member = AsyncMock()
    member.id = 222
    member.guild = MagicMock()
    member.guild.id = 999

    await apply_pending_requestor_access(member)

    channel.set_permissions.assert_not_awaited()


async def test_apply_status_change_done_revokes_requestor_access_when_moved(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_IMPLEMENTED_CHANNEL] = "50"

    old_item_message = _fake_message(message_id=100)
    old_reports_channel = _fake_channel(fetch_message=old_item_message)
    old_reports_channel.set_permissions = AsyncMock()
    implemented_channel = _fake_channel(send_message=_fake_message(message_id=555))
    implemented_channel.id = 50
    bot = _wire_bot_multi(monkeypatch, {10: old_reports_channel, 50: implemented_channel})

    member = MagicMock()
    member.id = 222
    guild = MagicMock()
    guild.get_member = MagicMock(return_value=member)
    bot.get_guild = MagicMock(return_value=guild)

    item_number = await _make_item(db, reporter_id="222", guild_id="999")
    await db.update_tracker_item(item_number, channel_id="10", message_id="100", status="open")

    await apply_status_change(item_number, "done", actor_id="1")

    old_reports_channel.set_permissions.assert_awaited_once()
    args, kwargs = old_reports_channel.set_permissions.call_args
    assert args[0] is member
    assert kwargs["overwrite"] is None


async def test_apply_status_change_done_keeps_access_with_another_open_item_in_same_channel(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    CACHE.tracker_settings[TRACKER_SETTING_IMPLEMENTED_CHANNEL] = "50"

    old_item_message = _fake_message(message_id=100)
    old_reports_channel = _fake_channel(fetch_message=old_item_message)
    old_reports_channel.set_permissions = AsyncMock()
    implemented_channel = _fake_channel(send_message=_fake_message(message_id=555))
    implemented_channel.id = 50
    bot = _wire_bot_multi(monkeypatch, {10: old_reports_channel, 50: implemented_channel})

    member = MagicMock()
    member.id = 222
    guild = MagicMock()
    guild.get_member = MagicMock(return_value=member)
    bot.get_guild = MagicMock(return_value=guild)

    item_number = await _make_item(db, reporter_id="222", guild_id="999")
    await db.update_tracker_item(item_number, channel_id="10", message_id="100", status="open")
    other_item_number = await _make_item(db, reporter_id="222", guild_id="999")
    await db.update_tracker_item(other_item_number, channel_id="10", status="open")

    await apply_status_change(item_number, "done", actor_id="1")

    old_reports_channel.set_permissions.assert_not_awaited()


async def test_apply_status_change_done_without_implemented_channel_does_not_revoke(db, monkeypatch):
    old_item_message = _fake_message(message_id=100)
    old_reports_channel = _fake_channel(fetch_message=old_item_message)
    old_reports_channel.set_permissions = AsyncMock()
    _wire_bot(monkeypatch, channel=old_reports_channel)

    item_number = await _make_item(db, reporter_id="222", guild_id="999")
    await db.update_tracker_item(item_number, channel_id="10", message_id="100", status="open")

    await apply_status_change(item_number, "done", actor_id="1")

    old_reports_channel.set_permissions.assert_not_awaited()
