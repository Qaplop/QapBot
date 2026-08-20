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
    TRACKER_SETTING_ENABLED,
    TRACKER_SETTING_TEST_CHANNEL,
    TrackerDraftView,
    TrackerItemModal,
    _sanitize_attachment_filename,
    apply_status_change,
    build_tracker_embed,
    handle_tracker_test_reaction,
    handle_tracker_upload_message,
    mark_environment_passed_and_refresh,
    mark_testing_failed,
    post_test_cases,
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


async def _make_item(db, item_type="bug", **overrides):
    kwargs = dict(item_type=item_type, title="t", description="d", reporter_id="111", reporter_name="A")
    kwargs.update(overrides)
    item_number = await db.create_tracker_item(**kwargs)
    return item_number


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
    assert modal.environment_input not in modal.children


def test_modal_keeps_environment_field_for_bug():
    modal = TrackerItemModal("bug", guild_id=None, user_id="1")
    assert modal.environment_input in modal.children


def test_modal_title_differs_by_type():
    bug_modal = TrackerItemModal("bug", guild_id=None, user_id="1")
    feature_modal = TrackerItemModal("feature", guild_id=None, user_id="1")
    assert bug_modal.title != feature_modal.title


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
async def test_mark_environment_passed_transitions_to_done_when_all_envs_pass(db, monkeypatch):
    message = _fake_message()
    channel = _fake_channel(fetch_message=message)
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.update_tracker_item(item_number, test_channel_id="1", test_message_id="999", status="testing")

    await mark_environment_passed_and_refresh(item_number, "DEV", "1")

    item = await db.get_tracker_item(item_number)
    assert item["status"] == "done"


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

    item = await db.get_tracker_item(item_number)
    assert item["status"] == "done"


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
