"""Tests for db_manager sync functions — Phase 4 coverage.

Uses real in-memory SQLite databases for accurate testing.
Covers: add_war_records_sync, get_clan_history_sync.
"""
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownArgumentType=false, reportUnusedImport=false
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

import pytest

from qapbot.db_manager import WarHistoryDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# add_war_records_sync
# ---------------------------------------------------------------------------

