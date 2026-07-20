"""Extended tests for qapbot/i18n.py — TranslationManager and helper functions.

Covers:
- TranslationManager singleton, load/reload, nested value lookup
- get_available_languages
- get_guild_language with CACHE fallback
- get_user_language with CACHE fallback
- set_guild_language validation
- Language fallback chain
"""
# pyright: reportPrivateUsage=false, reportUnusedImport=false
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from qapbot.i18n import (
    TranslationManager,
    t,
    get_guild_language,
    get_user_language,
    set_guild_language,
    reload_translations,
    get_available_languages,
)


# ---------------------------------------------------------------------------
# TranslationManager internals
# ---------------------------------------------------------------------------

class TestTranslationManagerInternals:
    def test_get_nested_value_deep(self):
        tm = TranslationManager()
        data = {"a": {"b": {"c": "found"}}}
        assert tm._get_nested_value(data, "a.b.c") == "found"

    def test_get_nested_value_missing(self):
        tm = TranslationManager()
        assert tm._get_nested_value({"a": 1}, "a.b.c") is None

    def test_get_nested_value_non_string_leaf(self):
        tm = TranslationManager()
        data = {"a": {"b": {"c": 42}}}
        # Should return None for non-str leaves
        assert tm._get_nested_value(data, "a.b.c") is None

    def test_get_nested_value_empty_dict(self):
        tm = TranslationManager()
        assert tm._get_nested_value({}, "a") is None


class TestTranslationManagerLoading:
    def test_load_creates_dir_if_missing(self, tmp_path: Path):
        tm = TranslationManager()
        tm.translations_dir = tmp_path / "nonexistent"
        tm._loaded = False
        tm.load_translations()
        # Dir is created, but since it's empty, _loaded stays False (early return)
        assert (tmp_path / "nonexistent").exists()

    def test_load_reads_json_files(self, tmp_path: Path):
        # Create a fake translation file
        (tmp_path / "test_lang.json").write_text(
            json.dumps({"greet": {"hello": "Hola"}}), encoding="utf-8"
        )
        tm = TranslationManager()
        tm.translations_dir = tmp_path
        tm._loaded = False
        tm.load_translations()
        assert "test_lang" in tm.translations
        assert tm.translations["test_lang"]["greet"]["hello"] == "Hola"

    def test_load_skips_malformed_json(self, tmp_path: Path):
        (tmp_path / "bad.json").write_text("NOT JSON", encoding="utf-8")
        (tmp_path / "good.json").write_text(json.dumps({"k": "v"}), encoding="utf-8")
        tm = TranslationManager()
        tm.translations_dir = tmp_path
        tm._loaded = False
        tm.load_translations()
        assert "good" in tm.translations
        assert "bad" not in tm.translations

    def test_reload_clears_and_reloads(self, tmp_path: Path):
        (tmp_path / "en.json").write_text(json.dumps({"k": "val1"}), encoding="utf-8")
        tm = TranslationManager()
        tm.translations_dir = tmp_path
        tm._loaded = False
        tm.load_translations()
        assert tm.translations["en"]["k"] == "val1"

        # Update file
        (tmp_path / "en.json").write_text(json.dumps({"k": "val2"}), encoding="utf-8")
        tm.reload_translations()
        assert tm.translations["en"]["k"] == "val2"


class TestGetTranslation:
    def test_fallback_to_default_language(self, tmp_path: Path):
        (tmp_path / "en.json").write_text(json.dumps({"msg": "Hello"}), encoding="utf-8")
        tm = TranslationManager()
        tm.translations_dir = tmp_path
        tm._loaded = False
        tm.load_translations()
        # Request in "fr" which doesn't exist — falls back to "en"
        result = tm.get_translation("msg", "fr")
        assert result == "Hello"

    def test_returns_key_when_not_found(self):
        tm = TranslationManager()
        tm.translations = {"en": {}}
        tm._loaded = True
        result = tm.get_translation("nonexistent.key", "en")
        assert result == "nonexistent.key"

    def test_interpolation(self, tmp_path: Path):
        (tmp_path / "en.json").write_text(
            json.dumps({"greet": "Hello, {name}!"}), encoding="utf-8"
        )
        tm = TranslationManager()
        tm.translations_dir = tmp_path
        tm._loaded = False
        tm.load_translations()
        result = tm.get_translation("greet", "en", name="World")
        assert result == "Hello, World!"

    def test_missing_interpolation_variable(self, tmp_path: Path):
        (tmp_path / "en.json").write_text(
            json.dumps({"greet": "Hello, {name}!"}), encoding="utf-8"
        )
        tm = TranslationManager()
        tm.translations_dir = tmp_path
        tm._loaded = False
        tm.load_translations()
        # Missing 'name' variable — returns template as-is
        result = tm.get_translation("greet", "en")
        assert result == "Hello, {name}!"

    def test_lazy_load_on_first_call(self, tmp_path: Path):
        (tmp_path / "en.json").write_text(json.dumps({"k": "v"}), encoding="utf-8")
        tm = TranslationManager()
        tm.translations_dir = tmp_path
        tm._loaded = False  # Not loaded yet
        result = tm.get_translation("k", "en")
        assert result == "v"
        assert tm._loaded is True


class TestGetAvailableLanguages:
    def test_returns_loaded_languages(self, tmp_path: Path):
        for lang in ["en", "de", "es"]:
            (tmp_path / f"{lang}.json").write_text(json.dumps({}), encoding="utf-8")
        tm = TranslationManager()
        tm.translations_dir = tmp_path
        tm._loaded = False
        tm.load_translations()
        langs = tm.get_available_languages()
        assert set(langs) == {"en", "de", "es"}


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestGetGuildLanguage:
    def test_none_guild_id_returns_default(self):
        result = get_guild_language(None)
        assert result == "en"

    def test_guild_with_language_in_cache(self, monkeypatch: pytest.MonkeyPatch):
        mock_cache = MagicMock()
        mock_cache.server_config = {"999": {"language": "de"}}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            result = get_guild_language(999)
        assert result == "de"

    def test_guild_without_language_returns_default(self, monkeypatch: pytest.MonkeyPatch):
        mock_cache = MagicMock()
        mock_cache.server_config = {"999": {}}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            result = get_guild_language(999)
        assert result == "en"


class TestGetUserLanguage:
    def test_none_user_id_returns_none(self):
        result = get_user_language(None)
        assert result is None

    def test_user_with_language_preference(self, monkeypatch: pytest.MonkeyPatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {"42": {"user_language": "fr"}}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            result = get_user_language("42")
        assert result == "fr"

    def test_user_without_language_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {"42": {}}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            result = get_user_language("42")
        assert result is None

    def test_user_not_in_accounts_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        mock_cache = MagicMock()
        mock_cache.user_accounts = {}
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            result = get_user_language("999")
        assert result is None


class TestSetGuildLanguage:
    @pytest.mark.asyncio
    async def test_valid_language_sets_and_persists(self, monkeypatch: pytest.MonkeyPatch):
        tm = TranslationManager.get_instance()
        tm.translations = {"en": {}, "de": {}}
        tm._loaded = True

        mock_cache = MagicMock()
        mock_cache.server_config = {}
        mock_cache.persist_server_config = AsyncMock()
        with patch("qapbot.cache_manager.CACHE", mock_cache):
            result = await set_guild_language(123, "de")
        assert result is True
        assert mock_cache.server_config["123"]["language"] == "de"
        mock_cache.persist_server_config.assert_awaited_once_with("123")

    @pytest.mark.asyncio
    async def test_invalid_language_returns_false(self):
        tm = TranslationManager.get_instance()
        tm.translations = {"en": {}}
        tm._loaded = True

        result = await set_guild_language(123, "klingon")
        assert result is False


class TestModuleLevelFunctions:
    def test_reload_translations_delegates(self, tmp_path: Path):
        tm = TranslationManager.get_instance()
        tm.translations_dir = tmp_path
        (tmp_path / "en.json").write_text(json.dumps({"k": "v"}), encoding="utf-8")
        reload_translations()
        assert "en" in tm.translations

    def test_get_available_languages_delegates(self):
        tm = TranslationManager.get_instance()
        tm.translations = {"en": {}, "de": {}}
        tm._loaded = True
        assert set(get_available_languages()) == {"en", "de"}
