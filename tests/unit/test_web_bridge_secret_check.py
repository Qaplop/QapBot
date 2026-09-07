"""The web bridge's shared-secret gate (`_check_secret`), tracker follow-up 2026-09-07.

`_check_secret` is the single chokepoint for every bridge handler (~20 call sites), so its
comparison only has to be right in one place — and it was using `==`, which short-circuits at
the first differing byte. That leaks, through response timing, how many leading bytes of a
guess were correct, turning an infeasible-by-length search into a byte-at-a-time one.

"It's bound to 127.0.0.1" is not the boundary it appears to be: a cloudflared tunnel
deliberately makes this endpoint reachable from the Cloudflare Worker (see
`start_web_bridge()`), so the attack surface is not purely local.

The end-to-end 403 behaviour is covered in tests/discord/test_web_bridge.py; these pin the
gate's own properties, including the ones an integration test can't see.
"""
from __future__ import annotations

import dataclasses
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from qapbot.config import CONFIG
from qapbot.web_bridge import _check_secret


def _request(header_value: str | None) -> MagicMock:
    """A stand-in for aiohttp's Request — _check_secret only reads one header."""
    request = MagicMock()
    request.headers = {} if header_value is None else {"X-Bridge-Secret": header_value}
    return request


@pytest.fixture
def configured(monkeypatch):
    """Point the module's CONFIG lookup at a known secret. _check_secret imports CONFIG inside
    the function, so patching the module attribute is what takes effect."""
    def _set(secret: str):
        import qapbot.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", dataclasses.replace(CONFIG, web_bridge_secret=secret))
    return _set


class TestAuthenticationDecision:
    def test_accepts_the_exact_secret(self, configured):
        configured("s3cret-value")
        assert _check_secret(_request("s3cret-value")) is True

    def test_rejects_a_wrong_secret(self, configured):
        configured("s3cret-value")
        assert _check_secret(_request("wrong")) is False

    def test_rejects_a_missing_header(self, configured):
        configured("s3cret-value")
        assert _check_secret(_request(None)) is False

    def test_rejects_an_empty_header(self, configured):
        configured("s3cret-value")
        assert _check_secret(_request("")) is False

    @pytest.mark.parametrize("guess", [
        "s3cret-valu",     # correct prefix, truncated
        "s3cret-value ",   # correct plus trailing whitespace
        "S3CRET-VALUE",    # case differs
        "s3cret-valuex",   # correct plus one byte
    ])
    def test_rejects_near_misses(self, configured, guess):
        configured("s3cret-value")
        assert _check_secret(_request(guess)) is False


class TestUnconfiguredSecretNeverAuthenticates:
    """The guard that must survive the switch to compare_digest: `compare_digest("", "")` is
    True, so folding the empty-secret case into the comparison would make an unconfigured
    bridge authenticate every caller — including one sending no header at all."""

    @pytest.mark.parametrize("provided", [None, "", "anything"])
    def test_empty_configured_secret_rejects_everything(self, configured, provided):
        configured("")
        assert _check_secret(_request(provided)) is False


class TestConstantTimeComparison:
    def test_uses_hmac_compare_digest_not_equality(self, configured, monkeypatch):
        """Pins the mechanism, not just the outcome: `==` would pass every behavioural test
        above while still leaking the prefix through timing. Asserting compare_digest is
        actually called is the only way a test can tell the two apart."""
        import hmac

        calls = []
        real = hmac.compare_digest

        def _spy(a, b):
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(hmac, "compare_digest", _spy)
        configured("s3cret-value")

        assert _check_secret(_request("s3cret-value")) is True
        assert calls, "the comparison did not go through hmac.compare_digest"

    def test_compares_bytes_so_a_non_ascii_secret_cannot_raise(self, configured):
        """compare_digest raises TypeError on str arguments containing non-ASCII. The secret is
        operator-supplied, so an accented character would otherwise turn every request into a
        500 instead of a clean 401 — and a 500-vs-403 split is itself an oracle."""
        configured("sécret-vàlue")

        assert _check_secret(_request("sécret-vàlue")) is True
        assert _check_secret(_request("wrong")) is False
        assert _check_secret(_request("sécret-vàlu")) is False
