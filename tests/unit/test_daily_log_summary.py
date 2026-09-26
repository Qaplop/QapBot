"""Tracker #0144: daily error/warning summary DM for the bot admin."""
from __future__ import annotations

import dataclasses
import os
from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import clashcontrol.daily_log_summary as dls  # noqa: E402

DAY = date(2026, 9, 25)


def _write_log(tmp_path, lines):
    path = tmp_path / "clashcontrol.log.2026-09-25"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_groups_by_pattern_attaches_traceback_and_ignores_other_days(tmp_path):
    path = _write_log(tmp_path, [
        "2026-09-24 23:59:59,000 [ERROR] yesterday's error must not count",
        "2026-09-25 00:01:00,123 [INFO] Version: 1.3.39  build 118  src abc",
        "2026-09-25 01:00:00,000 [WARNING] [LOOP-LAG] Event loop stalled 0.83s",
        "2026-09-25 02:30:00,000 [WARNING] [LOOP-LAG] Event loop stalled 1.20s",
        "2026-09-25 03:00:00,000 [ERROR] Error handling request from 127.0.0.1",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in f',
        "RuntimeError: [DB-MAINT] Database closed for maintenance",
        "2026-09-25 04:00:00,000 [ERROR] clan #2PP8Q0 failed for guild 1145641080621109312",
        "2026-09-25 05:00:00,000 [ERROR] clan #L2J0C0PY failed for guild 1394733938961944787",
        "2026-09-25 06:00:00,000 [INFO] Version: 1.3.39  build 119  src def",
    ])
    s = dls.summarize_log_day(str(tmp_path), DAY, files=[path])

    assert s.starts == 2 and s.last_build == "119"
    assert s.warning_count == 2 and len(s.warnings) == 1          # 0.83s / 1.20s → one pattern
    lag = next(iter(s.warnings.values()))
    assert (lag.first, lag.last, lag.count) == ("01:00", "02:30", 2)
    assert s.error_count == 3 and len(s.errors) == 2              # tags + snowflakes normalized
    req = next(g for g in s.errors.values() if "Error handling request" in g.sample)
    assert req.detail.startswith("RuntimeError: [DB-MAINT]")


def test_report_length_is_bounded_on_a_disastrous_day(tmp_path):
    lines = [f"2026-09-25 10:00:00,000 [ERROR] distinct failure kind {chr(65 + i % 26)}{chr(65 + i // 26 % 26)} " + "x" * 200
             for i in range(5000)]
    s = dls.summarize_log_day(str(tmp_path), DAY, files=[_write_log(tmp_path, lines)])
    embed = dls.build_summary_embed(s, "PROD", None)
    assert len(embed.description or "") <= dls.MAX_REPORT_CHARS + 5
    assert len(embed) <= 6000
    assert "more distinct" in (embed.description or "")


def test_clean_and_empty_days():
    clean = dls.DaySummary(day=DAY, lines_seen=10)
    assert "clean day" in (dls.build_summary_embed(clean, "PROD", None).description or "")
    empty = dls.DaySummary(day=DAY)
    assert "No log lines" in (dls.build_summary_embed(empty, "PROD", None).description or "")


def test_due_day_waits_for_the_rotation():
    assert dls.due_day(datetime(2026, 9, 26, 0, 4)) == date(2026, 9, 24)
    assert dls.due_day(datetime(2026, 9, 26, 0, 5)) == date(2026, 9, 25)
    assert dls.due_day(datetime(2026, 9, 26, 18, 0)) == date(2026, 9, 25)


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    import QBcore
    from clashcontrol.cache_manager import CACHE
    from clashcontrol.config import CONFIG

    store: dict = {}
    db = AsyncMock()
    db.get_bot_metadata = AsyncMock(side_effect=lambda k: store.get(k))
    db.set_bot_metadata = AsyncMock(side_effect=lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(CACHE, "db_manager", db)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(CACHE, "send_user_dm", send)
    monkeypatch.setattr(QBcore, "maintenance_mode", False)
    monkeypatch.setattr("clashcontrol.config.CONFIG", dataclasses.replace(CONFIG, server_admin="495306744231231488"))
    _write_log(tmp_path, ["2026-09-25 10:00:00,000 [ERROR] boom"])
    return store, send, str(tmp_path)


@pytest.mark.asyncio
async def test_sends_once_per_day(fake_env):
    store, send, log_dir = fake_env
    now = datetime(2026, 9, 26, 0, 6)
    assert await dls.send_due_summary(log_dir, now) == "sent"
    assert store[dls.METADATA_KEY] == "2026-09-25"
    assert send.await_args.args[0] == "495306744231231488"
    assert await dls.send_due_summary(log_dir, now) == "already_sent"
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_skips_during_maintenance(fake_env, monkeypatch):
    import QBcore
    _store, send, log_dir = fake_env
    monkeypatch.setattr(QBcore, "maintenance_mode", True)
    assert await dls.send_due_summary(log_dir, datetime(2026, 9, 26, 1, 0)) == "maintenance"
    send.assert_not_awaited()


def test_enabled_by_default_on_prod_only():
    from clashcontrol.config import CONFIG
    if os.getenv("DAILY_LOG_SUMMARY_ENABLED") is not None:
        pytest.skip("DAILY_LOG_SUMMARY_ENABLED is set explicitly in this environment")
    assert CONFIG.daily_log_summary_enabled is (not CONFIG.is_dev_mode)
