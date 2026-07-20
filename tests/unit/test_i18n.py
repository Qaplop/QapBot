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
