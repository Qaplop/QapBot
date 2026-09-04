"""Tests for the build identity reported at startup (QBcore.BOT_BUILD / source_fingerprint).

Deployment to the server-machine is a file copy, not a git operation, so "is it committed?"
says nothing about what is actually running. BOT_BUILD plus the source fingerprint are what
make a log line attributable to a specific edit — see Cardinal Rule 17.

The fingerprint is the safety net for a forgotten BOT_BUILD bump, so the properties that
matter are: it is stable for unchanged sources, it changes when a shipped source file
changes, and it never raises (diagnostics must not be able to stop the bot from starting).
"""
# pyright: reportPrivateUsage=false, reportMissingParameterType=false
from __future__ import annotations

import os
import re

import pytest

import QBcore


class TestBuildNumber:
    def test_bot_build_is_a_positive_int(self) -> None:
        assert isinstance(QBcore.BOT_BUILD, int)
        assert QBcore.BOT_BUILD >= 1

    def test_bot_build_is_not_a_string(self) -> None:
        """Guards the 'just bump it' edit: quoting it would break ordering comparisons."""
        assert not isinstance(QBcore.BOT_BUILD, str)

    def test_bot_version_still_present(self) -> None:
        """BOT_BUILD supplements BOT_VERSION, it does not replace it."""
        assert isinstance(QBcore.BOT_VERSION, str)
        assert QBcore.BOT_VERSION


class TestSourceFingerprint:
    def test_shape(self) -> None:
        fp = QBcore.source_fingerprint()
        assert re.fullmatch(r"[0-9a-f]{8}|unknown", fp), fp

    def test_not_unknown_in_a_normal_checkout(self) -> None:
        """'unknown' means the sources could not be read — never expected in the repo."""
        assert QBcore.source_fingerprint() != "unknown"

    def test_deterministic(self) -> None:
        assert QBcore.source_fingerprint() == QBcore.source_fingerprint()

    def test_changes_when_a_shipped_source_changes(self, tmp_path: object) -> None:
        """The whole point: an edit without a BOT_BUILD bump must still be detectable."""
        before = QBcore.source_fingerprint()
        probe = os.path.join(os.path.dirname(os.path.abspath(QBcore.__file__)), "qapbot", "_fp_probe.py")
        assert not os.path.exists(probe), "probe file leaked from a previous run"
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("# fingerprint probe\n")
            assert QBcore.source_fingerprint() != before
        finally:
            if os.path.exists(probe):
                os.remove(probe)
        assert QBcore.source_fingerprint() == before

    def test_never_raises_when_sources_are_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Diagnostics must not be able to prevent startup."""
        import os as _os

        def _boom(*_a: object, **_k: object) -> None:
            raise OSError("simulated unreadable source tree")

        monkeypatch.setattr(_os, "listdir", _boom)
        assert QBcore.source_fingerprint() == "unknown"
