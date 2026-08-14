"""Tests for qapbot/emojis.py's CDN-URL helpers (added for the CWL "Manage Enrollment" board,
which renders TH icons in plain HTML and so can't use Discord's native <:name:id> emoji markup)."""
from __future__ import annotations

from qapbot.emojis import BotEmojis, emoji_cdn_url, th_icon_url


def test_emoji_cdn_url_extracts_id_from_custom_emoji_string():
    assert emoji_cdn_url(BotEmojis.TH15) == "https://cdn.discordapp.com/emojis/1470128241271640075.png"


def test_emoji_cdn_url_returns_none_for_plain_unicode_emoji():
    assert emoji_cdn_url("⭐") is None


def test_emoji_cdn_url_returns_none_for_garbage_input():
    assert emoji_cdn_url("not an emoji") is None


def test_th_icon_url_resolves_known_level():
    assert th_icon_url(15) == emoji_cdn_url(BotEmojis.TH15)


def test_th_icon_url_returns_none_for_unknown_level():
    assert th_icon_url(99) is None
