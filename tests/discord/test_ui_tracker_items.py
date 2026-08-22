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
    TrackerDraftView,
    TrackerItemModal,
    _sanitize_attachment_filename,
    apply_status_change,
    build_tracker_embed,
    create_tracker_item_for_agent,
    finalize_testcases_move,
    get_thread_messages,
    handle_tracker_test_reaction,
    handle_tracker_upload_message,
    mark_environment_passed_and_refresh,
    mark_testing_failed,
    post_test_cases,
    start_tracker_item,
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
    monkeypatch.setattr("qapbot.config.CONFIG", dataclasses.replace(CONFIG, server_admin=ADMIN_ID))
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
async def test_reaction_from_non_admin_is_ignored(db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="testing")

    await handle_tracker_test_reaction(_fake_payload(user_id=999999))

    cases = await db.get_tracker_testcases(item_number)
    assert cases[0]["passed"] == 0


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
