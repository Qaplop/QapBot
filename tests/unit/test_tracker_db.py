"""Tests for the bug/feature tracker persistence layer (BUG_FEATURE_TRACKER_PLAN.md Phase 1):
bot_settings, tracker_items, tracker_attachments, tracker_testcases DDL + db_manager CRUD.
"""
from __future__ import annotations

import pytest

from qapbot.db_manager import WarHistoryDB


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "qapbot_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    try:
        yield manager
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_reinitialize_is_idempotent(tmp_path):
    """Re-running initialize() against the same DB file must not raise (Cardinal Rule 12)."""
    db_path = tmp_path / "qapbot_test.db"
    manager = WarHistoryDB()
    await manager.initialize(str(db_path))
    await manager.close()

    manager2 = WarHistoryDB()
    await manager2.initialize(str(db_path))
    await manager2.close()


# -- bot_settings ---------------------------------------------------------

@pytest.mark.asyncio
async def test_bot_setting_roundtrip_global_scope(db):
    assert await db.get_bot_setting("tracker_bug_channel_id") is None
    await db.set_bot_setting("tracker_bug_channel_id", "12345")
    assert await db.get_bot_setting("tracker_bug_channel_id") == "12345"


@pytest.mark.asyncio
async def test_bot_setting_upsert_overwrites(db):
    await db.set_bot_setting("tracker_enabled", "1")
    await db.set_bot_setting("tracker_enabled", "0")
    assert await db.get_bot_setting("tracker_enabled") == "0"


@pytest.mark.asyncio
async def test_bot_setting_scoped_by_guild_id(db):
    await db.set_bot_setting("some_key", "global_value")
    await db.set_bot_setting("some_key", "guild_value", guild_id="999")
    assert await db.get_bot_setting("some_key") == "global_value"
    assert await db.get_bot_setting("some_key", guild_id="999") == "guild_value"


@pytest.mark.asyncio
async def test_get_all_bot_settings(db):
    await db.set_bot_setting("a", "1")
    await db.set_bot_setting("b", "2")
    await db.set_bot_setting("c", "3", guild_id="999")  # different scope, excluded
    assert await db.get_all_bot_settings() == {"a": "1", "b": "2"}


# -- tracker_items ----------------------------------------------------------

@pytest.mark.asyncio
async def test_create_and_get_tracker_item(db):
    item_number = await db.create_tracker_item(
        item_type="bug",
        title="Leaderboard shows stale stars",
        description="Between round 3 and 4 the leaderboard keeps showing old data.",
        reporter_id="111",
        reporter_name="Qaplop",
        details="1. Do X\n2. Do Y",
        environment="PROD",
        guild_id="222",
    )
    item = await db.get_tracker_item(item_number)
    assert item is not None
    assert item["item_type"] == "bug"
    assert item["status"] == "open"
    assert item["title"] == "Leaderboard shows stale stars"
    assert item["reporter_id"] == "111"
    assert item["environment"] == "PROD"
    assert item["guild_id"] == "222"


@pytest.mark.asyncio
async def test_get_tracker_item_missing_returns_none(db):
    assert await db.get_tracker_item(9999) is None


@pytest.mark.asyncio
async def test_bugs_and_features_share_one_id_pool(db):
    bug_id = await db.create_tracker_item(
        item_type="bug", title="t1", description="d1", reporter_id="1", reporter_name="A"
    )
    feature_id = await db.create_tracker_item(
        item_type="feature", title="t2", description="d2", reporter_id="1", reporter_name="A"
    )
    assert feature_id == bug_id + 1


@pytest.mark.asyncio
async def test_list_tracker_items_filters(db):
    await db.create_tracker_item(item_type="bug", title="b1", description="d", reporter_id="1", reporter_name="A")
    await db.create_tracker_item(item_type="feature", title="f1", description="d", reporter_id="2", reporter_name="B")

    bugs = await db.list_tracker_items(item_type="bug")
    assert len(bugs) == 1
    assert bugs[0]["item_type"] == "bug"

    by_reporter = await db.list_tracker_items(reporter_id="2")
    assert len(by_reporter) == 1
    assert by_reporter[0]["reporter_id"] == "2"

    open_items = await db.list_tracker_items(status="open")
    assert len(open_items) == 2


@pytest.mark.asyncio
async def test_update_tracker_item_updates_fields_and_timestamp(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    before = await db.get_tracker_item(item_number)
    await db.update_tracker_item(item_number, status="in_progress", last_edited_by="1")
    after = await db.get_tracker_item(item_number)
    assert after["status"] == "in_progress"
    assert after["last_edited_by"] == "1"
    assert after["updated_at"] >= before["updated_at"]


@pytest.mark.asyncio
async def test_update_tracker_item_rejects_unknown_field(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    with pytest.raises(ValueError):
        await db.update_tracker_item(item_number, item_type="feature")  # not in whitelist


@pytest.mark.asyncio
async def test_set_tracker_item_message(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    await db.set_tracker_item_message(item_number, channel_id="c1", message_id="m1", thread_id="th1")
    item = await db.get_tracker_item(item_number)
    assert item["channel_id"] == "c1"
    assert item["message_id"] == "m1"
    assert item["thread_id"] == "th1"


# -- tracker_attachments ------------------------------------------------

@pytest.mark.asyncio
async def test_add_and_get_tracker_attachments(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    await db.add_tracker_attachment(
        item_number, filename="1_shot.png", original_name="shot.png",
        size_bytes=1024, local_path="tracker/0001/1_shot.png", content_type="image/png",
    )
    await db.add_tracker_attachment(
        item_number, filename="2_log.txt", original_name="log.txt",
        size_bytes=512, local_path="tracker/0001/2_log.txt", content_type="text/plain",
    )
    attachments = await db.get_tracker_attachments(item_number)
    assert len(attachments) == 2
    assert attachments[0]["filename"] == "1_shot.png"
    assert attachments[1]["content_type"] == "text/plain"


@pytest.mark.asyncio
async def test_tracker_attachments_cascade_delete_with_item(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    await db.add_tracker_attachment(
        item_number, filename="1_shot.png", original_name="shot.png",
        size_bytes=1024, local_path="tracker/0001/1_shot.png",
    )
    await db._ensure_connection()
    async with db._write_lock:
        await db._conn.execute("DELETE FROM tracker_items WHERE item_number = ?", (item_number,))
        await db._conn.commit()
    assert await db.get_tracker_attachments(item_number) == []


# -- tracker_testcases ------------------------------------------------------

@pytest.mark.asyncio
async def test_set_and_get_tracker_testcases(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    await db.set_tracker_testcases(item_number, [
        {"environment": "DEV", "description": "Run /leaderboard during CWL round"},
        {"environment": "DEV", "description": "Advance round, re-run"},
        {"environment": "PROD", "description": "Same as DEV #1 on live clan"},
    ])
    cases = await db.get_tracker_testcases(item_number)
    assert len(cases) == 3
    dev_cases = [c for c in cases if c["environment"] == "DEV"]
    assert [c["seq"] for c in dev_cases] == [1, 2]
    assert all(c["passed"] == 0 for c in cases)


@pytest.mark.asyncio
async def test_set_tracker_testcases_replaces_existing_set(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "old"}])
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "new"}])
    cases = await db.get_tracker_testcases(item_number)
    assert len(cases) == 1
    assert cases[0]["description"] == "new"


@pytest.mark.asyncio
async def test_mark_tracker_testcase_passed(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "case 1"}])
    cases = await db.get_tracker_testcases(item_number)
    await db.mark_tracker_testcase_passed(cases[0]["id"], user_id="42")
    cases = await db.get_tracker_testcases(item_number)
    assert cases[0]["passed"] == 1
    assert cases[0]["passed_by"] == "42"
    assert cases[0]["passed_at"] is not None


@pytest.mark.asyncio
async def test_mark_tracker_environment_passed_only_affects_that_environment(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    await db.set_tracker_testcases(item_number, [
        {"environment": "DEV", "description": "d1"},
        {"environment": "DEV", "description": "d2"},
        {"environment": "PROD", "description": "p1"},
    ])
    await db.mark_tracker_environment_passed(item_number, "DEV", user_id="42")
    cases = await db.get_tracker_testcases(item_number)
    dev_cases = [c for c in cases if c["environment"] == "DEV"]
    prod_cases = [c for c in cases if c["environment"] == "PROD"]
    assert all(c["passed"] == 1 for c in dev_cases)
    assert all(c["passed"] == 0 for c in prod_cases)


@pytest.mark.asyncio
async def test_mark_tracker_environment_passed_does_not_reset_already_passed(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "d1"}])
    await db.mark_tracker_environment_passed(item_number, "DEV", user_id="1")
    cases = await db.get_tracker_testcases(item_number)
    first_passed_at = cases[0]["passed_at"]
    # Re-run against the same (already-passed) environment should be a no-op (WHERE passed = 0).
    await db.mark_tracker_environment_passed(item_number, "DEV", user_id="2")
    cases = await db.get_tracker_testcases(item_number)
    assert cases[0]["passed_by"] == "1"
    assert cases[0]["passed_at"] == first_passed_at


@pytest.mark.asyncio
async def test_tracker_testcases_cascade_delete_with_item(db):
    item_number = await db.create_tracker_item(
        item_type="bug", title="t", description="d", reporter_id="1", reporter_name="A"
    )
    await db.set_tracker_testcases(item_number, [{"environment": "DEV", "description": "d1"}])
    await db._ensure_connection()
    async with db._write_lock:
        await db._conn.execute("DELETE FROM tracker_items WHERE item_number = ?", (item_number,))
        await db._conn.commit()
    assert await db.get_tracker_testcases(item_number) == []
