"""Tests for the bug/feature tracker's web bridge endpoints (BUG_FEATURE_TRACKER_PLAN.md
Phase 6, §6.4): the shared-secret gate, item list/detail/attachment reads, and the
status/comment/testcases write endpoints.
"""
# pyright: reportPrivateUsage=false, reportOptionalMemberAccess=false, reportArgumentType=false
from __future__ import annotations

import dataclasses
import os
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.config import CONFIG
from qapbot.db_manager import WarHistoryDB

SECRET = "test-secret"


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
def _wire_cache(db, monkeypatch):
    from qapbot.cache_manager import CACHE
    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "tracker_settings", {})


@pytest.fixture
def bridge_config(monkeypatch):
    config = dataclasses.replace(CONFIG, web_bridge_secret=SECRET, web_bridge_port=1)
    monkeypatch.setattr("qapbot.config.CONFIG", config)
    return config


@pytest.fixture
async def client(bridge_config):
    from qapbot.web_bridge import create_app
    async with TestClient(TestServer(create_app())) as c:
        yield c


def _wire_bot(monkeypatch, channel=None, user=None):
    import QBcore
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    bot.get_user = MagicMock(return_value=user)
    bot.fetch_user = AsyncMock(return_value=user)
    monkeypatch.setattr(QBcore, "bot", bot)
    return bot


def _fake_channel():
    channel = AsyncMock()
    message = AsyncMock()
    message.id = 999
    message.jump_url = "https://discord.com/channels/1/2/999"
    channel.send = AsyncMock(return_value=message)
    channel.fetch_message = AsyncMock(return_value=None)
    return channel


async def _make_item(db, **overrides):
    kwargs = dict(item_type="bug", title="t", description="d", reporter_id="111", reporter_name="A")
    kwargs.update(overrides)
    return await db.create_tracker_item(**kwargs)


# -- auth gate ------------------------------------------------------------

async def test_list_items_requires_secret(client):
    resp = await client.get("/api/tracker/items")
    assert resp.status == 403


async def test_list_items_rejects_wrong_secret(client):
    resp = await client.get("/api/tracker/items", headers={"X-Bridge-Secret": "wrong"})
    assert resp.status == 403


# -- list / get ----------------------------------------------------------

async def test_list_items_returns_seeded_items(client, db):
    await _make_item(db, title="first")
    await _make_item(db, item_type="feature", title="second")
    resp = await client.get("/api/tracker/items", headers={"X-Bridge-Secret": SECRET})
    assert resp.status == 200
    body = await resp.json()
    assert len(body["items"]) == 2


async def test_list_items_includes_priority(client, db):
    await _make_item(db, priority="HIGH")
    resp = await client.get("/api/tracker/items", headers={"X-Bridge-Secret": SECRET})
    body = await resp.json()
    assert body["items"][0]["priority"] == "HIGH"


async def test_list_items_filters_by_type(client, db):
    await _make_item(db, item_type="bug")
    await _make_item(db, item_type="feature")
    resp = await client.get("/api/tracker/items?type=feature", headers={"X-Bridge-Secret": SECRET})
    body = await resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["item_type"] == "feature"


async def test_get_item_returns_full_detail(client, db):
    item_number = await _make_item(db, details="steps")
    await db.add_tracker_attachment(
        item_number, filename="1_shot.png", original_name="shot.png",
        size_bytes=10, local_path="x", content_type="image/png",
    )
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "d"}])

    resp = await client.get(f"/api/tracker/items/{item_number}", headers={"X-Bridge-Secret": SECRET})
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["item_number"] == item_number
    assert len(body["attachments"]) == 1
    assert len(body["testcases"]) == 1


async def test_get_item_404_for_missing(client):
    resp = await client.get("/api/tracker/items/99999", headers={"X-Bridge-Secret": SECRET})
    assert resp.status == 404


# -- attachment bytes + path containment -----------------------------------

async def test_get_attachment_streams_file(client, db, tmp_path, bridge_config, monkeypatch):
    item_number = await _make_item(db)
    tracker_dir = str(tmp_path / "tracker_data")
    file_dir = os.path.join(tracker_dir, f"{item_number:04d}")
    os.makedirs(file_dir, exist_ok=True)
    full_path = os.path.join(file_dir, "1_shot.png")
    with open(full_path, "wb") as f:
        f.write(b"hello")
    aid = await db.add_tracker_attachment(
        item_number, filename="1_shot.png", original_name="shot.png",
        size_bytes=5, local_path=full_path, content_type="image/png",
    )
    monkeypatch.setattr("qapbot.config.CONFIG", dataclasses.replace(bridge_config, tracker_data_dir=tracker_dir))

    resp = await client.get(f"/api/tracker/items/{item_number}/attachments/{aid}", headers={"X-Bridge-Secret": SECRET})
    assert resp.status == 200
    body = await resp.read()
    assert body == b"hello"


async def test_get_attachment_rejects_path_outside_tracker_root(client, db, tmp_path, bridge_config, monkeypatch):
    item_number = await _make_item(db)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"secret")
    aid = await db.add_tracker_attachment(
        item_number, filename="1_x.txt", original_name="x.txt",
        size_bytes=6, local_path=str(outside_file), content_type="text/plain",
    )
    monkeypatch.setattr("qapbot.config.CONFIG", dataclasses.replace(bridge_config, tracker_data_dir=str(tmp_path / "tracker_data")))

    resp = await client.get(f"/api/tracker/items/{item_number}/attachments/{aid}", headers={"X-Bridge-Secret": SECRET})
    assert resp.status == 400


async def test_get_attachment_404_for_unknown_id(client, db):
    item_number = await _make_item(db)
    resp = await client.get(f"/api/tracker/items/{item_number}/attachments/999", headers={"X-Bridge-Secret": SECRET})
    assert resp.status == 404


# -- status ----------------------------------------------------------------

async def test_post_status_updates_item(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    resp = await client.post(
        f"/api/tracker/items/{item_number}/status",
        json={"status": "triaged"},
        headers={"X-Bridge-Secret": SECRET, "X-Tracker-Admin": "claude"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "triaged"
    assert body["item"]["last_edited_by"] == "claude"


async def test_post_status_rejects_invalid_value(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    resp = await client.post(
        f"/api/tracker/items/{item_number}/status",
        json={"status": "not_a_real_status"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 400


async def test_post_status_implemented_dms_reporter(client, db, monkeypatch):
    fake_user = AsyncMock()
    _wire_bot(monkeypatch, channel=None, user=fake_user)
    item_number = await _make_item(db, reporter_id="222")
    resp = await client.post(
        f"/api/tracker/items/{item_number}/status",
        json={"status": "implemented", "note": "fixed in commit abc123"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 200
    fake_user.send.assert_awaited_once()
    body = await resp.json()
    assert body["item"]["implemented_note"] == "fixed in commit abc123"


async def test_post_status_404_for_missing_item(client, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    resp = await client.post(
        "/api/tracker/items/99999/status", json={"status": "triaged"}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 404


# -- comment -----------------------------------------------------------

async def test_post_comment_requires_thread(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    resp = await client.post(
        f"/api/tracker/items/{item_number}/comment", json={"text": "hi"}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 404


async def test_post_comment_posts_to_thread(client, db, monkeypatch):
    thread = AsyncMock()
    _wire_bot(monkeypatch, channel=thread)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, thread_id="777")
    resp = await client.post(
        f"/api/tracker/items/{item_number}/comment",
        json={"text": "which clan tag?"},
        headers={"X-Bridge-Secret": SECRET, "X-Tracker-Admin": "claude"},
    )
    assert resp.status == 200
    thread.send.assert_awaited_once()


async def test_post_comment_requires_text(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, thread_id="777")
    resp = await client.post(
        f"/api/tracker/items/{item_number}/comment", json={"text": "  "}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 400


async def test_post_comment_reports_discord_failure_as_json_not_a_bare_500(client, db, monkeypatch):
    """Tracker #0028, live incident: post_comment() used to let discord.HTTPException propagate
    all the way to aiohttp's default handler, which produced a bare text/plain 500 the MCP client
    could not even parse as JSON ("Attempt to decode JSON with unexpected mimetype"). Chunking
    (see test_ui_tracker_items.py) fixes the routine over-length case; this covers what's left --
    a genuine Discord-side failure (outage, permissions) even after chunking -- and asserts it
    comes back as a real JSON error instead."""
    thread = AsyncMock()
    thread.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=503), "service unavailable"))
    _wire_bot(monkeypatch, channel=thread)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, thread_id="777")

    resp = await client.post(
        f"/api/tracker/items/{item_number}/comment",
        json={"text": "hello"},
        headers={"X-Bridge-Secret": SECRET},
    )

    assert resp.status == 502
    body = await resp.json()  # must be parseable JSON, not the raw exception text
    assert "error" in body


# -- thread history (2026-08-22: tracker_comment could only WRITE into the discussion thread; ---
# -- this is the read side, previously missing entirely) ----------------------------------------

def _fake_thread_message(author_id, author_name, content, is_bot=False):
    import datetime

    message = MagicMock()
    message.author = MagicMock(id=author_id, display_name=author_name, bot=is_bot)
    message.content = content
    message.created_at = datetime.datetime(2026, 8, 22, 10, 0, 0, tzinfo=datetime.timezone.utc)
    return message


def _fake_thread_with_history(messages):
    """Discord's history() is an async iterator, not a coroutine -- AsyncMock's default mocking
    of `.history` would make it an awaitable returning a plain value, not something `async for`
    can iterate. Wire it explicitly as a plain callable returning an async generator."""
    async def _history(limit=50):
        for m in messages:
            yield m

    thread = AsyncMock()
    thread.history = _history
    return thread


async def test_get_thread_returns_messages_oldest_first(client, db, monkeypatch):
    # Discord's history() yields newest-first; the endpoint must reverse it.
    newest = _fake_thread_message("1", "Qaplop", "any update?")
    oldest = _fake_thread_message("2", "QapBot", "filed as #0016", is_bot=True)
    thread = _fake_thread_with_history([newest, oldest])
    _wire_bot(monkeypatch, channel=thread)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, thread_id="777")

    resp = await client.get(
        f"/api/tracker/items/{item_number}/thread", headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 200
    body = await resp.json()
    assert [m["content"] for m in body["messages"]] == ["filed as #0016", "any update?"]
    assert body["messages"][0]["is_bot"] is True
    assert body["messages"][1]["author_name"] == "Qaplop"


async def test_get_thread_on_item_with_no_thread_returns_empty(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)  # no thread_id set

    resp = await client.get(
        f"/api/tracker/items/{item_number}/thread", headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["messages"] == []


async def test_get_thread_on_unknown_item_is_404(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    resp = await client.get(
        "/api/tracker/items/99999/thread", headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 404


async def test_get_thread_requires_secret(client, db):
    item_number = await _make_item(db)
    resp = await client.get(f"/api/tracker/items/{item_number}/thread")
    assert resp.status == 403


# -- testcases ---------------------------------------------------------

async def test_post_testcases_creates_and_posts(client, db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_SETTING_TEST_CHANNEL
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases",
        json={"cases": [{"environment": "DEV", "description": "run /leaderboard"}]},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "testing"
    channel.send.assert_awaited_once()


async def test_post_testcases_accepts_per_case_priority(client, db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_SETTING_TEST_CHANNEL
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases",
        json={"cases": [{"environment": "DEV", "description": "run it", "priority": "HIGH"}]},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 200
    cases = await db.get_tracker_testcases(item_number)
    assert cases[0]["priority"] == "HIGH"


async def test_post_testcases_rejects_invalid_priority(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases",
        json={"cases": [{"environment": "DEV", "description": "x", "priority": "URGENT"}]},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 400


async def test_post_testcases_validates_case_shape(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases",
        json={"cases": [{"environment": "QA", "description": "bad env"}]},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 400


async def test_post_testcases_requires_non_empty_list(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases", json={"cases": []}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 400


async def test_post_testcases_reports_discord_failure_as_json_and_keeps_the_db_write(client, db, monkeypatch):
    """Tracker #0028, live incident: a Discord-side failure posting the test-case message used to
    surface as a bare text/plain 500 the caller couldn't parse as JSON -- and worse, masked that
    set_tracker_testcases() (the DB write, which runs BEFORE the Discord post) had already
    committed successfully. Confirms both halves of the fix: a real JSON error comes back, AND
    the rows are there regardless -- a caller retrying the same cases after this error is safe
    (set_tracker_testcases replaces the full set, it doesn't append)."""
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_SETTING_TEST_CHANNEL
    CACHE.tracker_settings[TRACKER_SETTING_TEST_CHANNEL] = "1"
    channel = _fake_channel()
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=503), "service unavailable"))
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases",
        json={"cases": [{"environment": "DEV", "description": "run it"}]},
        headers={"X-Bridge-Secret": SECRET},
    )

    assert resp.status == 502
    body = await resp.json()
    assert "error" in body
    cases = await db.get_tracker_testcases(item_number)
    assert len(cases) == 1
    assert cases[0]["description"] == "run it"


# -- create item (tracker item #0015) --------------------------------------

async def test_post_create_item_requires_secret(client):
    resp = await client.post("/api/tracker/items", json={"item_type": "bug", "title": "t", "description": "d"})
    assert resp.status == 403


async def test_post_create_item_persists_and_posts(client, db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_SETTING_BUG_CHANNEL
    CACHE.tracker_settings[TRACKER_SETTING_BUG_CHANNEL] = "1"
    channel = _fake_channel()
    _wire_bot(monkeypatch, channel=channel)

    resp = await client.post(
        "/api/tracker/items",
        json={"item_type": "bug", "title": "Odd fetch", "description": "desc"},
        headers={"X-Bridge-Secret": SECRET, "X-Tracker-Admin": "claude"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["item_number"]
    assert body["jump_url"]
    channel.send.assert_awaited_once()
    stored = await db.get_tracker_item(body["item_number"])
    assert stored["reporter_id"] == "agent:claude"
    assert stored["reporter_name"] == "claude"


async def test_post_create_item_rejects_invalid_item_type(client, db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_SETTING_BUG_CHANNEL
    CACHE.tracker_settings[TRACKER_SETTING_BUG_CHANNEL] = "1"
    resp = await client.post(
        "/api/tracker/items", json={"item_type": "epic", "title": "t", "description": "d"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 400


async def test_post_create_item_requires_title_and_description(client, db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_SETTING_BUG_CHANNEL
    CACHE.tracker_settings[TRACKER_SETTING_BUG_CHANNEL] = "1"
    resp = await client.post(
        "/api/tracker/items", json={"item_type": "bug", "title": "", "description": "d"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 400


async def test_post_create_item_rejects_invalid_priority(client, db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_SETTING_BUG_CHANNEL
    CACHE.tracker_settings[TRACKER_SETTING_BUG_CHANNEL] = "1"
    resp = await client.post(
        "/api/tracker/items",
        json={"item_type": "bug", "title": "t", "description": "d", "priority": "URGENT"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 400


async def test_post_create_item_requires_configured_channel(client, db, monkeypatch):
    resp = await client.post(
        "/api/tracker/items", json={"item_type": "bug", "title": "t", "description": "d"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 400


# -- test case pass/fail (agent-drivable sign-off loop) --------------------

async def test_post_testcase_pass_requires_secret(client):
    resp = await client.post("/api/tracker/items/1/testcases/pass", json={"environment": "PROD"})
    assert resp.status == 403


async def test_post_testcase_pass_marks_environment_passed(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "PROD", "description": "x"}])

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/pass",
        json={"environment": "PROD"},
        headers={"X-Bridge-Secret": SECRET, "X-Tracker-Admin": "claude"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["testcases"][0]["passed"] == 1
    assert body["testcases"][0]["passed_by"] == "claude"


async def test_post_testcase_pass_reports_completion_without_touching_item_status(client, db, monkeypatch):
    """Decoupled (tracker item #0015 follow-up, 2026-08-22): the pass endpoint reports
    completion/linked-item info but never changes the item's own status any more."""
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="testing")
    await db.set_tracker_testcases(item_number, [{"environment": "PROD", "description": "x"}])

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/pass",
        json={"environment": "PROD"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "testing"
    assert body["testcases_just_completed"] is True
    assert body["moved"] is False  # no Done Testing channel configured
    assert body["linked_item"]["item_number"] == item_number


async def test_post_testcase_pass_skips_linked_item_when_already_terminal(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="rejected")
    await db.set_tracker_testcases(item_number, [{"environment": "PROD", "description": "x"}])

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/pass",
        json={"environment": "PROD"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["testcases_just_completed"] is True
    assert body["linked_item"] is None


async def test_post_testcase_pass_rejects_invalid_environment(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/pass",
        json={"environment": "QA"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 400


async def test_post_testcase_pass_404_for_missing_item(client, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    resp = await client.post(
        "/api/tracker/items/99999/testcases/pass", json={"environment": "PROD"}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 404


async def test_post_testcase_pass_400_for_environment_with_no_cases(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/pass",
        json={"environment": "PROD"},
        headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 400


async def test_post_testcase_fail_requires_secret(client):
    resp = await client.post("/api/tracker/items/1/testcases/fail", json={})
    assert resp.status == 403


async def test_post_testcase_fail_reverts_to_in_progress(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.update_tracker_item(item_number, status="testing")

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/fail", json={}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["item"]["status"] == "in_progress"


async def test_post_testcase_fail_404_for_missing_item(client, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    resp = await client.post(
        "/api/tracker/items/99999/testcases/fail", json={}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 404


# -- move-done (manual archive, decoupled from item status) ----------------

async def test_post_testcase_move_done_requires_secret(client):
    resp = await client.post("/api/tracker/items/1/testcases/move-done", json={})
    assert resp.status == 403


async def test_post_testcase_move_done_404_for_missing_item(client, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    resp = await client.post(
        "/api/tracker/items/99999/testcases/move-done", json={}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 404


async def test_post_testcase_move_done_requires_testcases(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/move-done", json={}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 400


async def test_post_testcase_move_done_needs_confirmation_when_incomplete(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [
        {"environment": "DEV", "description": "x"}, {"environment": "PROD", "description": "y"},
    ])
    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/move-done", json={}, headers={"X-Bridge-Secret": SECRET}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["moved"] is False
    assert body["needs_confirmation"] is True
    assert body["unchecked_count"] == 2


async def test_post_testcase_move_done_force_moves_despite_incomplete(client, db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_SETTING_DONE_TESTING_CHANNEL
    CACHE.tracker_settings[TRACKER_SETTING_DONE_TESTING_CHANNEL] = "1"
    channel = _fake_channel()
    channel.fetch_message = AsyncMock(return_value=AsyncMock())
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.update_tracker_item(item_number, test_channel_id="2", test_message_id="999")

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/move-done",
        json={"force": True}, headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["moved"] is True
    assert body["needs_confirmation"] is False
    channel.send.assert_awaited_once()
    # forcing a move must not silently mark the still-unchecked case passed
    cases = await db.get_tracker_testcases(item_number)
    assert cases[0]["passed"] == 0


async def test_post_testcase_move_done_moves_directly_when_already_complete(client, db, monkeypatch):
    from qapbot.cache_manager import CACHE
    from qapbot.ui_tracker import TRACKER_SETTING_DONE_TESTING_CHANNEL
    CACHE.tracker_settings[TRACKER_SETTING_DONE_TESTING_CHANNEL] = "1"
    channel = _fake_channel()
    channel.fetch_message = AsyncMock(return_value=AsyncMock())
    _wire_bot(monkeypatch, channel=channel)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.mark_tracker_environment_passed(item_number, "DEV", "1")
    await db.update_tracker_item(item_number, test_channel_id="2", test_message_id="999", status="testing")

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/move-done", json={}, headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["moved"] is True
    assert body["needs_confirmation"] is False
    assert body["linked_item"]["item_number"] == item_number  # still testing -> eligible


async def test_post_testcase_move_done_no_linked_item_when_item_terminal(client, db, monkeypatch):
    _wire_bot(monkeypatch, channel=None)
    item_number = await _make_item(db)
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "x"}])
    await db.mark_tracker_environment_passed(item_number, "DEV", "1")
    await db.update_tracker_item(item_number, status="done")

    resp = await client.post(
        f"/api/tracker/items/{item_number}/testcases/move-done", json={}, headers={"X-Bridge-Secret": SECRET},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["linked_item"] is None

