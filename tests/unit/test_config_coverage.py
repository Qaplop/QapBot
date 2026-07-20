"""Tests for qapbot/config.py — load_config() and _validate_config().

Covers:
- DEV/PROD mode selection based on DISCORD_GUILD_ID
- Safe integer fallbacks for invalid env vars
- Validation rules (missing credentials, numeric ranges, mode consistency)
- no_coc_api flag parsing
"""
# pyright: reportPrivateUsage=false, reportUnusedFunction=false
from __future__ import annotations

import os
import pytest
from unittest.mock import patch
from qapbot.exceptions import ConfigurationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_env(*, dev: bool = False) -> dict[str, str]:
    """Minimal valid environment for load_config()."""
    if dev:
        return {
            "DISCORD_GUILD_ID": "123456789",
            "DISCORD_TOKEN_DEV": "dev_tok",
            "COC_API_EMAIL_DEV": "dev@example.com",
            "COC_API_PASSWORD_DEV": "dev_pass",
            "SLEEP_INTERVAL": "300",
        }
    return {
        "DISCORD_GUILD_ID": "0",
        "DISCORD_TOKEN": "prod_tok",
        "COC_API_EMAIL": "prod@example.com",
        "COC_API_PASSWORD": "prod_pass",
        "SLEEP_INTERVAL": "300",
    }


def _load_with_env(env: dict[str, str]):  # noqa: F811
    """Re-import load_config with a controlled environment."""
    _ = env  # used by callers via patch.dict
    with patch.dict(os.environ, env, clear=True):
        # Re-import to trigger load_config with fresh env
        from qapbot.config import load_config, _validate_config, BotConfig
        return load_config, _validate_config, BotConfig


# ---------------------------------------------------------------------------
# PROD mode
# ---------------------------------------------------------------------------

class TestLoadConfigProd:
    def test_prod_mode_basic(self):
        """DISCORD_GUILD_ID=0 means PROD mode → is_dev_mode should be False."""
        env = _base_env(dev=False)
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.is_dev_mode is False
            assert cfg.discord_guild_id == 0

    def test_prod_defaults(self):
        env = _base_env(dev=False)
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.sleep_interval == 300
            assert cfg.max_clan_subscriptions == 15
            assert cfg.notification_hours_before_end == 4
            assert cfg.notification_batch_delay == 2
            assert cfg.notification_max_retries == 1
            assert cfg.db_path == os.path.join("data", "qapbot.db")
            assert cfg.no_coc_api is False


# ---------------------------------------------------------------------------
# DEV mode
# ---------------------------------------------------------------------------

class TestLoadConfigDev:
    def test_dev_mode_selects_dev_credentials(self):
        env = _base_env(dev=True)
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.is_dev_mode is True
            assert cfg.discord_guild_id == 123456789
            assert cfg.coc_email == "dev@example.com"

    def test_dev_playerregistration_channel(self):
        env = {**_base_env(dev=True), "DEV_PLAYERREGISTRATION_CHANNEL_ID": "42"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.dev_playerregistration_channel_id == 42

    def test_dev_playerregistration_channel_invalid(self):
        env = {**_base_env(dev=True), "DEV_PLAYERREGISTRATION_CHANNEL_ID": "notanumber"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.dev_playerregistration_channel_id == 0


# ---------------------------------------------------------------------------
# Safe integer fallbacks
# ---------------------------------------------------------------------------

class TestIntFallbacks:
    def test_invalid_guild_id_falls_back_to_prod(self):
        env = {**_base_env(dev=False), "DISCORD_GUILD_ID": "not_a_number"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.discord_guild_id == 0
            assert cfg.is_dev_mode is False

    def test_invalid_max_subs_falls_back(self):
        env = {**_base_env(dev=False), "MAX_CLAN_SUBSCRIPTIONS": "abc"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.max_clan_subscriptions == 15

    def test_invalid_notif_hours_falls_back(self):
        env = {**_base_env(dev=False), "NOTIFICATION_HOURS_BEFORE_END": "xyz"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.notification_hours_before_end == 4

    def test_invalid_notif_delay_falls_back(self):
        env = {**_base_env(dev=False), "NOTIFICATION_BATCH_DELAY": "bad"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.notification_batch_delay == 2

    def test_invalid_notif_retries_falls_back(self):
        env = {**_base_env(dev=False), "NOTIFICATION_MAX_RETRIES": "oops"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.notification_max_retries == 1


# ---------------------------------------------------------------------------
# no_coc_api flag
# ---------------------------------------------------------------------------

class TestNoCocApiFlag:
    @pytest.mark.parametrize("value,expected", [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
    ])
    def test_no_coc_api_parsing(self, value: str, expected: bool):
        env = {**_base_env(dev=False), "NO_COC_API": value}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.no_coc_api is expected


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateConfig:
    def test_missing_discord_token_prod(self):
        env = {**_base_env(dev=False), "DISCORD_TOKEN": ""}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            with pytest.raises(ConfigurationError, match="DISCORD_TOKEN must be set"):
                load_config()

    def test_missing_discord_token_dev(self):
        env = {**_base_env(dev=True), "DISCORD_TOKEN_DEV": ""}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            with pytest.raises(ConfigurationError, match="DISCORD_TOKEN_DEV must be set"):
                load_config()

    def test_missing_coc_email_prod(self):
        env = {**_base_env(dev=False), "COC_API_EMAIL": ""}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            with pytest.raises(ConfigurationError, match="COC_API_EMAIL must be set"):
                load_config()

    def test_missing_coc_email_dev(self):
        env = {**_base_env(dev=True), "COC_API_EMAIL_DEV": ""}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            with pytest.raises(ConfigurationError, match="COC_API_EMAIL_DEV must be set"):
                load_config()

    def test_missing_coc_password_prod(self):
        env = {**_base_env(dev=False), "COC_API_PASSWORD": ""}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            with pytest.raises(ConfigurationError, match="COC_API_PASSWORD must be set"):
                load_config()

    def test_missing_coc_password_dev(self):
        env = {**_base_env(dev=True), "COC_API_PASSWORD_DEV": ""}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            with pytest.raises(ConfigurationError, match="COC_API_PASSWORD_DEV must be set"):
                load_config()

    def test_no_coc_api_skips_coc_credential_validation(self):
        env = {**_base_env(dev=False), "COC_API_EMAIL": "", "COC_API_PASSWORD": "", "NO_COC_API": "true"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.no_coc_api is True
            assert cfg.coc_email == ""

    def test_sleep_interval_too_low(self):
        from qapbot.config import BotConfig, _validate_config
        cfg = BotConfig(
            coc_email="x", coc_password="x", discord_token="x",
            sleep_interval=30, server_admin=""
        )
        with pytest.raises(ConfigurationError, match="SLEEP_INTERVAL must be >= 60"):
            _validate_config(cfg)

    def test_max_subs_too_low(self):
        from qapbot.config import BotConfig, _validate_config
        cfg = BotConfig(
            coc_email="x", coc_password="x", discord_token="x",
            sleep_interval=300, server_admin="", max_clan_subscriptions=0
        )
        with pytest.raises(ConfigurationError, match="MAX_CLAN_SUBSCRIPTIONS must be >= 1"):
            _validate_config(cfg)

    def test_negative_notification_hours(self):
        from qapbot.config import BotConfig, _validate_config
        cfg = BotConfig(
            coc_email="x", coc_password="x", discord_token="x",
            sleep_interval=300, server_admin="", notification_hours_before_end=-1
        )
        with pytest.raises(ConfigurationError, match="NOTIFICATION_HOURS_BEFORE_END must be >= 0"):
            _validate_config(cfg)

    def test_negative_batch_delay(self):
        from qapbot.config import BotConfig, _validate_config
        cfg = BotConfig(
            coc_email="x", coc_password="x", discord_token="x",
            sleep_interval=300, server_admin="", notification_batch_delay=-1
        )
        with pytest.raises(ConfigurationError, match="NOTIFICATION_BATCH_DELAY must be >= 0"):
            _validate_config(cfg)

    def test_negative_max_retries(self):
        from qapbot.config import BotConfig, _validate_config
        cfg = BotConfig(
            coc_email="x", coc_password="x", discord_token="x",
            sleep_interval=300, server_admin="", notification_max_retries=-1
        )
        with pytest.raises(ConfigurationError, match="NOTIFICATION_MAX_RETRIES must be >= 0"):
            _validate_config(cfg)

    def test_dev_mode_with_zero_guild_id(self):
        from qapbot.config import BotConfig, _validate_config
        cfg = BotConfig(
            coc_email="x", coc_password="x", discord_token="x",
            sleep_interval=300, server_admin="",
            is_dev_mode=True, discord_guild_id=0
        )
        with pytest.raises(ConfigurationError, match="DEV mode requires DISCORD_GUILD_ID > 0"):
            _validate_config(cfg)

    def test_prod_mode_with_nonzero_guild_id(self):
        from qapbot.config import BotConfig, _validate_config
        cfg = BotConfig(
            coc_email="x", coc_password="x", discord_token="x",
            sleep_interval=300, server_admin="",
            is_dev_mode=False, discord_guild_id=999
        )
        with pytest.raises(ConfigurationError, match="PROD mode requires DISCORD_GUILD_ID == 0"):
            _validate_config(cfg)


# ---------------------------------------------------------------------------
# Custom env overrides
# ---------------------------------------------------------------------------

class TestCustomEnvOverrides:
    def test_custom_db_path(self):
        env = {**_base_env(dev=False), "DB_PATH": "/custom/path.db"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.db_path == "/custom/path.db"

    def test_custom_sleep_interval(self):
        env = {**_base_env(dev=False), "SLEEP_INTERVAL": "600"}
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            assert cfg.sleep_interval == 600

    def test_frozen_dataclass_immutable(self):
        env = _base_env(dev=False)
        with patch.dict(os.environ, env, clear=True):
            from qapbot.config import load_config
            cfg = load_config()
            with pytest.raises(AttributeError):
                cfg.coc_email = "changed"  # type: ignore[misc]
