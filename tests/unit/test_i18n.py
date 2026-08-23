from __future__ import annotations

import pytest


class _FakeCache:
    def __init__(self):
        self.server_config = {}
        self.user_accounts = {}

    async def persist_server_config(self, guild_id_str: str) -> None:
        return None


@pytest.mark.smoke
def test_t_returns_english_by_default(monkeypatch: pytest.MonkeyPatch):
    import qapbot.cache_manager
    from qapbot.i18n import t

    monkeypatch.setattr(qapbot.cache_manager, "CACHE", _FakeCache())
    assert t("common.errors.not_found") == "Not found"


@pytest.mark.smoke
def test_t_interpolates_variables(monkeypatch: pytest.MonkeyPatch):
    import qapbot.cache_manager
    from qapbot.i18n import t

    monkeypatch.setattr(qapbot.cache_manager, "CACHE", _FakeCache())
    out = t("playerregistration.welcome_title", server_name="My Server")
    assert "My Server" in out


@pytest.mark.smoke
def test_t_missing_interpolation_variable_returns_template(monkeypatch: pytest.MonkeyPatch):
    import qapbot.cache_manager
    from qapbot.i18n import t

    monkeypatch.setattr(qapbot.cache_manager, "CACHE", _FakeCache())
    out = t("playerregistration.welcome_title")
    assert "{server_name}" in out


@pytest.mark.smoke
def test_language_resolution_user_over_guild(monkeypatch: pytest.MonkeyPatch):
    import qapbot.cache_manager
    from qapbot.i18n import t

    fake_cache = _FakeCache()
    fake_cache.server_config[str(1)] = {"language": "en"}
    fake_cache.user_accounts[str(2)] = {"user_language": "de"}

    monkeypatch.setattr(qapbot.cache_manager, "CACHE", fake_cache)

    # User language should override guild language
    assert t("common.errors.not_found", guild_id=1, user_id="2") == "Nicht gefunden"


@pytest.mark.smoke
def test_missing_key_falls_back_to_key_path(monkeypatch: pytest.MonkeyPatch):
    import qapbot.cache_manager
    from qapbot.i18n import t

    monkeypatch.setattr(qapbot.cache_manager, "CACHE", _FakeCache())
    assert t("this.key.does.not.exist") == "this.key.does.not.exist"


# ---------------------------------------------------------------------------
# get_namespace — plans/cwl-personal-hub.md Phase 6b, the Discord Activity's bulk-fetch
# translation accessor (GET /api/i18n).
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_get_namespace_returns_flat_dict_with_real_keys():
    from qapbot.i18n import get_namespace

    strings = get_namespace("cwl.player_hub", language="en")

    assert strings["title"] == "🛡️ Your CWL Preferences"
    assert strings["button_preferences"] == "Your CWL Preferences"
    # Placeholders stay uninterpolated — the caller substitutes, this function never does.
    assert "{" not in strings.get("title", "")  # this key has no placeholder; sanity check only


@pytest.mark.smoke
def test_get_namespace_defaults_to_english_language():
    from qapbot.i18n import get_namespace

    default_lang = get_namespace("cwl.player_hub")
    explicit_en = get_namespace("cwl.player_hub", language="en")

    assert default_lang == explicit_en


@pytest.mark.smoke
def test_get_namespace_resolves_german():
    from qapbot.i18n import get_namespace

    strings = get_namespace("cwl.player_hub", language="de")

    assert strings["title"] == "🛡️ Deine CWL-Einstellungen"


@pytest.mark.smoke
def test_get_namespace_unknown_namespace_returns_empty_dict():
    from qapbot.i18n import get_namespace

    assert get_namespace("this.namespace.does.not.exist", language="en") == {}
    assert get_namespace("this.namespace.does.not.exist", language="de") == {}


@pytest.mark.smoke
def test_get_namespace_falls_back_per_key_not_per_namespace(monkeypatch: pytest.MonkeyPatch):
    """A de.json missing ONE key under a namespace must yield English for that key only —
    never blank every sibling key in the namespace just because one is missing."""
    import qapbot.i18n as i18n_module

    fake_translations = {
        "en": {"cwl": {"activity": {"only_in_default": "English fallback", "shared_key": "English shared"}}},
        "de": {"cwl": {"activity": {"shared_key": "German shared"}}},  # missing only_in_default
    }
    monkeypatch.setattr(i18n_module._translation_manager, "translations", fake_translations)
    monkeypatch.setattr(i18n_module._translation_manager, "_loaded", True)

    strings = i18n_module.get_namespace("cwl.activity", language="de")

    assert strings["shared_key"] == "German shared"  # target language wins where present
    assert strings["only_in_default"] == "English fallback"  # falls back per-key, not dropped
