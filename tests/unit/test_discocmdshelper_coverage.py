"""Tests for qapbot/QBdiscocmdshelper.py — Phase 3 coverage.

Covers: _split_message_into_chunks, _split_embed_by_description,
normalize_clan_tag, normalize_family_tag, _get_clan_tag,
generate_family_tag, _build_autocomplete_choices, is_player_in_member_clans.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportUnusedImport=false, reportUnusedVariable=false
# pyright: reportOperatorIssue=false, reportOptionalMemberAccess=false
# pyright: reportArgumentType=false
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import discord
import pytest


# ---------------------------------------------------------------------------
# _split_message_into_chunks
# ---------------------------------------------------------------------------

class TestSplitMessageIntoChunks:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import _split_message_into_chunks
        return _split_message_into_chunks

    def test_short_message_single_chunk(self):
        result = self._fn()("Hello world", 100)
        assert result == ["Hello world"]

    def test_exactly_at_limit(self):
        msg = "A" * 50
        result = self._fn()(msg, 50)
        assert result == [msg]

    def test_non_code_block_split(self):
        lines = [f"Line {i}" for i in range(20)]
        content = "\n".join(lines)
        result = self._fn()(content, 40)
        assert len(result) > 1
        reassembled = "\n".join(result)
        assert "Line 0" in reassembled
        assert "Line 19" in reassembled

    def test_code_block_wrapping(self):
        inner = "\n".join([f"code {i}" for i in range(20)])
        content = f"```{inner}```"
        result = self._fn()(content, 60)
        assert len(result) > 1
        for chunk in result:
            assert chunk.startswith("```")
            assert chunk.endswith("```")

    def test_empty_string(self):
        result = self._fn()("", 100)
        assert result == [""]

    def test_single_long_line_non_code(self):
        line = "A" * 200
        result = self._fn()(line, 100)
        # Single line can't be split by newline — goes into one chunk
        assert len(result) >= 1
        assert "A" * 200 in result[0]


# ---------------------------------------------------------------------------
# _split_embed_by_description
# ---------------------------------------------------------------------------

class TestSplitEmbedByDescription:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import _split_embed_by_description
        return _split_embed_by_description

    def test_small_embed_returns_single(self):
        embed = discord.Embed(description="short text", color=0xFF0000)
        result = self._fn()(embed)
        assert len(result) == 1
        assert result[0] is embed

    def test_no_description_returns_single(self):
        embed = discord.Embed(color=0x00FF00)
        result = self._fn()(embed)
        assert len(result) == 1

    def test_large_embed_splits(self):
        desc = "\n".join([f"Line {i}: {'x' * 50}" for i in range(100)])
        embed = discord.Embed(description=desc, color=0x0000FF)
        embed.set_author(name="TestAuthor")
        embed.title = "TestTitle"
        embed.set_footer(text="TestFooter")
        result = self._fn()(embed, max_size=500)
        assert len(result) > 1
        # First embed gets author and title
        assert result[0].author.name == "TestAuthor"
        assert result[0].title == "TestTitle"
        # Last embed gets original footer
        assert "TestFooter" in result[-1].footer.text
        # All have part text
        for i, e in enumerate(result):
            assert f"Part {i+1}" in e.footer.text

    def test_color_preserved(self):
        desc = "\n".join([f"data {'x' * 100}" for _ in range(50)])
        embed = discord.Embed(description=desc, color=0xABCDEF)
        result = self._fn()(embed, max_size=300)
        for e in result:
            assert e.color.value == 0xABCDEF


# ---------------------------------------------------------------------------
# normalize_clan_tag
# ---------------------------------------------------------------------------

class TestNormalizeClanTag:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import normalize_clan_tag
        return normalize_clan_tag

    def test_valid_tag_with_hash(self):
        assert self._fn()("#L2J0C0PY") == "#L2J0C0PY"

    def test_adds_hash(self):
        assert self._fn()("L2J0C0PY") == "#L2J0C0PY"

    def test_lowercased(self):
        assert self._fn()("l2j0c0py") == "#L2J0C0PY"

    def test_replaces_o_with_zero(self):
        assert self._fn()("#L2JOCOPY") == "#L2J0C0PY"

    def test_strips_whitespace(self):
        assert self._fn()("  #L2J0C0PY  ") == "#L2J0C0PY"

    def test_too_short_returns_none(self):
        assert self._fn()("#AB") is None

    def test_too_long_returns_none(self):
        assert self._fn()("#ABCDEFGHIJK") is None

    def test_invalid_chars_returns_none(self):
        assert self._fn()("#ABC!@#$") is None

    def test_five_chars_valid(self):
        assert self._fn()("#ABCDE") == "#ABCDE"

    def test_ten_chars_valid(self):
        # 10 chars after # is valid per regex {5,10}
        assert self._fn()("#ABCDE12345") == "#ABCDE12345"
        # 11 chars after # is invalid
        assert self._fn()("#ABCDEFGHIJK") is None


# ---------------------------------------------------------------------------
# normalize_family_tag
# ---------------------------------------------------------------------------

class TestNormalizeFamilyTag:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import normalize_family_tag
        return normalize_family_tag

    def test_valid_ten_chars(self):
        assert self._fn()("#34570457B9") == "#34570457B9"

    def test_adds_hash(self):
        assert self._fn()("ABCDEF1234") == "#ABCDEF1234"

    def test_replaces_o(self):
        assert self._fn()("#ABCDEFO234") == "#ABCDEF0234"

    def test_nine_chars_invalid(self):
        assert self._fn()("#ABCDEFGH9") is None

    def test_eleven_chars_invalid(self):
        assert self._fn()("#ABCDEFGHIJK") is None


# ---------------------------------------------------------------------------
# _get_clan_tag
# ---------------------------------------------------------------------------

class TestGetClanTag:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import _get_clan_tag
        return _get_clan_tag

    def test_direct_tag_match(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {"#ABC12345": {"name": "TestClan"}}
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        count, tag = self._fn()("#ABC12345")
        assert count == 1
        assert tag == "#ABC12345"

    def test_name_substring_match(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {
            "#C1": {"name": "Alpha Wolves"},
            "#C2": {"name": "Beta Pack"},
        }
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        count, tag = self._fn()("Alpha")
        assert count == 1
        assert tag == "#C1"

    def test_multiple_name_matches(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {
            "#C1": {"name": "War Wolves"},
            "#C2": {"name": "War Eagles"},
        }
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        count, tag = self._fn()("War")
        assert count == 2

    def test_no_match(self, monkeypatch):
        cache = MagicMock()
        cache.clan_name_cache = {"#C1": {"name": "Test"}}
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        count, tag = self._fn()("Nonexistent")
        assert count == 0
        assert tag is None


# ---------------------------------------------------------------------------
# generate_family_tag
# ---------------------------------------------------------------------------

class TestGenerateFamilyTag:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import generate_family_tag
        return generate_family_tag

    def test_returns_hash_prefixed(self):
        tag = self._fn()("TestFamily", ["#C1", "#C2"])
        assert tag.startswith("#")

    def test_length_eleven(self):
        tag = self._fn()("Fam", ["#T1"])
        assert len(tag) == 11  # # + 10

    def test_alphanumeric_only(self):
        tag = self._fn()("Test!", ["#A", "#B"])
        assert re.match(r"^#[A-Z0-9]{10}$", tag)

    def test_deterministic_same_inputs(self):
        # Same inputs at same time should give same result
        # (timestamp differs, so technically non-deterministic;
        #  just verify format)
        tag = self._fn()("X", ["#Y"])
        assert len(tag) == 11


# ---------------------------------------------------------------------------
# _build_autocomplete_choices
# ---------------------------------------------------------------------------

class TestBuildAutocompleteChoices:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import _build_autocomplete_choices
        return _build_autocomplete_choices

    def test_no_filter(self):
        items = [("v1", "Alpha"), ("v2", "Beta")]
        result = self._fn()(items, "")
        assert len(result) == 2

    def test_filter_by_current(self):
        items = [("v1", "Alpha"), ("v2", "Beta"), ("v3", "Gamma")]
        result = self._fn()(items, "al")
        assert len(result) == 1
        assert result[0].value == "v1"

    def test_max_choices_limit(self):
        items = [(f"v{i}", f"Item{i}") for i in range(50)]
        result = self._fn()(items, "", max_choices=10)
        assert len(result) == 10

    def test_label_truncated_to_100(self):
        items = [("v1", "A" * 200)]
        result = self._fn()(items, "")
        assert len(result[0].name) <= 100

    def test_none_current_treated_as_empty(self):
        items = [("v1", "Test")]
        result = self._fn()(items, None)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# is_player_in_member_clans
# ---------------------------------------------------------------------------

class TestIsPlayerInMemberClans:
    def _fn(self):
        from qapbot.QBdiscocmdshelper import is_player_in_member_clans
        return is_player_in_member_clans

    def test_none_tag_returns_false(self, monkeypatch):
        cache = MagicMock()
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()(None, 12345) is False

    def test_empty_tag_returns_false(self, monkeypatch):
        cache = MagicMock()
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("", 12345) is False

    def test_in_member_clans(self, monkeypatch):
        cache = MagicMock()
        cache.server_config = {"12345": {"member_clans": ["#C1", "#C2"], "member_families": []}}
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#C1", 12345) is True

    def test_in_family_clans(self, monkeypatch):
        cache = MagicMock()
        cache.server_config = {"12345": {"member_clans": [], "member_families": ["#FAM1"]}}
        cache.clan_families = {"#FAM1": {"clans": ["#C1", "#C2"]}}
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#C2", 12345) is True

    def test_not_found(self, monkeypatch):
        cache = MagicMock()
        cache.server_config = {"12345": {"member_clans": ["#C1"], "member_families": []}}
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#NOTFOUND", 12345) is False

    def test_no_config_for_guild(self, monkeypatch):
        cache = MagicMock()
        cache.server_config = {}
        monkeypatch.setattr("qapbot.QBdiscocmdshelper.CACHE", cache)
        assert self._fn()("#C1", 99999) is False
