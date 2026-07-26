# QapBot Test Concept

**Status**: Implemented  
**Created**: 2026-02-17  
**Last updated**: 2026-07-26 (accuracy audit, then redundancy pass merging the "how to run
tests" content that was duplicated across §7/§14/old-§15 into one canonical §7; old §15
"Convenience Scripts" was folded into §7 and removed, so §16 "Key Risks" is now §15)  
**Audience**: Developer(s), CI/CD pipeline

---

## 1. Goals

| Goal | How |
|------|-----|
| **Fast iterative feedback** | `smoke` suite runs fast (< 60s), offline: no network, no Discord token, no CoC |
| **Full regression safety** | `full` suite covers every module before prod deployment |
| **Minimal manual effort** | `smoke` is fully automated; `full` is mostly automated but allows targeted manual steps for interaction-heavy Discord workflows |
| **No prod data risk** | Tests run in DEV guild (`DISCORD_GUILD_ID > 0`) and post only to `DEV_PLAYERREGISTRATION_CHANNEL_ID`; DB is in-memory or temp; tests never write to `data/` (any filesystem checks are DEV-only and read-only) |

---

## 2. Tool Stack

| Tool | Purpose |
|------|---------|
| **pytest** | Test runner, fixtures, markers, parametrize |
| **pytest-asyncio** | `async def test_*` for aiosqlite / discord.py coroutines |
| **pytest-cov** | Coverage reporting (target: 80%+ on business logic) |
| **unittest.mock / AsyncMock** | Mock Discord client, CoC client, interactions |
| **aiosqlite** (already installed) | In-memory `:memory:` databases for db_manager tests |
| **discord.py** (already used by the bot) | Live smoke: connect to DEV guild, send messages to test channel, fetch history for assertions |

Note: `pytest`, `pytest-asyncio`, and `pytest-cov` are already included in `requirements.txt`.

---

## 3. Directory Layout

```
tests/
├── conftest.py              # Shared fixtures (CACHE mock, in-memory DB, fake war data)
├── builders.py              # Programmatic test-data builders (see section 11)
├── fixtures/                # Static test data files
│   └── war_data_sample.json
├── smoke_live/               # Live DEV smoke (network + real Discord; CoC optional) — runs only with -m live
│   ├── test_live_leaderboard.py
│   ├── test_live_status_and_admin_checks.py
│   ├── test_live_message_assertions.py
│   └── _review.py           # shared review/approval helper, not a test module
├── unit/                    # Pure logic, no I/O — runs in smoke + full
│   ├── test_formatting.py
│   ├── test_warsim.py
│   ├── test_constants.py
│   ├── test_exceptions.py
│   ├── test_i18n.py
│   ├── test_helpers_pure.py
│   ├── test_csv_handling.py
│   └── ... (50+ more files; the suite has grown far beyond this original tier design)
├── integration/             # Real DB, mocked APIs — runs in full only
│   ├── test_db_manager.py
│   ├── test_cache_manager.py
│   ├── test_coc_cache.py
│   ├── test_account_protection.py
│   └── ... (test_coc_cache_failures.py, test_db_manager_extended.py, test_guild_roles.py)
├── discord/                 # Mocked Discord interactions — runs in full only
│   ├── test_commands.py
│   ├── test_ui_registration.py
│   ├── test_discord_helper.py
│   └── ... (10+ more phase-specific files, e.g. test_ui_notifications_phase*.py, test_ui_clan_management_phase4.py)
└── e2e/                     # Not yet created. Marker exists in pyproject.toml; tier remains deferred (see section 8).
```

Note: the file lists above are illustrative of the original tier design (2026-02-17/18 session). The
suite has grown substantially since — as of this audit, `tests/unit/` alone has 50+ files. Only the two
`fixtures/` files originally planned (`cwl_war_sample.json`, `translations_test.json`) were never created;
`war_data_sample.json` is the only fixture file that exists.

---

## 4. Pytest Configuration

### pyproject.toml (already in project root)

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "smoke: fast offline unit smoke (no network, no Discord, no CoC)",
    "live: DEV live smoke (network + real Discord; CoC optional depending on test)",
    "integration: requires in-memory DB, mocked APIs",
    "discord: requires mocked Discord client/interaction objects",
    "e2e: end-to-end with live test bot (manual only)",
    "slow: tests that take > 5s individually",
]
filterwarnings = [
    "ignore::DeprecationWarning",
    # aiosqlite worker thread delivers a callback to an already-closed per-test
    # event loop under asyncio_mode="auto"; known benign race, suppressed.
    "ignore::pytest.PytestUnhandledThreadExceptionWarning",
]

[tool.coverage.run]
source = ["."]
omit = [
    "tests/*",
    "qapbot/scripts/*",
    # UI-heavy / bot-lifecycle files: ~90% Discord wiring, not unit-testable
    "qapbot/ui_clan_management.py",
    "QBdiscordcmds.py",
    "QapBot.py",
]

[tool.coverage.report]
fail_under = 70
show_missing = true
```

---

## 5. Fixture Strategy

### 5.1 conftest.py — Core Fixtures

**Note — planned vs. actual**: the code block below is the original fixture design from this
concept's first session. The real `tests/conftest.py` today only implements `sample_war_data()`
and `mock_interaction()` as shared fixtures (plus `review_timeout_seconds`, `fixtures_dir`, and
the `--review-timeout-seconds` CLI option). The `db()`, `mock_cache()`, and `mock_coc_client()`
fixtures shown below were never added as shared fixtures — individual test modules construct
their own local `WarHistoryDB(":memory:")` instances and CACHE/coc-client mocks inline instead.

```python
"""Shared fixtures for all QapBot tests."""
import json
import pytest
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# ---------------------------------------------------------------------------
# In-memory database
# ---------------------------------------------------------------------------
@pytest.fixture
async def db():
    """Fresh in-memory WarHistoryDB for each test."""
    from qapbot.db_manager import WarHistoryDB
    db = WarHistoryDB()
    await db.initialize(":memory:")
    yield db
    await db.close()

# ---------------------------------------------------------------------------
# Mocked CACHE singleton
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_cache(db):
    """
    CacheManager-like object backed by in-memory DB.
    Patches the global CACHE import so modules see the mock.
    """
    from qapbot.cache_manager import CacheManager
    cache = CacheManager.__new__(CacheManager)
    cache.db_manager = db
    cache.clan_name_cache = {}
    cache.user_accounts = {}
    cache.subscriptions = {}
    cache.notification_state = {}
    cache.leaderboard_messages = {}
    cache.temp_war_stats = {}
    cache.clan_history = {}
    cache.history_cache = {}
    cache.clan_families = {}
    cache.server_config = {}
    # Patch global CACHE references
    with patch("qapbot.cache_manager.CACHE", cache), \
         patch("QBhelperfunctions.CACHE", cache):
        yield cache

# ---------------------------------------------------------------------------
# Sample war data
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_war_data():
    """Minimal valid war_data dict for warsim / helper tests."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "war_data_sample.json", "r") as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# Mock Discord objects
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_interaction():
    """Fake discord.Interaction with common attributes."""
    interaction = AsyncMock()
    interaction.user = MagicMock()
    interaction.user.id = 123456789
    interaction.user.name = "TestUser"
    interaction.user.display_name = "TestUser"
    interaction.guild = MagicMock()
    interaction.guild.id = 987654321
    interaction.guild.name = "TestGuild"
    interaction.response = AsyncMock()
    interaction.response.is_done.return_value = False
    interaction.followup = AsyncMock()
    interaction.channel = AsyncMock()
    interaction.channel.id = 111222333
    return interaction

# ---------------------------------------------------------------------------
# Mock CoC client
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_coc_client():
    """Fake coc.Client with common methods mocked."""
    client = AsyncMock()
    client.get_clan = AsyncMock()
    client.get_current_war = AsyncMock()
    client.get_player = AsyncMock()
    return client
```

### 5.2 Test Data Fixtures

Create `tests/fixtures/war_data_sample.json` with a minimal but realistic war structure (both clans, members, attacks, TH levels) that exercises all warsim code paths.

---

## 6. Test Tiers — What Goes Where

### Tier 1: Unit Smoke (`tests/unit/`) — marker: `@pytest.mark.smoke`

Smoke is intended to catch breakages quickly and deterministically:
- Pure Python logic, no network
- No Discord login / token
- No CoC API calls

### Tier 1.5: Live DEV Smoke (`tests/smoke_live/`) — marker: `@pytest.mark.live`

**Scope**: Real Discord (DEV guild only) and optional CoC API in DEV mode.
These tests post to `DEV_PLAYERREGISTRATION_CHANNEL_ID`, read messages back via Discord API for assertions, and then clean up.

**Required DEV env vars** (tests should `pytest.skip` if missing):
- `DISCORD_GUILD_ID` (must be > 0)
- `DISCORD_TOKEN_DEV`
- `DEV_PLAYERREGISTRATION_CHANNEL_ID`

Optional dev-feedback controls:
- `--review-timeout-seconds N` (e.g. 20) to keep messages visible and accept ✅/❌ within a timeout

**Important limitation (slash commands):** Discord does not provide a supported way for a bot to “execute” slash commands like a user via the API. For automation, live tests call the underlying Python command handler functions and validate output by reading channel history. Interaction-heavy workflows remain manual E2E.

**Scope**: Pure functions with no I/O, no async, no external deps.  
**Run time target**: < 30 seconds total.

| Module | What to test | Priority |
|--------|-------------|----------|
| `formatting.py` | `text_display_width`, `truncate_to_width`, `right_pad_number`, `normalize_player_name`, `best_practice_player_cell`, `render_leaderboard` with sample data | **HIGH** — regressions here break every leaderboard |
| `QBwarsim.py` | `th_star_probabilities` (boundary TH levels), `assign_attacks_to_bases_with_stars` (edge: no remaining attacks, all 3-starred), `calculate_max_possible_stars`, `monte_carlo_war_prediction` deterministic seeds | **HIGH** — math-heavy, easy to break |
| `constants.py` | Smoke: all constants exist, types are correct, no accidental mutation | LOW |
| `exceptions.py` | Each exception class instantiates, has `message` and `context` | LOW |
| `i18n.py` | `t()` returns key fallback when translation missing, interpolation works, language resolution priority (user > guild > default) | **MEDIUM** |
| `config.py` | Validation raises `ConfigurationError` on missing required vars | MEDIUM |
| `QBhelperfunctions.py` (pure parts) | `calculate_content_hash`, `_merge_entries`, `_parse_war_stats_from_api` (with dict input, no CoC objects) | **HIGH** |
| `QBcsvhandling.py` | War ID extraction, file path helpers | LOW |
| `QBdiscocmdshelper.py` (pure parts) | `normalize_clan_tag`, `generate_family_tag`, `normalize_family_tag`, `_split_message_into_chunks`, `_split_embed_by_description`, `is_player_in_member_clans` | **MEDIUM** |

**Example: test_warsim.py**
```python
import pytest
from QBwarsim import th_star_probabilities, calculate_max_possible_stars

class TestThStarProbabilities:
    """Verify probability distributions for all TH matchup categories."""

    @pytest.mark.smoke
    @pytest.mark.parametrize("atk_th,def_th,expected_sum", [
        (16, 16, 1.0),   # equal TH
        (16, 14, 1.0),   # attacker higher
        (14, 16, 1.0),   # attacker lower
        (8, 8, 1.0),     # low TH
    ])
    def test_probabilities_sum_to_one(self, atk_th, def_th, expected_sum):
        probs = th_star_probabilities(atk_th, def_th)
        assert len(probs) == 4  # 0, 1, 2, 3 stars
        assert abs(sum(probs) - expected_sum) < 1e-9

    @pytest.mark.smoke
    def test_higher_th_has_better_3star_rate(self):
        higher = th_star_probabilities(16, 14)
        equal = th_star_probabilities(14, 14)
        assert higher[3] >= equal[3]
```

**Example: test_formatting.py**
```python
import pytest
from qapbot.formatting import (
    text_display_width, truncate_to_width, right_pad_number,
    normalize_player_name
)

class TestTextDisplayWidth:
    @pytest.mark.smoke
    @pytest.mark.parametrize("text,expected", [
        ("hello", 5),
        ("", 0),
        ("A" * 100, 100),
    ])
    def test_ascii_width(self, text, expected):
        assert text_display_width(text) == expected

    @pytest.mark.smoke
    def test_wide_chars_count_double(self):
        # CJK characters should count as width 2
        assert text_display_width("你好") > 2

class TestNormalizePlayerName:
    @pytest.mark.smoke
    @pytest.mark.parametrize("raw,expected_non_empty", [
        ("  spaces  ", True),
        ("normal", True),
        ("", False),  # or whatever the contract is
    ])
    def test_normalize(self, raw, expected_non_empty):
        result = normalize_player_name(raw)
        assert bool(result.strip()) == expected_non_empty
```

---

### Tier 2: Integration (`tests/integration/`) — marker: `@pytest.mark.integration`

**Scope**: Real in-memory SQLite, mocked CACHE, no network.  
**Run time target**: < 2 minutes total.

| Module | What to test | Priority |
|--------|-------------|----------|
| `db_manager.py` | Full CRUD cycle for all 13 tables, idempotency (INSERT OR IGNORE twice), FK cascades (delete user → user_players gone), WAL mode active, schema creation idempotent, `check_integrity_sync` | **CRITICAL** |
| `cache_manager.py` | `load_all` populates all dicts from DB, write-through methods persist (mutate + read-back), `save_all` is no-op, error propagation | **CRITICAL** |
| `coc_cache.py` | TTL behavior (mock time), stale-while-revalidate returns cached then refreshes, cache miss → fetch | **HIGH** |
| Account protection | `get_verified_player_owner`, `get_any_player_owner`, `_link_player_to_user` with all 4 rules (unlinked, unverified re-link, verified re-link, API token override) | **CRITICAL** |

**Example: test_db_manager.py**
```python
import pytest

class TestWarRecords:
    @pytest.mark.integration
    async def test_add_and_retrieve_war_records(self, db):
        records = [
            {"player_tag": "#P1", "player_name": "Alice", "th_level": 15,
             "stars": 5, "attacks": 2, "war_id": "W1",
             "war_start_time": "2026-01-01T00:00:00", "war_type": "regular"},
        ]
        count = await db.add_war_records("#CLAN1", records)
        assert count == 1

        history = await db.get_clan_history("#CLAN1")
        assert len(history) == 1
        assert history[0]["player_tag"] == "#P1"

    @pytest.mark.integration
    async def test_idempotent_insert(self, db):
        """INSERT OR IGNORE: same record twice = 1 total."""
        record = [{"player_tag": "#P1", "player_name": "Alice", "th_level": 15,
                    "stars": 5, "attacks": 2, "war_id": "W1",
                    "war_start_time": "2026-01-01T00:00:00", "war_type": "regular"}]
        await db.add_war_records("#C1", record)
        count = await db.add_war_records("#C1", record)
        assert count == 0  # no new rows

    @pytest.mark.integration
    async def test_fk_cascade_user_delete(self, db):
        """Deleting a user cascades to user_players."""
        await db.save_user("12345", {"display_name": "Test", "players": [
            {"player_tag": "#P1", "player_name": "Alice"}
        ]})
        await db.delete_user("12345")
        user = await db.get_user("12345")
        assert user is None

class TestSchemaIdempotency:
    @pytest.mark.integration
    async def test_double_init(self, db):
        """Calling _create_schema twice must not raise."""
        await db._create_schema()
        await db._create_maindata_schema()
```

---

### Tier 3: Discord (`tests/discord/`) — marker: `@pytest.mark.discord`

**Scope**: Mocked `discord.Interaction`, `discord.Client`, channels. Tests command handlers and UI views.  
**Run time target**: < 2 minutes total.

| Module | What to test | Priority |
|--------|-------------|----------|
| `QBdiscocmdshelper.py` | `send_and_track` stores message IDs, `check_admin_permissions` returns correct bools, `complete_account_linking_flow` calls correct security checks | **HIGH** |
| `ui_registration.py` | `RegistrationView` button callbacks call correct methods, `PlayerSubstringModal` validates input | **MEDIUM** |
| `ui_notifications.py` | `UnifiedNotificationView` state transitions (enable/disable/change mode) | **MEDIUM** |
| `ui_clan_management.py` | `ClanManagementView` mode switching, admin override flow | **MEDIUM** |
| `QBdiscordcmds.py` | Key slash commands with mocked interaction (leaderboard, subscribe, admin) | **HIGH** |

**Example: test_discord_helper.py**
```python
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

class TestAdminPermissions:
    @pytest.mark.discord
    async def test_server_admin_is_admin(self, mock_interaction):
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        mock_interaction.user.name = "BotAdmin"
        assert check_bot_admin_only(mock_interaction, "BotAdmin") is True

    @pytest.mark.discord
    async def test_regular_user_not_admin(self, mock_interaction):
        from qapbot.QBdiscocmdshelper import check_bot_admin_only
        mock_interaction.user.name = "RegularUser"
        assert check_bot_admin_only(mock_interaction, "BotAdmin") is False

class TestNormalizeClanTag:
    @pytest.mark.smoke
    @pytest.mark.parametrize("input_tag,expected", [
        ("#ABC123", "#ABC123"),
        ("ABC123", "#ABC123"),
        ("  #abc123  ", "#ABC123"),
        ("", None),
        ("   ", None),
    ])
    def test_normalize(self, input_tag, expected):
        from qapbot.QBdiscocmdshelper import normalize_clan_tag
        assert normalize_clan_tag(input_tag) == expected
```

---

### Tier 4: E2E (`tests/e2e/`) — marker: `@pytest.mark.e2e`

**Scope**: Optional. Live bot instance on a dedicated test Discord server.  
**Trigger**: Manual only (recommended), never in CI by default.  
**Purpose**: Validate full startup → periodic loop → shutdown lifecycle, and workflows that inherently require real user interactions (button clicks, dropdown selections, modals).

This tier is deferred until the other tiers are stable. It requires:
- A test Discord server with the bot invited
- DEV-mode `.env` credentials
- Manual trigger: `pytest -m e2e --runlive`

---

## 7. Running Tests — Command Reference

**Canonical local invocation**: always run tests via `.\run_tests.ps1` (repo root, PowerShell)
rather than constructing a raw `pytest` command. No `run_tests_smoke.bat` / `run_tests_full.bat`
ever existed — this script is the only convenience wrapper. It resolves `venv\Scripts\python.exe`
automatically (falling back to `python` on PATH), always runs with `-x -q --tb=short`, and applies
the repo's canonical `--deselect` list (known-broken/irrelevant tests) before any extra args.
Defaults to `-m "not live"`.

```powershell
.\run_tests.ps1              # fast local tests only (skips @pytest.mark.live) — < 30 sec
.\run_tests.ps1 -Full        # all tests including live DEV Discord smoke
.\run_tests.ps1 -k my_test   # pass extra pytest filters through (still skips live)
.\run_tests.ps1 -Full -k foo # full run with an additional filter
.\run_tests.ps1 -Full --cov --cov-report=term-missing  # full run + coverage report
```

The raw `pytest -m ...` invocations below describe the marker-based tiers conceptually, and are
what CI runs directly on Linux (see § 9), where the wrapper doesn't apply. For local Windows
development, use `run_tests.ps1` with the equivalent marker/filter instead of typing these by hand.

| Intent | Marker expression | Notes |
|---|---|---|
| Offline unit smoke only | `-m smoke` | No Discord/CoC. Expected time < 30s. |
| Live DEV smoke only | `-m live` | Add `--review-timeout-seconds 20` for an optional manual ✅/❌ review pause. `/admin` live coverage is part of this marker, no separate flag. |
| Unit smoke + live DEV smoke | `-m "smoke or live"` | `-m "smoke and live"` would select the *intersection*, which is empty — `smoke` and `live` are intentionally disjoint (offline vs. Discord-posting). |
| Full offline regression (unit + integration + mocked Discord) | `-m "smoke or integration or discord"` | Recommended pre-commit/pre-PR; add `--cov --cov-report=term-missing` for coverage. |
| Full regression + live DEV smoke | `-m "smoke or integration or discord or live"` | Add `--review-timeout-seconds 0` to skip the manual pause. Interaction-heavy Views/Modals still need `e2e` manual runs or a targeted manual checklist. |
| One module | `pytest tests/unit/test_warsim.py -v` | Bypasses markers entirely. |
| Coverage report only | `pytest --cov --cov-report=html` | Open `htmlcov/index.html`. |

---

## 8. Implementation Priority & Roadmap

### Current Implementation Status (as of 2026-02-18)

Completed:
- Offline smoke tests (`-m smoke`)
- Offline integration tests (`-m integration`)
- Mocked Discord-layer tests (`-m discord`)
- DEV live smoke tests (`-m live`) with optional review pause via `--review-timeout-seconds`
- CI workflow for offline suites (`smoke` + `full-offline`) in `.github/workflows/test.yml`

Deferred / optional:
- `e2e` tier (manual-only live bot lifecycle + interaction-heavy UI workflows)
- Coverage gate in CI (currently informational; `--cov-fail-under=0`)

---

## 9. CI Pipeline (GitHub Actions)

By default, CI runs without real Discord/CoC credentials. `live` tests are excluded from CI.

See `.github/workflows/test.yml` for the authoritative CI configuration. Current CI runs:
- `smoke`: `python -m pytest -m "smoke" -x -q --tb=short`
- `full-offline`: `python -m pytest -m "smoke or integration or discord" --tb=short` (+ coverage as informational)

---

## 10. Mocking Strategy

### What to Mock (and how)

| Dependency | Mock Approach | Why |
|-----------|--------------|-----|
| **Discord API** | `AsyncMock` for most tests; **real Discord connection** in `smoke_live` | Offline tests stay deterministic; smoke_live validates real message sending + read-back |
| **CoC API** | `AsyncMock` for most tests; **real CoC API** in `smoke_live` | Offline tests avoid credentials; smoke_live validates connectivity and API shape |
| **SQLite (unit tests)** | Skip — not used in unit tests | Pure functions don't touch DB |
| **SQLite (integration)** | Real `aiosqlite` with `:memory:` | Fast, isolated, disposable per test |
| **File system** | `tmp_path` fixture (pytest built-in) for war file tests | No pollution of `data/` directory |
| **Time** | `unittest.mock.patch("time.time")` or `freezegun` | Control TTL / stale-while-revalidate |
| **CACHE singleton** | Fixture constructs minimal CacheManager, patches global refs | Isolation between tests |
| **CONFIG singleton** | `patch("qapbot.config.CONFIG", ...)` with test values | No `.env` file needed |
| **Logging** | Default (no mock) — let tests log for debugging | Useful for diagnosis |

### Key Mocking Patterns

```python
# Mock CONFIG for tests that import it
@pytest.fixture(autouse=True)
def mock_config():
    """Provide a safe CONFIG for all tests."""
    from unittest.mock import patch, MagicMock
    fake_config = MagicMock()
    fake_config.is_dev_mode = True
    fake_config.server_admin = "TestAdmin"
    fake_config.sleep_interval = 10
    fake_config.max_clan_subscriptions = 7
    fake_config.db_path = ":memory:"
    with patch("qapbot.config.CONFIG", fake_config):
        yield fake_config

# Mock Discord interaction for command tests
async def test_leaderboard_command(mock_interaction, mock_cache):
    from QBdiscordcmds import leaderboard  # the slash command function
    mock_interaction.response.defer = AsyncMock()
    mock_interaction.followup.send = AsyncMock()
    
    await leaderboard(mock_interaction, clantag="#TEST")
    
    mock_interaction.response.defer.assert_called_once()
    mock_interaction.followup.send.assert_called()
```

---

## 11. Test Data Management

### Strategy: Fixtures over Factory

Use **static JSON fixtures** for war data (deterministic, reviewable) and **programmatic builders** for simple objects.

### War Data Builder

```python
# tests/builders.py
def make_war_member(tag="#P1", name="Player1", th=15, attacks_used=0, max_attacks=2):
    return {
        "tag": tag, "name": name, "townhall_level": th,
        "attacks": [], "attacks_used": attacks_used,
        "max_attacks": max_attacks, "map_position": 1
    }

def make_war_data(clan_tag="#CLAN1", opponent_tag="#OPP1",
                  clan_members=None, opponent_members=None,
                  state="inWar", team_size=15):
    return {
        "state": state, "team_size": team_size,
        "clan": {
            "tag": clan_tag, "name": "TestClan",
            "members": clan_members or [make_war_member()],
        },
        "opponent": {
            "tag": opponent_tag, "name": "OpponentClan",
            "members": opponent_members or [make_war_member(tag="#O1", name="Opponent1")],
        },
        "start_time": "20260101T000000.000Z",
        "end_time": "20260102T000000.000Z",
    }

def make_user(discord_id="12345", display_name="TestUser", players=None):
    return {
        "display_name": display_name,
        "notification_settings": {},
        "players": players or [],
        "user_language": "en"
    }

def make_player(tag="#P1", name="Player1", th=15, verified=False, clan_tag=None):
    return {
        "player_tag": tag, "player_name": name,
        "th_level": th, "verified": verified,
        "current_clan_tag": clan_tag
    }
```

---

## 12. Coverage Targets

| Module Group | Smoke Target | Full Target | Notes |
|-------------|-------------|-------------|-------|
| `formatting.py` | 80% | 90% | Critical for visual output |
| `QBwarsim.py` | 70% | 85% | Math-heavy, parametrize edge cases |
| `db_manager.py` | — | 80% | Integration only (no smoke) |
| `cache_manager.py` | — | 75% | Integration only |
| `i18n.py` | 60% | 80% | Key fallback paths |
| `QBhelperfunctions.py` | 30% | 60% | Large file; test pure functions first |
| `QBdiscocmdshelper.py` | 20% | 50% | Large file; focus on security-critical |
| `ui_*.py` | — | 40% | View callbacks, modal submissions |
| `QBdiscordcmds.py` | — | 30% | Command dispatch; hard to unit-test |
| **Overall** | **40%** | **70%** | Weighted by module size |

---

## 13. What NOT to Test (Diminishing Returns)

| Area | Why skip |
|------|---------|
| `discord.py` / `coc.py` library internals | Tested by upstream |
| SQLite engine behavior | Tested by Python stdlib |
| Exact Discord message formatting (pixel-perfect) | Too brittle; test content/structure instead |
| `QapBot.py` main loop orchestration | Better covered by E2E; mocking everything is fragile |
| `QBcore.py` singleton init | Trivial; relies on environment |
| Static translations JSON structure | Covered by `check_translation_files.py` script |

---

## 14. Quick-Reference: Daily Workflow

```
┌─────────────────────────────────────────────────────────┐
│  Development Cycle                                       │
│                                                         │
│  1. Make code change                                    │
│  2. Run: .\run_tests.ps1                 (< 30 sec)    │
│     → GREEN? Continue coding                            │
│     → RED? Fix immediately                              │
│                                                         │
│  Pre-Deployment                                         │
│                                                         │
│  3. Run: .\run_tests.ps1 -Full --cov --cov-report=term  (< 5 min)                           │
│     → All GREEN + coverage ≥ 70%? Deploy               │
│     → RED or coverage drop? Investigate                 │
│                                                         │
│  CI (manual trigger only — workflow_dispatch)           │
│                                                         │
│  4. Smoke job runs first (fail-fast)                    │
│  5. Full job runs if smoke passes                       │
│  6. Coverage XML uploaded as CI artifact                │
└─────────────────────────────────────────────────────────┘
```

---

## 15. Key Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| **Global state pollution** (CACHE, CONFIG singletons) | Every test patches globals via fixtures; `autouse` where needed |
| **Async test complexity** | `pytest-asyncio` with `asyncio_mode = "auto"` eliminates boilerplate |
| **Test data drift** from production schema | `db_manager` tests create schema from code (same path as prod) |
| **Flaky timing tests** | Mock `time.time()` or `asyncio.sleep` — never depend on wall clock |
| **Large module monoliths** (QBdiscocmdshelper 4300+ lines) | Test pure functions first; defer interaction-heavy tests |
| **CoC API response format changes** | Fixture JSON files match current API; update fixtures when API changes |
| **Live smoke flakiness (rate limits / network)** | Keep live assertions coarse (keywords/structure), add retries/backoff, and run live smoke only in DEV with a dedicated channel |
| **Channel spam during live smoke** | Use `DEV_PLAYERREGISTRATION_CHANNEL_ID` exclusively and delete/cleanup test messages when possible |

---

## Related Documentation

- Architecture + patterns: ../CODE_STRUCTURE.md
- DB schema + migration rules: ../DATABASE_ARCHITECTURE.md  
- Rate limiting + parallel pipeline: ../RATE_LIMITING_IMPLEMENTATION.md
- Copilot pitfalls: ../COPILOT_PITFALLS_COOKBOOK.md
