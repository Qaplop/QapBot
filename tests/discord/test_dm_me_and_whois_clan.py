"""Tracker #0119 (/dm me, /admin DM User — greeting DM) and #0120 (/whois clan)."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

import QBdiscordcmds  # noqa: E402
from clashcontrol.db_manager import WarHistoryDB  # noqa: E402


# ---------------------------------------------------------------------------
# #0119 — /dm me and the greeting DM
# ---------------------------------------------------------------------------

def test_dm_group_registered_and_dm_invokable():
    assert {c.name for c in QBdiscordcmds.dm_group.commands} == {"me"}
    assert QBdiscordcmds.dm_me.guild_only is False
    assert "dm me" in QBdiscordcmds._get_help_command_names()
    assert QBdiscordcmds._get_help_command_dm_status()["dm me"] is False


def test_greeting_embed_resolves_every_text():
    from clashcontrol.greeting_dm import build_greeting_dm_embed

    embed = build_greeting_dm_embed("1", None, "Qap")
    texts = [embed.title or "", embed.description or "", embed.footer.text or ""]
    texts += [f.name for f in embed.fields] + [f.value for f in embed.fields]
    joined = "\n".join(texts)
    assert "Qap" in (embed.title or "")
    # A missing key comes back as the raw dotted key — none may leak into the DM.
    assert "commands.dm." not in joined and "activity.landing." not in joined
    assert "/help" in joined or "</help:" in joined
    assert len(embed) <= 6000


def test_greeting_dm_has_install_and_support_buttons(monkeypatch):
    """Tracker #0141: the DM carries the landing page's "Add to server" + support link buttons."""
    import QBcore
    from clashcontrol.constants import SUPPORT_INVITE_URL
    from clashcontrol.greeting_dm import build_greeting_dm_embed, build_greeting_dm_view

    monkeypatch.setattr(QBcore, "bot", MagicMock(application_id=1234))
    urls = [b.url for b in build_greeting_dm_view("1", None).children]
    assert urls == ["https://discord.com/oauth2/authorize?client_id=1234", SUPPORT_INVITE_URL]
    assert any("➕" in f.name for f in build_greeting_dm_embed("1", None, "Qap").fields)


def test_help_texts_tracker_142_143():
    import json
    from pathlib import Path

    en = json.loads((Path(QBdiscordcmds.__file__).parent / "clashcontrol/translations/en.json").read_text(encoding="utf-8"))
    helps = en["commands"]["help"]
    assert helps["analyse cwl_opponent"]["short"] == "Analyse a CWL opponent clan's roster"
    assert helps["whois"]["short"] == "Look up of Discord and CoC users and clan stats"
    assert "Missing your language?" in helps["feature"]["detailed"]


@pytest.mark.asyncio
async def test_dm_me_confirms_with_jump_link(mock_interaction, monkeypatch):
    sent = MagicMock()
    sent.jump_url = "https://discord.com/channels/@me/1/2"
    send = AsyncMock(return_value=("sent", sent))
    monkeypatch.setattr("clashcontrol.greeting_dm.send_greeting_dm", send)

    await QBdiscordcmds.dm_me.callback(mock_interaction)  # type: ignore[arg-type]

    send.assert_awaited_once()
    call = mock_interaction.followup.send.await_args
    assert sent.jump_url in call.args[0]
    assert call.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_dm_me_explains_blocked_dms(mock_interaction, monkeypatch):
    monkeypatch.setattr("clashcontrol.greeting_dm.send_greeting_dm", AsyncMock(return_value=("blocked", None)))

    await QBdiscordcmds.dm_me.callback(mock_interaction)  # type: ignore[arg-type]

    text = mock_interaction.followup.send.await_args.args[0]
    assert text.startswith("❌")


@pytest.mark.asyncio
async def test_admin_dm_user_is_server_only(mock_interaction):
    mock_interaction.guild = None
    mock_interaction.guild_id = None

    await QBdiscordcmds.admin.callback(mock_interaction, action="DM_USER")  # type: ignore[arg-type]

    call = mock_interaction.followup.send.await_args
    assert "view" not in call.kwargs
    assert call.kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# #0120 — /whois clan
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    manager = WarHistoryDB()
    await manager.initialize(str(tmp_path / "whois_clan.db"))
    try:
        yield manager
    finally:
        await manager.close()


def _insert_war(conn, schema: str, war_id: str, is_cwl: int, result: str, date: str, season: str = "") -> None:
    conn.execute(
        f"INSERT INTO {schema}.war_summary (war_id, clan_tag, opponent_tag, is_cwl, cwl_season, result, date)"
        " VALUES (?, '#CLAN', '#OPP', ?, ?, ?, ?)",
        (war_id, is_cwl, season, result, date),
    )


@pytest.mark.asyncio
async def test_clan_whois_stats_sum_main_and_history(db):
    with db._sync_conn() as conn:
        conn.execute("INSERT INTO main.clans (clan_tag, name, war_league) VALUES ('#CLAN', 'Clan', 'Crystal League I')")
        _insert_war(conn, "main", "w1", 0, "win", "2026-09-01")
        _insert_war(conn, "main", "w2", 1, "loss", "2026-09-05", "2026-09")
        _insert_war(conn, "history", "w0", 0, "draw", "2025-01-10")
        _insert_war(conn, "history", "w00", 1, "win", "2025-02-03", "2025-02")
        conn.commit()

    stats = db.get_clan_whois_stats_sync("#CLAN")

    assert stats["clan"]["name"] == "Clan" and stats["clan"]["war_league"] == "Crystal League I"
    assert stats["cw"] == {"wars": 2, "wins": 1, "losses": 0, "draws": 1, "unknown": 0}
    assert stats["cwl"] == {"wars": 2, "wins": 1, "losses": 1, "draws": 0, "unknown": 0}
    assert stats["first_war"] == "2025-01-10" and stats["last_war"] == "2026-09-05"
    assert stats["cwl_seasons"] == 2


@pytest.mark.asyncio
async def test_clan_whois_stats_unknown_clan(db):
    stats = db.get_clan_whois_stats_sync("#NOPE")
    assert stats["clan"] is None and stats["cw"]["wars"] == 0 and stats["first_war"] == ""


@pytest.mark.asyncio
async def test_created_at_pulled_back_to_first_war(db):
    """Nightly step 0.8: created_at moves back to the first war (main or history, same-day
    comparisons included), never forward, and a second run changes nothing."""
    with db._sync_conn() as conn:
        conn.execute("INSERT INTO main.clans (clan_tag, name, created_at) VALUES ('#CLAN', 'Clan', '2026-02-17 19:14:54')")
        conn.execute("INSERT INTO main.clans (clan_tag, name, created_at) VALUES ('#SAME', 'Same', '2026-03-01 19:00:00')")
        conn.execute("INSERT INTO main.clans (clan_tag, name, created_at) VALUES ('#OLD', 'Old', '2025-01-01 00:00:00')")
        _insert_war(conn, "history", "w0", 0, "win", "2025-11-20T20:18")
        _insert_war(conn, "main", "w1", 0, "win", "2026-09-01T10:00")
        conn.execute("INSERT INTO main.war_summary (war_id, clan_tag, opponent_tag, date) VALUES ('s1', '#SAME', '#X', '2026-03-01T08:30')")
        conn.execute("INSERT INTO main.war_summary (war_id, clan_tag, opponent_tag, date) VALUES ('o1', '#OLD', '#X', '2025-12-01T08:30')")
        conn.commit()

    assert db.backfill_clan_created_at_from_first_war_sync() == 2
    assert db.backfill_clan_created_at_from_first_war_sync() == 0
    with db._sync_conn() as conn:
        got = {r["clan_tag"]: r["created_at"] for r in conn.execute("SELECT clan_tag, created_at FROM clans")}
    assert got == {"#CLAN": "2025-11-20 20:18:00", "#SAME": "2026-03-01 08:30:00", "#OLD": "2025-01-01 00:00:00"}


@pytest.mark.asyncio
async def test_every_clan_write_stamps_updated_at(db):
    """save_clan's upsert and all three bulk updates refresh updated_at; created_at stays."""
    old = "2026-01-01 00:00:00"

    def _reset() -> None:
        with db._sync_conn() as conn:
            conn.execute("UPDATE clans SET updated_at = ?, created_at = ?", (old, old))
            conn.commit()

    def _row() -> dict:
        with db._sync_conn() as conn:
            r = conn.execute("SELECT created_at, updated_at FROM clans WHERE clan_tag = '#CLAN'").fetchone()
            return {"created_at": r["created_at"], "updated_at": r["updated_at"]}

    await db.save_clan("#CLAN", "Clan")
    for write in (
        lambda: db.save_clan("#CLAN", "Clan renamed"),
        lambda: db.bulk_update_clan_subscription_statuses([(True, "#CLAN")]),
        lambda: db.bulk_update_clan_track_war_updates(["#CLAN"]),
        lambda: db.bulk_update_clan_timestamps([("2026-09-26T10:00:00+00:00", "#CLAN")]),
    ):
        _reset()
        await write()
        row = _row()
        assert row["created_at"] == old
        assert row["updated_at"] > old


def test_leaderboard_text_keeps_plain_sections_outside_code():
    from QBhelperfunctions import _PLAIN_SENTINEL_START, _PLAIN_SENTINEL_END

    text = f"header\n{_PLAIN_SENTINEL_START}<:emoji:1> roster{_PLAIN_SENTINEL_END}table row"
    messages = QBdiscordcmds._whois_leaderboard_text_to_messages(text, "CUT")
    assert len(messages) == 1
    content = messages[0]
    assert "```ansi\nheader\n```" in content
    assert "\n<:emoji:1> roster\n" in content
    assert "```ansi\ntable row\n```" in content
    assert "\x00" not in content and "CUT" not in content


def test_long_leaderboard_is_split_into_capped_messages():
    """Tracker #0140: message content (full width), each <= 2000 chars, at most 5 messages; the
    rest is cut with the hint pointing to /leaderboard."""
    text = "\n".join(f"{i:04d} " + "x" * 60 for i in range(400))  # ~26k chars
    messages = QBdiscordcmds._whois_leaderboard_text_to_messages(text, "CUT")
    assert len(messages) == QBdiscordcmds._WHOIS_CLAN_MAX_MESSAGES
    assert all(len(m) <= QBdiscordcmds._WHOIS_CLAN_MESSAGE_LIMIT for m in messages)
    assert all(m.count("```") % 2 == 0 for m in messages)  # every code block is closed
    assert messages[-1].endswith("CUT")

    short = QBdiscordcmds._whois_leaderboard_text_to_messages("\n".join("row" for _ in range(40)), "CUT")
    assert len(short) == 1 and not short[0].endswith("CUT")


@pytest.mark.asyncio
async def test_clan_view_replaces_previous_continuation_messages(mock_interaction, monkeypatch):
    """A long board's extra messages go away on the next pick (tracker #0140)."""
    from unittest.mock import AsyncMock as _AM

    view = QBdiscordcmds._WhoisClanView("#CLAN", MagicMock(), "1", None, set())
    old = MagicMock()
    old.delete = _AM()
    view.extra_messages = [old]
    monkeypatch.setattr(QBdiscordcmds, "_render_whois_clan_mode", _AM(return_value=(["part1", "part2"], [], [])))
    mock_interaction.data = {"values": ["attack"]}
    mock_interaction.followup.send = _AM(return_value=MagicMock())

    await view._on_select(mock_interaction)

    old.delete.assert_awaited_once()
    kwargs = mock_interaction.edit_original_response.await_args.kwargs
    assert kwargs["content"] == "part1" and kwargs["embeds"] == []
    mock_interaction.followup.send.assert_awaited_once_with("part2", ephemeral=True, wait=True)
    assert len(view.extra_messages) == 1


def test_clan_view_offers_overview_and_every_mode():
    from clashcontrol.formatting import MODE_REGISTRY

    view = QBdiscordcmds._WhoisClanView("#CLAN", MagicMock(), "1", None, set())
    values = [o.value for o in view.select.options]
    assert values[0] == QBdiscordcmds._WHOIS_CLAN_OVERVIEW
    assert values[1:] == list(MODE_REGISTRY.keys())
    assert len(values) <= 25


@pytest.mark.asyncio
async def test_whois_clan_untracked_is_refused(mock_interaction, monkeypatch):
    monkeypatch.setattr(QBdiscordcmds.CACHE, "clan_name_cache", {})

    await QBdiscordcmds.whois_slash.callback(mock_interaction, clan="#2PP")  # type: ignore[arg-type]

    call = mock_interaction.followup.send.await_args
    assert call.kwargs.get("ephemeral") is True
    assert "#2PP" in call.args[0]
    mock_interaction.edit_original_response.assert_not_awaited()
