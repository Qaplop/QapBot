"""/whois fetches every linked account in bounded parallel (2026-08-22 bug report).

It used to await one `asyncio.wait_for(CACHE.get_player(tag), timeout=8.0)` per account INSIDE
the formatting loop — fully serial. CACHE.get_player() is uncached (every call is a live API
round-trip), so a user with 82 linked accounts issued 82 serialised calls: 30s wall clock live,
and one call exceeding the 8s per-account budget produced an ERROR-level traceback per account.
"""
from __future__ import annotations

import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import QBdiscordcmds  # noqa: E402


def _fake_player(tag: str, th: int = 15):
    """A minimal stand-in for coc.Player as _whois_logic consumes it."""
    p = MagicMock()
    p.town_hall = th
    p.heroes = []
    p.clan = None
    p.role = None
    return p


def _mock_interaction():
    interaction = MagicMock()
    interaction.guild = None
    interaction.followup.send = AsyncMock()
    return interaction


def _mock_user(user_id: int = 555):
    user = MagicMock()
    user.id = user_id
    user.name = "tester"
    user.display_avatar.url = "https://example.invalid/a.png"
    return user


def _seed_accounts(monkeypatch, n: int, th: int = 15):
    players = [
        {"player_tag": f"#P{i}", "player_name": f"Player{i}", "verified": False,
         "is_primary": False, "th_level": th}
        for i in range(n)
    ]
    monkeypatch.setattr(QBdiscordcmds.CACHE, "user_accounts", {"555": {"players": players}})
    return players


@pytest.mark.discord
@pytest.mark.asyncio
async def test_accounts_are_fetched_concurrently_not_serially(monkeypatch):
    """The regression guard for the actual defect. 12 accounts x 0.1s serial would be >=1.2s;
    bounded-parallel (semaphore of 10) completes in roughly two waves. Also asserts peak
    concurrency actually exceeded 1 — an elapsed-time check alone could pass on a fast machine
    for the wrong reason."""
    _seed_accounts(monkeypatch, 12)

    in_flight = 0
    peak = 0

    async def _slow_get_player(tag):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.1)
            return _fake_player(tag)
        finally:
            in_flight -= 1

    monkeypatch.setattr(QBdiscordcmds.CACHE, "get_player", _slow_get_player)

    started = asyncio.get_running_loop().time()
    await QBdiscordcmds._whois_logic(_mock_interaction(), _mock_user())
    elapsed = asyncio.get_running_loop().time() - started

    assert peak > 1, "accounts were fetched one at a time"
    assert elapsed < 0.9, f"took {elapsed:.2f}s — looks serial rather than parallel"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_concurrency_is_bounded(monkeypatch):
    """Unbounded gather would spike the CoC rate limiter, which is shared with the background
    clan-poll cycle — hence the semaphore rather than a bare gather."""
    _seed_accounts(monkeypatch, 40)

    in_flight = 0
    peak = 0

    async def _slow_get_player(tag):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
            return _fake_player(tag)
        finally:
            in_flight -= 1

    monkeypatch.setattr(QBdiscordcmds.CACHE, "get_player", _slow_get_player)
    await QBdiscordcmds._whois_logic(_mock_interaction(), _mock_user())

    assert peak <= 10, f"peak concurrency {peak} exceeded the intended bound"


@pytest.mark.discord
@pytest.mark.asyncio
async def test_one_failing_account_does_not_abort_the_others(monkeypatch):
    """return_exceptions=True on the gather: a single bad tag must not take the command down."""
    _seed_accounts(monkeypatch, 4)

    async def _get_player(tag):
        if tag == "#P2":
            raise RuntimeError("boom")
        return _fake_player(tag)

    monkeypatch.setattr(QBdiscordcmds.CACHE, "get_player", _get_player)

    interaction = _mock_interaction()
    await QBdiscordcmds._whois_logic(interaction, _mock_user())

    interaction.followup.send.assert_awaited()
    rendered = " ".join(
        str(kwargs.get("embed").to_dict()) for _, kwargs in interaction.followup.send.call_args_list
    )
    assert "#P0" in rendered and "#P1" in rendered and "#P3" in rendered
    assert "#P2" in rendered  # rendered as the error row, not silently dropped


@pytest.mark.discord
@pytest.mark.asyncio
async def test_batch_timeout_renders_cached_rows_instead_of_raising(monkeypatch):
    """The whole-batch budget degrades to CACHE.user_accounts data (kept fresh by the clan-poll
    cycle) rather than failing the command — a degraded row beats an error row. Previously the
    per-account timeout path produced an '*Could not fetch player data*' row per account."""
    _seed_accounts(monkeypatch, 3, th=13)

    async def _never_returns(tag):
        await asyncio.sleep(60)

    monkeypatch.setattr(QBdiscordcmds.CACHE, "get_player", _never_returns)
    monkeypatch.setattr(QBdiscordcmds.asyncio, "wait_for", _fast_timeout_wait_for)

    interaction = _mock_interaction()
    await QBdiscordcmds._whois_logic(interaction, _mock_user())

    interaction.followup.send.assert_awaited()
    rendered = " ".join(
        str(kwargs.get("embed").to_dict()) for _, kwargs in interaction.followup.send.call_args_list
    )
    for tag in ("#P0", "#P1", "#P2"):
        assert tag in rendered, f"{tag} was dropped from the output entirely"
    assert "Live data unavailable" in rendered


async def _fast_timeout_wait_for(awaitable, timeout):
    """Stand-in for asyncio.wait_for that times out immediately, so the test doesn't wait 45s."""
    task = asyncio.ensure_future(awaitable)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    raise asyncio.TimeoutError()


@pytest.mark.discord
@pytest.mark.asyncio
async def test_timeout_does_not_log_at_error_level(monkeypatch, caplog):
    """Pitfall 24: a user with many accounts during a busy cycle emitted one ERROR + full stack
    trace per account for what is a routine timeout under API load."""
    _seed_accounts(monkeypatch, 2)

    async def _never_returns(tag):
        await asyncio.sleep(60)

    monkeypatch.setattr(QBdiscordcmds.CACHE, "get_player", _never_returns)
    monkeypatch.setattr(QBdiscordcmds.asyncio, "wait_for", _fast_timeout_wait_for)

    with caplog.at_level(logging.DEBUG):
        await QBdiscordcmds._whois_logic(_mock_interaction(), _mock_user())

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, f"timeout logged at ERROR level: {[r.getMessage() for r in errors]}"
