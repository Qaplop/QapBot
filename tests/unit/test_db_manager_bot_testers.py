"""Tests for the bot_testers table CRUD (DM-testing allowlist) and its CACHE.testers
load/write-through, added alongside /admin's MANAGE_TESTERS action (2026-08-15) — see
QBdiscocmdshelper_cwl.py's start_cwl_enrollment() DM guard for where CACHE.testers is
consumed (extended in tests/integration/test_cwl_start_enrollment.py).
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
async def test_add_and_get_testers_roundtrip(db):
    assert await db.get_testers() == []
    await db.add_tester("111")
    await db.add_tester("222")
    assert sorted(await db.get_testers()) == ["111", "222"]


@pytest.mark.asyncio
async def test_add_tester_is_idempotent(db):
    await db.add_tester("111")
    await db.add_tester("111")
    assert await db.get_testers() == ["111"]


@pytest.mark.asyncio
async def test_remove_tester(db):
    await db.add_tester("111")
    await db.add_tester("222")
    await db.remove_tester("111")
    assert await db.get_testers() == ["222"]


@pytest.mark.asyncio
async def test_remove_tester_not_enrolled_is_a_noop(db):
    await db.add_tester("111")
    await db.remove_tester("999")  # never enrolled
    assert await db.get_testers() == ["111"]


@pytest.mark.asyncio
async def test_cache_load_testers_populates_set(db, monkeypatch):
    from qapbot.cache_manager import CACHE

    monkeypatch.setattr(CACHE, "db_manager", db)
    await db.add_tester("111")
    await db.add_tester("222")

    await CACHE.load_testers()

    assert CACHE.testers == {"111", "222"}


@pytest.mark.asyncio
async def test_cache_add_tester_write_through(db, monkeypatch):
    from qapbot.cache_manager import CACHE

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "testers", set())

    await CACHE.add_tester("111")

    assert CACHE.testers == {"111"}
    assert await db.get_testers() == ["111"]


@pytest.mark.asyncio
async def test_cache_remove_tester_write_through(db, monkeypatch):
    from qapbot.cache_manager import CACHE

    monkeypatch.setattr(CACHE, "db_manager", db)
    monkeypatch.setattr(CACHE, "testers", {"111", "222"})
    await db.add_tester("111")
    await db.add_tester("222")

    await CACHE.remove_tester("111")

    assert CACHE.testers == {"222"}
    assert await db.get_testers() == ["222"]
