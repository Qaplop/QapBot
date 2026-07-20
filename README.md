# QapBot - Clash of Clans Discord Bot

A powerful, modular Discord bot designed for Clash of Clans clan management, featuring automated leaderboards, war predictions, player notifications, and comprehensive clan family support.

## 📦 Library Versions

- **discord.py**: 2.7.1
- **coc.py**: 4.0.0
- **aiosqlite**: 0.22.1
- **Python**: 3.14 or higher (tested with 3.14.5)

## 🌟 Features

### Core Functionality
- **Automated Leaderboards**: Generate and post clan war leaderboards with multiple display modes
- **War Predictions**: Simulate war outcomes and calculate win probabilities using advanced algorithms
- **War Notifications**: Automated DM reminders for players with remaining attacks
- **Clan Family Support**: Manage multiple clans as families with unified tracking and leaderboards
- **Player Registration**: Interactive player account linking with verification system
- **Multi-language Support**: Internationalization (i18n) with guild-specific language preferences

### Leaderboard Modes
- **Current War**: Real-time stats from active wars
- **Historical Performance**: Stats from previous wars with time filtering
- **Sorting Options**: By stars, attacks, participation, win rate, and more
- **Time Filters**: Current war, current month, current year, all-time
- **Display Formats**: Optimized for both Discord and terminal output
- **Automatic Updates**: Subscribed channels receive updates on war state changes
- **Mode Registry**: Extensible system for adding new leaderboard modes
- **Hash-Based Optimization**: Prevents duplicate posts when content hasn't changed

### Administrative Tools
- Subscription management across channels and servers
- Player account linking and verification
- Diagnostic commands for troubleshooting
- Log analysis with notification counting
- Data consistency checking

### Notification System
- **DM Reminders**: Automated direct messages for players with remaining attacks towards the end of a war
- **Notification Modes**: 
  - `once` - Single reminder after war starts (no repeat)
  - `repeated` - Periodic reminders until attacks are used
- **War Type Filtering**: Choose which war types trigger notifications:
  - All Wars (Regular CWs, Friendly Wars and CWL)
  - Clan War Leagues only (CWL)
- **Per-Player Configuration**: Each player manages their own notification preferences
- **Intelligent Timing**: Respects minimum time windows to avoid spam
- **State Tracking**: Remembers which players have been notified to prevent duplicates
- **Aggregated Messages**: Reduces Discord notification spam with consolidated updates
- **Integration**: Seamlessly works with player registration system

## 🚀 Quick Start

### Prerequisites
- Python 3.10 or higher
- Discord bot token ([create one here](https://discord.com/developers/applications))
- Clash of Clans API credentials ([get them here](https://developer.clashofclans.com))

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/Qaplop/QapBot.git
   cd QapBot
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   ```

3. **Activate virtual environment**
   - Windows: `.\venv\Scripts\activate`
   - Linux/Mac: `source venv/bin/activate`

4. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

5. **Configure environment**
   
   Create a `.env` file in the root directory (Only placeholders here! Never commit real tokens or passwords. Keep `.env` in .gitignore!):
   ```env
   # Discord Configuration
   DISCORD_TOKEN=your_discord_bot_token_here
   DISCORD_TOKEN_DEV=your_dev_bot_token_here
   DISCORD_GUILD_ID_FOR_CLEANUP=your_test_server_guild_id
   DEV_PLAYERREGISTRATION_CHANNEL_ID=your_dev_channel_id_for_registration_testing
   
   # Clash of Clans API (Production)
   COC_API_EMAIL=your_coc_api_email
   COC_API_PASSWORD=your_coc_api_password
   
   # Clash of Clans API (Development)
   COC_API_EMAIL_DEV=your_dev_coc_api_email
   COC_API_PASSWORD_DEV=your_dev_coc_api_password
   
   # Bot Configuration
   SERVER_ADMIN=your_discord_user_id
   QAPBOT_LOG_LEVEL=INFO  # Options: DEBUG, INFO, WARNING, ERROR
   SLEEP_INTERVAL=300  # Main loop interval in seconds
   
   # Notification Settings
   NOTIFICATION_HOURS_BEFORE_END=4  # Hours before war end to send reminders
   NOTIFICATION_BATCH_DELAY=2  # Seconds between notification batches
   NOTIFICATION_MAX_RETRIES=1  # Max retry attempts for failed notifications

   # Monte Carlo Simulation
   SIM_MULTIPROCESS_ENABLED=true   # true/false — enable multi-process parallel simulation
   SIM_MAX_WORKERS=0               # 0 = all logical cores (capped at 8); >0 = use exactly that many

   # Production data / server paths (placeholders only - DO NOT commit real prod paths)
   # Set `PROD_DATA_DIR` to the production BOT ROOT (the scripts expect data under ${PROD_DATA_DIR}/data)
   # PROD_DATA_DIR and PROD_BOT_ROOT can be identical, but if different PROD_BOT_ROOT is the place where the code files reside (e.g. slow HDD) while
   # PROD_DATA_DIR is where the data files reside (e.g. fast SSD). The scripts will look for data files under ${PROD_DATA_DIR}/data.
   # PROD_SSD_UNC is the UNC path to PROD_DATA_DIR where the data files reside. This is used for faster access to data files on a server or network share.
   PROD_DATA_DIR=<path-to-prod-bot-root>
   PROD_BOT_ROOT=<path-to-prod-bot-root>
   PROD_SSD_UNC=\\<server-machine_HOST>\<share>\QapBot
  ```

6. **Run the bot**
   ```bash
   python QapBot.py
   ```

## 📋 Commands

### User Commands
- `/subscribe` - Subscribe a channel to clan or family leaderboard updates
- `/unsubscribe` - Unsubscribe a channel from updates
- `/leaderboard` - Display leaderboards with various modes
- `/subscriptions` - List subscriptions for the channel or server
- `/clans` - List all tracked clans
- `/list_families` - Display all clan families
- `/list_players` - Show players in a clan or family
- `/status` - Display bot status and statistics
- `/ping` - Check bot latency
- `/help` - Show help information

### Admin Commands
- `/admin` - Administrative diagnostic actions with multiple action choices:
  - **POST_REGISTRATION** - Post player registration welcome message (server admin)
  - **CLEANUP_MESSAGES** - Clean up orphaned messages in current channel (server admin)
  - **CLEANUP_MESSAGES_ALL** - Clean up messages across all channels (bot admin only)
  - **CHECK_LOGS** - Scan and summarize QapBot logs (bot admin only)
  - **CHECK_DATA** - Validate data consistency and check current wars (bot admin only)
  - **LIST_ALL_SUBSCRIPTIONS** - View all channel subscriptions (bot admin only)
- `/link_account` - Link a CoC player to a Discord user (with autocomplete)
- `/import_data` - Import player accounts from ClashPerk embed
- `/list_accounts` - List all registered Discord accounts with verification status
- `/list_families` - List all clan families and their member clans
- `/list_players` - List players for a clan or family with tags and names
- `/removeclan` - Remove a clan from tracking if not subscribed

## 🏗️ Architecture

### Project Structure
```
QapBot/
├── QapBot.py              # Main bot orchestration and periodic loops
├── QBcore.py              # Core bot and CoC client initialization
├── QBdiscordcmds.py       # Discord command handlers and registration
├── QBhelperfunctions.py   # Leaderboard generation and Discord posting
├── QBcsvhandling.py       # JSON war data loading and file management
├── QBwarsim.py            # War simulation and probability calculations
├── requirements.txt       # Python dependencies
├── qapbot/
│   ├── cache_manager.py   # In-memory cache manager (CACHE) with write-through DB persistence
│   ├── db_manager.py      # SQLite database operations (WarHistoryDB, 17 tables)
│   ├── config.py          # Configuration values and validation logic
│   ├── constants.py       # Centralized constants (API limits, timeouts, thresholds)
│   ├── exceptions.py      # Custom exception hierarchy (16 exception classes)
│   ├── formatting.py      # Leaderboard rendering, alignment, MODE_REGISTRY
│   ├── war_notifications.py  # War notification system with DM reminders
│   ├── ui_common.py       # Shared Discord UI utilities
│   ├── ui_registration.py # Welcome/registration UI (Views/Modals)
│   ├── ui_notifications.py # Notification UI
│   ├── ui_clan_management.py # Clan management UI
│   ├── i18n.py            # Internationalization (i18n) module
│   ├── discord_health.py  # Discord API retry wrapper with rate limit handling
│   ├── emojis.py          # Emoji definitions for Discord messages
│   ├── QBdiscocmdshelper.py  # Command helper functions and autocomplete
│   ├── QBdiscocmdshelper_admin_command.py  # Admin command helpers
│   └── translations/      # Language files (en.json, de.json)
└── data/
    ├── qapbot.db          # SQLite database (hot: current + previous calendar month, WAL mode)
    ├── qapbot_history.db  # SQLite database (history: everything older, ATTACHed as schema 'history')
    ├── temp/              # Active war data (JSON format)
    ├── archive/           # Completed war data (JSON format)
    ├── logs/              # Rotating bot logs (daily)
    └── maindata/          # Legacy JSON files (kept for reference, DB is source of truth)
```

### Key Design Principles
- **Single-Source-of-Truth**: All runtime data managed via CACHE object
- **Write-Through Persistence**: All cache mutations immediately persisted to SQLite database
- **Unified Message Tracking**: Prevents spam with intelligent message reuse
- **Defensive Programming**: Assumes external dependencies will fail
- **Modular Architecture**: Clear separation of concerns across modules
- **Data Integrity First**: Cache consistency prioritized over features
- **Context-Aware Errors**: Custom exception hierarchy with operation details for better debugging
- **Configuration-as-Code**: Centralized constants module eliminates magic numbers

### Hot/History Database Split

To keep the live database small and nightly maintenance (VACUUM) fast, war/CWL data is
split across two SQLite files:

- **`data/qapbot.db`** (hot) — always holds the current calendar month + the immediately
  preceding calendar month, in full, for `war_attacks`, `war_summary`, `cwl_league_groups`
  and `cwl_league_rounds`. All other tables (clans, users, subscriptions, guild config,
  `player_name_index`, etc.) live here permanently.
- **`data/qapbot_history.db`** (history) — everything older than the hot window, for the
  same 4 time-series tables.

The history DB is `ATTACH`ed as schema `history` on every database connection (both the
async connection and every pooled sync connection), so almost all existing queries work
unchanged; only "all-time"/historical queries explicitly `UNION` `main.<table>` with
`history.<table>`. Once a month (the 1st, during the nightly maintenance window), a batched
migration job moves data older than the retention window from hot to history — see
`WarHistoryDB.monthly_history_migration()` in `qapbot/db_manager.py` and
`qapbot/docs/CLAN_AND_WAR_CYCLE_ARCHITECTURE.md` for the full design.

## 🔧 Configuration

### Discord Application Setup

**CRITICAL**: QapBot uses TWO separate Discord applications for development and production:

#### DEV App (`DISCORD_TOKEN_DEV`):
- Used for development and testing
- Registers guild-specific commands on test server (when `DISCORD_GUILD_ID` > 0)
- On startup: Clears any global commands (cleanup from previous PROD mode testing with DEV token)

#### PROD App (`DISCORD_TOKEN`):
- Used for live bot deployment
- Registers global commands for all servers (when `DISCORD_GUILD_ID` = 0)
- On startup: Uses DEV token to clear guild commands from test server (cleanup for when not actively developing)

#### Why This Matters:
- Prevents command conflicts when switching between DEV and PROD modes
- Ensures users only see relevant commands (guild commands in test server, global commands in prod servers)
- Critical for maintaining proper Discord command precedence
- Command clearing logic implemented in `QapBot.py`

### Environment Variables
| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `DISCORD_TOKEN` | Production Discord bot token | Yes | - |
| `DISCORD_TOKEN_DEV` | Development Discord bot token (for testing) | Yes | - |
| `DISCORD_GUILD_ID` | **DEV/PROD mode switch**: your test server's guild ID enables DEV mode (DEV token/credentials, guild-scoped commands); `0` enables PROD mode (PROD token, global commands). Set it in `.env`, not as a machine-global variable — a global value leaks into every process (e.g. test runs) and takes precedence over `.env`. | No | 0 (PROD) |
| `DISCORD_GUILD_ID_FOR_CLEANUP` | Test server guild ID for command cleanup | Yes | - |
| `DEV_PLAYERREGISTRATION_CHANNEL_ID` | DEV-only channel ID for player registration welcome message (restricts where the message can be bumped/reposted in dev mode) | No | 0 |
| `COC_API_EMAIL` | Clash of Clans API email (production) | Yes | - |
| `COC_API_PASSWORD` | Clash of Clans API password (production) | Yes | - |
| `COC_API_EMAIL_DEV` | Clash of Clans API email (development) | Yes | - |
| `COC_API_PASSWORD_DEV` | Clash of Clans API password (development) | Yes | - |
| `SERVER_ADMIN` | Numeric Discord user ID of the bot administrator (username accepted as deprecated fallback) | Yes | - |
| `QAPBOT_LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) | No | INFO |
| `SLEEP_INTERVAL` | Main loop interval in seconds | No | 300 |
| `NOTIFICATION_HOURS_BEFORE_END` | Hours before war end to send reminders | No | 4 |
| `NOTIFICATION_BATCH_DELAY` | Seconds delay between notification batches | No | 2 |
| `NOTIFICATION_MAX_RETRIES` | Maximum retry attempts for failed notifications | No | 1 |
| `PROD_DATA_DIR` | Base directory for `data/`, `archive/`, and `archive_old/` — set this on prod to point to the external SSD. **Ignored in DEV mode even if set.** | No | *(bot root)* |
| `DB_PATH` | Path to the hot SQLite database file (current + previous calendar month of war data) | No | `data/qapbot.db` |
| `HISTORY_DB_PATH` | Path to the history SQLite database file (ATTACHed as schema `history`; everything older than `DB_PATH`'s retention window) | No | `data/qapbot_history.db` |

## 🏭 Production Environment

### Deployment Locations

| | Development | Production |
|---|---|---|
| **Machine** | Windows PC (VS Code) | Prod server-machine `HOST-NAME` |
| **Bot root** | `<DEV_BOT_ROOT>` | `<PROD_BOT_ROOT>` |
| **data/ & archive/** | `<DEV_BOT_ROOT>/data` | `${PROD_DATA_DIR}/data` *(set in .env or your environment)* |
| **archive_old/** | `<DEV_BOT_ROOT>/archive_old` | `${PROD_DATA_DIR}/../archive_old` *(set via `PROD_DATA_DIR`)* |
| **archive_compressed/** | *(not used in dev)* | `<PROD_BOT_ROOT>/archive_compressed` *(HDD)* |
| **Windows UNC to prod SSD** | `<PROD_SSD_UNC>` | *(local)* |

> **Note:** The SSD data location (`data/`, `archive/`, `archive_old/`) is **optional** and configured
> via the `PROD_DATA_DIR` environment variable in `.env` (e.g. `PROD_DATA_DIR=${PROD_DATA_DIR}`).
> When not set, all directories default to subdirectories of the bot root. This variable is **automatically
> ignored in DEV mode** even if present in `.env`.

> ⚠️ **WARNING — Production data access from Windows:**
-> The prod SSD is accessible at `<PROD_SSD_UNC>` from the dev machine.
> **NEVER read, write, or browse this path without explicit user confirmation.**
> Any accidental write or deletion here directly corrupts the live production database.

### Production Server
- **Platform**: Any standard Linux/Windows machine (e.g., physical or virtual, Ubuntu)
- **Python Version**: 3.10 or higher (tested with 3.14.5)
- **Mode**: Set environment to use global commands
- **Logs**: `data/logs/qapbot.log` in your installation directory

### Switching Between Environments
1. **DEV Mode**: Set `DISCORD_GUILD_ID` to your test server ID
2. **PROD Mode**: Set `DISCORD_GUILD_ID=0`
3. Bot automatically uses correct token (`DISCORD_TOKEN_DEV` or `DISCORD_TOKEN`)
4. Command cleanup happens automatically on startup

## 🧪 Testing

**Work in progress.**

## 🌐 Internationalization

QapBot supports multiple languages through JSON translation files:
- Translation files located in `qapbot/translations/`
- Language preferences stored per Discord server
- Use `t()` function for all user-facing text
- Currently supported: English (more languages can be added)

##  Data Management

### Cache System
- **Single-Source-of-Truth**: CACHE object in `cache_manager.py` manages all runtime data
- **Write-Through Persistence**: All cache mutations immediately persisted to SQLite (`data/qapbot.db`, plus `data/qapbot_history.db` for older war/CWL data)
- CoC API caching (`CoCClanCache`) with stale-while-revalidate strategy (soft TTL 280s, hard TTL 600s)
- All persistent data stored in SQLite database (WAL mode for reliability)
- Consistency checks on startup with validation
- **Database-Only Architecture**: No JSON files used for primary data storage (only temp war files remain as JSON)

### File Structure
- **SQLite Database (hot)**: `data/qapbot.db` - Primary data store (current + previous calendar month of war/CWL data, plus users, subscriptions, config, etc.)
- **SQLite Database (history)**: `data/qapbot_history.db` - Older war/CWL data (`war_attacks`, `war_summary`, `cwl_league_groups`, `cwl_league_rounds`), migrated from the hot DB once a month
- **Active Wars**: `data/temp/{CLAN1_TAG}_{CLAN2_TAG}_war_data.json`
- **Archived Wars**: `archive/{CLAN1_TAG}_{CLAN2_TAG}_war_data.json` (moved after war ends)
- **Legacy Files**: `data/maindata/*.json` - Kept for reference (DB is source of truth)

### Data Flow
1. War data fetched from CoC API via `CACHE.coc_clan_cache.get_clan()`
2. Stored as JSON in `data/temp/` during active war
3. Loaded into memory via `CACHE.load_temp_war_stats(clan_tag)`
4. Moved to `archive/` when war ends
5. Appended to war history database via `CACHE.db_manager.add_war_records()`
6. All business logic accesses data through CACHE methods (never direct DB or file I/O)

## 🤝 Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Follow existing code patterns and conventions
4. Add/update tests as needed
5. Update documentation
6. Submit a pull request

See [copilot-instructions.md](.github/copilot-instructions.md) and [CODE_STRUCTURE.md](CODE_STRUCTURE.md) for detailed development guidelines.

### Development Workflow

1. **Pre-Implementation Analysis**
   - Identify data flow through the system
   - Map all dependencies and affected modules
   - Define success criteria
   - Plan rollback strategy
   - Estimate blast radius

2. **Implementation Best Practices**
   - Make small, testable changes (incremental development)
   - Assume external dependencies will fail (defensive programming)
   - Prioritize cache consistency over features
   - Follow existing patterns for cache management and file I/O
   - Document with clear docstrings and type hints

3. **Testing & Validation**
   - Test leaderboard and war prediction output
   - Verify cache operations and persistence
   - Check Discord message formatting (2000 char limit)
   - Validate error handling for API failures

4. **Documentation**
   - Update changelog in `changelog.txt` under `Change-Log:` section
   - Update `copilot-instructions.md` for architectural changes
   - Update `CODE_STRUCTURE.md` for new modules or flows

### Key Conventions
- **Single-Source-of-Truth**: All runtime data managed via CACHE object
- **Unified Message Tracking**: Prevents spam with message reuse
- **CoC API Calls**: Always use `CACHE.coc_clan_cache.get_clan()` for caching
- **Formatting**: Use `qapbot/formatting.py` MODE_REGISTRY for all leaderboards
- **Modularization**: Place features in the most relevant module
- **Debugging**: Use logging (DEBUG level) instead of print statements

## 📝 License

This project is private. All rights reserved.

## 🙏 Acknowledgments

- Built with [discord.py](https://github.com/Rapptz/discord.py)
- CoC API integration via [coc.py](https://github.com/mathsman5133/coc.py)
- Inspired by the Clash of Clans community

## 📞 Support

For issues, questions, or feature requests, please [open an issue on GitHub](https://github.com/Qaplop/QapBot/issues).

## 🔄 Version History

See [changelog.txt](changelog.txt) for detailed changelog and version history.

## 🐛 Troubleshooting

### Common Issues

**Bot doesn't respond to commands:**
- Check bot is online and connected
- Verify command sync completed (check startup logs)
- Ensure bot has proper permissions in the channel
- Try `/ping` command to test basic connectivity

**Commands not appearing:**
- Wait 5-10 minutes after bot startup for Discord to sync commands
- Check `DISCORD_GUILD_ID` setting (0 for global, guild ID for testing)
- Restart Discord client to refresh command cache
- For DEV mode: Ensure commands registered on correct test server

**Leaderboard not updating:**
- Check clan is subscribed: `/subscriptions`
- Verify clan is in active war state
- Check logs for API errors: `data/logs/qapbot.log`
- Ensure CoC API credentials are valid

**Cache/Data issues:**
- Use `/admin CHECK_DATA` to validate data consistency
- Check database integrity: `sqlite3 data/qapbot.db "PRAGMA integrity_check;"` (and likewise for `data/qapbot_history.db`)
- Review logs for backup/restore messages
- Restart bot to reload cache from database

**Notification issues:**
- Verify player accounts are linked via welcome message (My Accounts button)
- Check notification settings via welcome message (War Notifications button)
- Ensure war type matches notification preferences
- Review notification state in database (`notification_state` table)

### Log Analysis
Use `/admin CHECK_LOGS` to scan recent logs for:
- Error patterns and frequencies
- Notification counts
- Admin actions
- API failures

### Getting Help
- Check logs: `data/logs/qapbot.log` in your installation directory
- Review backlog: [backlog.txt](backlog.txt) for known issues
- Open GitHub issue with:
  - Error messages from logs
  - Steps to reproduce
  - Expected vs actual behavior
  - Bot configuration (sanitized)

---

**QapBot** - Empowering Clash of Clans communities with intelligent automation 🤖⚔️
