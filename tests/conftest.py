"""Shared fixtures for QapBot tests.

Phase 1 focuses on unit smoke tests + optional DEV live smoke.
Fixtures are kept lightweight and avoid touching PROD paths or on-disk data.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from unittest.mock import AsyncMock, MagicMock

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


# Make imports robust for both `python -m pytest` and `pytest` entrypoint.
# Ensures `import qapbot...` works even if the repo root is not automatically
# added to sys.path by the runner/environment.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load workspace .env for test runs (DEV live smoke reads DISCORD/COC vars from here).
# Does not override already-set environment variables.
if load_dotenv is not None:
    load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=False)

# PROD_DATA_DIR must never redirect test file I/O to the server-machine.
# Set to empty string so load_dotenv(override=False) in qapbot/config.py
# does not re-inject the .env value (override=False skips already-set vars).
os.environ["PROD_DATA_DIR"] = ""


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add CLI options for live smoke behavior.

These options are the single source of truth for live-smoke configuration.
"""
    group = parser.getgroup("qapbot")
    group.addoption(
        "--review-timeout-seconds",
        action="store",
        type=int,
        default=0,
        help=(
            "Optional dev review window for live Discord smoke messages. "
            "0 disables review pauses; >0 waits up to N seconds for ✅/❌."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """No-op (reserved for future pytest-wide configuration)."""
    return None


@pytest.fixture
def review_timeout_seconds(pytestconfig: pytest.Config) -> int:
    """Configured review timeout for live-smoke tests (CLI-only)."""
    value = pytestconfig.getoption("review_timeout_seconds")
    try:
        return max(0, int(value))
    except Exception:
        return 0


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_war_data(fixtures_dir: Path) -> Dict[str, Any]:
    """Minimal war_data dict for QBwarsim unit tests."""
    with open(fixtures_dir / "war_data_sample.json", "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def mock_interaction():
    """Mocked discord.Interaction for discord-layer tests (no live API)."""
    interaction = AsyncMock()
    interaction.user = MagicMock()
    interaction.user.id = 123456789
    interaction.user.name = "TestUser"
    interaction.user.display_name = "TestUser"
    interaction.user.mention = "@TestUser"

    interaction.guild = MagicMock()
    interaction.guild.id = 987654321
    interaction.guild.name = "TestGuild"

    interaction.response = AsyncMock()
    interaction.response.is_done.return_value = False
    interaction.followup = AsyncMock()

    interaction.channel = AsyncMock()
    interaction.channel.id = 111222333
    interaction.delete_original_response = AsyncMock()

    return interaction
