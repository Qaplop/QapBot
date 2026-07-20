from __future__ import annotations
# pyright: reportPrivateUsage=false

import re
from pathlib import Path

import pytest

import QBhelperfunctions as hf
from qapbot.exceptions import WarProcessingError


class _FakeDbManager:
    def __init__(self, exists: bool):
        self._exists = exists

    def war_attacks_exist_sync(self, clan_tag: str, war_id: str) -> bool:
        return self._exists and bool(clan_tag and war_id)


@pytest.mark.smoke
def test_generate_message_key_timestamp_format() -> None:
    ts = hf.generate_message_key_timestamp()

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{6}\.\d{3}", ts)


@pytest.mark.smoke
def test_load_history_filtered_filters_invalid_rows_and_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = {"count": 0}

    def _fake_get_clan_history(_clan_tag: str):
        call_count["count"] += 1
        return [
            {"Date": "2026-02-01T12:00:00", "Player": "A"},
            {"Date": "2026-01-05T09:00:00", "Player": "B"},
            {"Date": "not-a-date", "Player": "C"},
            {"Player": "D"},
        ]

    monkeypatch.setattr(hf.CACHE, "history_cache", {}, raising=False)
    monkeypatch.setattr(hf.CACHE, "get_clan_history", _fake_get_clan_history, raising=False)

    out1 = hf._load_history_filtered("#CLAN", month=2, year=2026)
    out2 = hf._load_history_filtered("#CLAN", month=2, year=2026)

    assert len(out1) == 1
    assert out1[0]["Player"] == "A"
    assert out2 == out1
    assert call_count["count"] == 1


@pytest.mark.smoke
def test_is_war_in_history_returns_false_on_empty_war_id() -> None:
    assert hf._is_war_in_history("#CLAN", "") is False


@pytest.mark.smoke
def test_is_war_in_history_raises_when_db_manager_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf.CACHE, "db_manager", None, raising=False)

    with pytest.raises(WarProcessingError):
        hf._is_war_in_history("#CLAN", "OPP_202601010000")


@pytest.mark.smoke
def test_is_war_in_history_uses_db_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hf.CACHE, "db_manager", _FakeDbManager(exists=True), raising=False)

    assert hf._is_war_in_history("#CLAN", "OPP_202601010000") is True


@pytest.mark.smoke
def test_process_war_history_returns_false_when_file_missing(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing_war_data.json"

    assert hf._process_war_history("#CLAN", str(missing_file), war_type_str="random") is False
