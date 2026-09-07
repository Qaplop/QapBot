"""Credentials must never be rendered by BotConfig's repr (2026-09-07).

BotConfig is a frozen dataclass, so it inherited the auto-generated ``__repr__`` that prints
every field verbatim — including the live Discord bot token and the CoC API password. That is
not a theoretical exposure: a pytest assertion failure printed both to the terminal in plain
text, and the same repr reaches log files through any traceback that carries a BotConfig
(traceback frames carry their locals, so the object merely being in scope somewhere up the
stack is enough to serialize it into a crash log).

These tests are the regression guard. The most important one is
``test_every_credential_field_is_masked``, which derives the credential list from the dataclass
itself rather than hard-coding it — so a NEW secret field added later fails the test instead of
silently inheriting the default "print me" behaviour.
"""
from __future__ import annotations

import dataclasses

import pytest

from qapbot.config import CONFIG, BotConfig


SENTINELS = {
    "coc_email": "canary-email@example.invalid",
    "coc_password": "canary-PASSWORD-a1b2c3d4",
    "discord_token": "canary.TOKEN.e5f6g7h8",
    "web_bridge_secret": "canary-BRIDGE-i9j0k1l2",
}


@pytest.fixture
def loaded() -> BotConfig:
    """A config carrying recognisable fake credentials in every secret field."""
    return dataclasses.replace(CONFIG, **SENTINELS)


class TestSecretsNeverRendered:
    def test_repr_contains_no_credential_value(self, loaded: BotConfig) -> None:
        rendered = repr(loaded)
        for name, secret in SENTINELS.items():
            assert secret not in rendered, f"{name} leaked into repr(): {rendered[:200]}"

    def test_str_contains_no_credential_value(self, loaded: BotConfig) -> None:
        """str() falls back to __repr__ for dataclasses — f-strings and %s logging go here."""
        rendered = str(loaded)
        for name, secret in SENTINELS.items():
            assert secret not in rendered, f"{name} leaked into str(): {rendered[:200]}"

    def test_format_and_percent_interpolation_are_also_safe(self, loaded: BotConfig) -> None:
        """The two shapes this actually reaches a log file through in practice."""
        assert SENTINELS["discord_token"] not in f"{loaded}"
        assert SENTINELS["discord_token"] not in "%s" % (loaded,)
        assert SENTINELS["discord_token"] not in "{}".format(loaded)

    def test_traceback_rendering_does_not_leak(self, loaded: BotConfig) -> None:
        """The real-world path: an exception raised while a BotConfig is a local, whose
        traceback then gets formatted into a log."""
        import traceback

        try:
            config = loaded  # noqa: F841 — deliberately a frame local, as in real call sites
            raise RuntimeError("boom")
        except RuntimeError:
            rendered = "".join(traceback.format_exc())
            # Also render locals the way rich/pytest-style handlers do
            rendered += repr(loaded)

        for name, secret in SENTINELS.items():
            assert secret not in rendered, f"{name} leaked through traceback rendering"


class TestMaskingIsComplete:
    def test_every_credential_field_is_masked(self, loaded: BotConfig) -> None:
        """Derives the list from BotConfig._SECRET_FIELDS so a newly-added credential that
        forgets masking fails here rather than leaking in production."""
        rendered = repr(loaded)
        for name in BotConfig._SECRET_FIELDS:
            assert f"{name}=<" in rendered, (
                f"{name} is listed as a secret but is not rendered in masked form"
            )

    def test_sentinels_cover_every_declared_secret_field(self) -> None:
        """If someone adds a secret to _SECRET_FIELDS, this test file must grow a sentinel for
        it too — otherwise the leak tests above would silently stop covering it."""
        assert set(BotConfig._SECRET_FIELDS) == set(SENTINELS), (
            "BotConfig._SECRET_FIELDS and this file's SENTINELS have diverged"
        )

    def test_declared_secrets_all_opt_out_of_the_generated_repr(self) -> None:
        """Belt and braces: even if __repr__ were ever removed, repr=False on the field keeps
        the dataclass-generated fallback from printing the value."""
        by_name = {f.name: f for f in dataclasses.fields(BotConfig)}
        for name in BotConfig._SECRET_FIELDS:
            assert by_name[name].repr is False, f"{name} is missing field(repr=False)"


class TestStillUsefulForDebugging:
    """Masking rather than omitting: 'is the token actually loaded?' stays answerable."""

    def test_set_and_empty_are_distinguishable(self) -> None:
        with_token = dataclasses.replace(CONFIG, discord_token="something")
        without_token = dataclasses.replace(CONFIG, discord_token="")

        assert "discord_token=<set>" in repr(with_token)
        assert "discord_token=<empty>" in repr(without_token)

    def test_no_length_or_prefix_is_disclosed(self) -> None:
        """A masked value must not vary with the secret, or it narrows a brute-force search."""
        short = repr(dataclasses.replace(CONFIG, discord_token="x"))
        long = repr(dataclasses.replace(CONFIG, discord_token="x" * 500))

        assert short == long, "the mask varies with the secret's content or length"

    def test_non_secret_fields_are_still_visible(self) -> None:
        rendered = repr(CONFIG)
        assert "sleep_interval=" in rendered
        assert "is_dev_mode=" in rendered
