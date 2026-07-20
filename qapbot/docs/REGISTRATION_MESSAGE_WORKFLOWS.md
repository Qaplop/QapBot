# Registration Message Workflow Documentation

This document provides comprehensive flowcharts for all registration message buttons to help prevent regressions and understand the complexity of each workflow.

## Table of Contents
1. [Link Accounts Workflow](#link-accounts-workflow)
2. [War Notifications Workflow](#war-notifications-workflow)
3. [API Verification Workflow](#api-verification-workflow)
4. [My Accounts Workflow](#my-accounts-workflow)
5. [Key Decision Points](#key-decision-points)

---

## Link Accounts Workflow

```
┌─────────────────────────────────────┐
│ User clicks "Link Accounts" button  │
└──────────────┬──────────────────────┘
               │
               ▼
┌───────────────────────────────────────┐
│ Check if user has unverified accounts │
└───────────────┬───────────────────────┘
                │
                ├──────────────────────┐
                │                      │
                ▼                      ▼
    ┌───────────────────┐    ┌────────────────────┐
    │ HAS UNVERIFIED    │    │ NO UNVERIFIED      │
    └────────┬──────────┘    └─────────┬──────────┘
             │                         │
             ▼                         │
┌──────────────────────────┐           │
│ Show AccountActionView   │           │
│ with dropdown:           │           │
│ - Verify players         │           │
│ - Link new account       │           │
└──────────┬───────────────┘           │
           │                           │
           ├─────────────┐             │
           │             │             │
           ▼             ▼             │
    ┌──────────┐  ┌───────────┐        │
    │ Verify   │  │ Link new  │        │
    │ existing │  │ account   │        │
    └────┬─────┘  └─────┬─────┘        │
         │              │              │
         │              └──────┬───────┘
         ▼                     │
(Go to API Verification        │
 workflow - show               │
 VerifyAccountModal,           │
 continue below)               │
                               ▼
┌───────────────────────────────────────┐
│ Show PlayerSubstringModal             │
│ (Contains players from ALL subscribed │
│  clans in this guild, fetched via     │
│  live API; already-registered players │
│  are excluded)                        │
│                                       │
│ - Player Name/Tag field (required)    │
│ - API Token field (optional)          │
└───────────────┬───────────────────────┘
                │
                ▼
        ┌───────────────┐
        │ User submits  │
        └───────┬───────┘
                │
                ▼
    ┌───────────────────────┐
    │ Search for matches in │
    │ cached player list    │
    └───────┬───────────────┘
            │
            ├─────────────────────────────┐
            │                             │
            ▼                             ▼
   ┌─────────────────┐          ┌─────────────────┐
   │ NO MATCHES      │          │ MATCHES FOUND   │
   └────────┬────────┘          └────────┬────────┘
            │                            │
            ▼                            │
  ┌──────────────────┐                   │
  │ Try direct tag   │                   │
  │ lookup via CoC   │                   │
  │ API              │                   │
  └────────┬─────────┘                   │
           │                             │
           ├────────────┐                │
           │            │                │
           ▼            ▼                ▼
    ┌──────────┐  ┌──────────┐  ┌───────────────┐
    │ SUCCESS  │  │ FAILURE  │  │ 1 MATCH       │
    └────┬─────┘  └────┬─────┘  └───────┬───────┘
         │             │                │
         │             ▼                ▼
         │      ┌─────────────┐   ┌─────────────┐
         │      │ Show error  │   │ Single      │
         │      │ message     │   │ player      │
         │      └─────────────┘   │ identified  │
         │                        └──────┬──────┘
         │                               │
         └───────────────────────────────┤
                                         │
                                         ▼
                              ┌──────────────────┐
                              │ MULTIPLE MATCHES │
                              │ (2-25 players)   │
                              └────────┬─────────┘
                                       │
                                       ▼
                            ┌────────────────────┐
                            │ Show player        │
                            │ selection dropdown │
                            └─────────┬──────────┘
                                      │
                                      ▼
                            ┌───────────────────┐
                            │ User selects      │
                            │ specific player   │
                            └─────────┬─────────┘
                                      │
                                      ▼
                            ┌───────────────────┐
                            │ TOO MANY MATCHES  │
                            │ (>25 players)     │
                            │                   │
                            │ Clan filtering    │
                            │ happens HERE      │
                            └─────────┬─────────┘
                                      │
                                      ├──────────────────────┐
                                      │                      │
                                      ▼                      ▼
                         ┌─────────────────────┐  ┌──────────────────┐
                         │ Multiple clans      │  │ Single clan or   │
                         │ available           │  │ already filtered │
                         └──────────┬──────────┘  └────────┬─────────┘
                                    │                      │
                                    ▼                      ▼
                         ┌─────────────────────┐  ┌──────────────────┐
                         │ Show clan filter    │  │ Ask for more     │
                         │ dropdown            │  │ specific search  │
                         └──────────┬──────────┘  └──────────────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │ User selects clan   │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │ Re-filter players   │
                         │ by selected clan    │
                         └──────────┬──────────┘
                                    │
                                    └────────► (Back to MATCHES FOUND)

═══════════════════════════════════════════════════════════════════

AFTER PLAYER IDENTIFIED:

┌────────────────────────────────────────┐
│ call process_player_registration()    │
└───────────────┬────────────────────────┘
                │
                ▼
┌───────────────────────────────────────────┐
│ Normalize tag & fetch name from CoC API  │
│ (if not provided or "Unknown")            │
└───────────────┬───────────────────────────┘
                │
                ├──────────────────────┐
                │                      │
                ▼                      ▼
        ┌──────────────┐      ┌──────────────┐
        │ SUCCESS      │      │ FAILURE      │
        └──────┬───────┘      └──────┬───────┘
               │                     │
               │                     ▼
               │              ┌─────────────────┐
               │              │ Show error msg  │
               │              │ (invalid tag)   │
               │              └─────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│ call complete_account_linking_flow()     │
└───────────────┬──────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────┐
│ STEP 1: REGISTRATION                     │
│ call _link_player_to_user()              │
└───────────────┬──────────────────────────┘
                │
                ├─────────────────────────────────────────┐
                │                                         │
                ▼                                         ▼
    ┌───────────────────────┐              ┌──────────────────────────┐
    │ Player ALREADY LINKED │              │ Player NOT YET LINKED    │
    └──────────┬────────────┘              └────────────┬─────────────┘
               │                                        │
               ▼                                        ▼
    ┌──────────────────────┐              ┌────────────────────────────┐
    │ API token provided?  │              │ Check if verified player   │
    └──────┬───────────────┘              │ belongs to different user  │
           │                               └────────────┬───────────────┘
           ├───────────┐                               │
           │           │                               ├──────────────┐
           ▼           ▼                               │              │
    ┌──────────┐ ┌─────────┐                         ▼              ▼
    │ YES      │ │ NO      │                  ┌──────────────┐ ┌────────┐
    └────┬─────┘ └────┬────┘                  │ YES (ERROR)  │ │ NO     │
         │            │                       └──────┬───────┘ └───┬────┘
         │            ▼                              │             │
         │     ┌────────────────┐                   ▼             ▼
         │     │ Show "already  │            ┌─────────────┐  ┌──────────┐
         │     │ linked" msg +  │            │ Send error  │  │ Link     │
         │     │ notification   │            │ message     │  │ player   │
         │     │ prompt         │            └─────────────┘  │ to user  │
         │     └────────────────┘                             └────┬─────┘
         │                                                          │
         │                                                          │
         └──────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 2: API VERIFICATION                                    │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ├──────────────────────────────┐
                  │                              │
                  ▼                              ▼
    ┌──────────────────────────┐   ┌─────────────────────────┐
    │ Already verified         │   │ API token provided      │
    └───────────┬──────────────┘   └────────┬────────────────┘
                │                           │
                ▼                           ▼
    ┌──────────────────────┐   ┌────────────────────────────┐
    │ Skip to Step 3       │   │ call verify_and_update_    │
    └──────────────────────┘   │ player()                   │
                               └────────┬───────────────────┘
                                        │
                                        ├──────────────┐
                                        │              │
                                        ▼              ▼
                              ┌──────────────┐  ┌─────────────┐
                              │ VERIFIED     │  │ FAILED      │
                              └──────┬───────┘  └──────┬──────┘
                                     │                 │
                                     │                 ▼
                                     │      ┌────────────────────┐
                                     │      │ Send error message │
                                     │      │ RETURN FALSE       │
                                     │      │ (stop flow)        │
                                     │      └────────────────────┘
                                     │
                                     ▼
                          ┌───────────────────┐
                          │ Save to cache     │
                          └─────────┬─────────┘
                                    │
                                    │
                  ┌─────────────────┴──────────────────┐
                  │                                    │
                  ▼                                    ▼
    ┌──────────────────────────┐        ┌─────────────────────────┐
    │ No API token + not       │        │ show_api_prompt=True    │
    │ verified                 │        │                         │
    └───────────┬──────────────┘        └────────┬────────────────┘
                │                                │
                ▼                                ▼
    ┌────────────────────────┐      ┌──────────────────────────┐
    │ show_api_prompt=True?  │      │ Show ApiVerification     │
    └───────┬────────────────┘      │ PromptView with buttons: │
            │                       │ - Enter API token        │
            ├────────────┐          │ - Skip verification      │
            │            │          └──────────────────────────┘
            ▼            ▼                     │
      ┌─────────┐  ┌─────────┐               │
      │ YES     │  │ NO      │               └──► RETURN EARLY
      └────┬────┘  └────┬────┘                   (notification check
           │            │                         happens in button
           │            ▼                         callbacks)
           │     ┌─────────────┐
           │     │ Continue to │
           │     │ Step 3      │
           │     └─────────────┘
           │
           └──────► (Show API prompt above)

═══════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────┐
│ STEP 3: NOTIFICATION CHECK                                  │
│ call check_and_prompt_war_notifications()                   │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ├──────────────────────────────┐
                  │                              │
                  ▼                              ▼
    ┌──────────────────────────┐   ┌─────────────────────────┐
    │ Notifications ENABLED    │   │ Notifications DISABLED  │
    └───────────┬──────────────┘   └────────┬────────────────┘
                │                           │
                ▼                           ▼
    ┌──────────────────────┐   ┌────────────────────────────┐
    │ Send success message │   │ Show notification prompt   │
    │ only                 │   │ with buttons:              │
    └──────────────────────┘   │ - Activate                 │
                               │ - Skip                     │
                               └────────────────────────────┘

═══════════════════════════════════════════════════════════════════

API VERIFICATION PROMPT BUTTON CALLBACKS:

┌───────────────────────────────────────┐
│ User clicks "Enter API token" button  │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Show ApiTokenEntryModal               │
│ - API Token field (required)          │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ User submits modal                    │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Find player in user's account         │
└───────────────┬───────────────────────┘
                │
                ├──────────────────┐
                │                  │
                ▼                  ▼
    ┌───────────────┐    ┌────────────────┐
    │ FOUND         │    │ NOT FOUND      │
    └───────┬───────┘    └────────┬───────┘
            │                     │
            │                     ▼
            │              ┌─────────────────┐
            │              │ Show error msg  │
            │              └─────────────────┘
            │
            ▼
┌────────────────────────────────────────┐
│ call verify_and_update_player()        │
└────────────────┬───────────────────────┘
                 │
                 ├──────────────────┐
                 │                  │
                 ▼                  ▼
    ┌────────────────┐    ┌─────────────────┐
    │ VERIFIED       │    │ FAILED          │
    └────────┬───────┘    └────────┬────────┘
             │                     │
             ▼                     ▼
┌──────────────────────┐  ┌──────────────────┐
│ Save to cache        │  │ Show error msg   │
└─────────┬────────────┘  └──────────────────┘
          │
          ▼
┌───────────────────────────────────────┐
│ call check_and_prompt_war_            │
│ notifications()                       │
│ (Same as Step 3 above)                │
└───────────────────────────────────────┘

─────────────────────────────────────────────────

┌───────────────────────────────────────┐
│ User clicks "Skip" button             │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ call check_and_prompt_war_            │
│ notifications()                       │
│ (Same as Step 3 above)                │
└───────────────────────────────────────┘
```

---

## War Notifications Workflow

```
┌───────────────────────────────────────┐
│ User clicks "War Notifications"       │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Check if user has any linked accounts │
└───────────────┬───────────────────────┘
                │
                ├──────────────────────┐
                │                      │
                ▼                      ▼
    ┌───────────────────┐    ┌────────────────────┐
    │ NO ACCOUNTS       │    │ HAS ACCOUNTS       │
    └────────┬──────────┘    └─────────┬──────────┘
             │                          │
             ▼                          ▼
┌──────────────────────────┐  ┌─────────────────────────┐
│ Show error: "Please      │  │ Show UnifiedNotification│
│ link an account first"   │  │ View with settings:     │
└──────────────────────────┘  │                         │
                              │ - Current status        │
                              │ - War type filter       │
                              │ - Notification mode     │
                              │ - Language              │
                              │                         │
                              │ Buttons:                │
                              │ - Enable/Disable        │
                              │ - Change War Type       │
                              │ - Change Mode           │
                              │ - Change Language       │
                              └─────────────────────────┘

═══════════════════════════════════════════════════════════════════

BUTTON: Enable/Disable

┌───────────────────────────────────────┐
│ User clicks Enable/Disable toggle     │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Toggle war_reminders setting          │
│ (True ↔ False)                        │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Save to cache                         │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Update the view with new status       │
└───────────────────────────────────────┘

─────────────────────────────────────────────────

BUTTON: Change War Type

┌───────────────────────────────────────┐
│ User clicks "Change War Type"         │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Toggle between:                       │
│ - All Wars ↔ CWL Only                 │
│ (No dropdown, instant toggle)         │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Update notification_type setting      │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Save to cache                         │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Update view with new settings         │
└───────────────────────────────────────┘

─────────────────────────────────────────────────

BUTTON: Change Mode

┌───────────────────────────────────────┐
│ User clicks "Change Mode"             │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Toggle between:                       │
│ - Once ↔ Repeated                     │
│ (No dropdown, instant toggle)         │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Update notification_mode setting      │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Save to cache                         │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Update view with new settings         │
└───────────────────────────────────────┘
```

---

## API Verification Workflow

```
┌───────────────────────────────────────┐
│ User clicks "API Verification" button │
│ in registration message                    │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Check if user has any linked accounts │
└───────────────┬───────────────────────┘
                │
                ├──────────────────────┐
                │                      │
                ▼                      ▼
    ┌───────────────────┐    ┌────────────────────┐
    │ NO ACCOUNTS       │    │ HAS ACCOUNTS       │
    └────────┬──────────┘    └─────────┬──────────┘
             │                          │
             ▼                          ▼
┌──────────────────────────┐  ┌─────────────────────────┐
│ Show error: "Please      │  │ Check for unverified    │
│ link an account first"   │  │ accounts                │
└──────────────────────────┘  └─────────┬───────────────┘
                                        │
                                        ├──────────────────┐
                                        │                  │
                                        ▼                  ▼
                            ┌───────────────────┐  ┌──────────────────┐
                            │ HAS UNVERIFIED    │  │ ALL VERIFIED     │
                            └────────┬──────────┘  └────────┬─────────┘
                                     │                      │
                                     ▼                      ▼
                          ┌────────────────────┐  ┌──────────────────┐
                          │ Show dropdown with │  │ Show success msg │
                          │ unverified players │  │ "All accounts    │
                          │ (NO "Link new"     │  │ are verified"    │
                          │  option here)      │  └──────────────────┘
                          └────────┬───────────┘
                                   │
                                   ▼
                          ┌────────────────────┐
                          │ User selects       │
                          │ specific player    │
                          └────────┬───────────┘
                                   │
                                   ▼
                          ┌────────────────────┐
                          │ Show               │
                          │ VerifyAccount      │
                          │ Modal              │
                          └────────┬───────────┘
                                                      │
                                                      ▼
                                          ┌────────────────────────┐
                                          │ - API Token field      │
                                          │   (required)           │
                                          └────────┬───────────────┘
                                                   │
                                                   ▼
                                          ┌────────────────────────┐
                                          │ User submits modal     │
                                          └────────┬───────────────┘
                                                   │
                                                   ▼
                                          ┌────────────────────────┐
                                          │ Validate API token     │
                                          │ not empty              │
                                          └────────┬───────────────┘
                                                   │
                                                   ├──────────────┐
                                                   │              │
                                                   ▼              ▼
                                          ┌──────────────┐ ┌─────────────┐
                                          │ EMPTY        │ │ NOT EMPTY   │
                                          └──────┬───────┘ └──────┬──────┘
                                                 │                │
                                                 ▼                ▼
                                        ┌─────────────────┐ ┌──────────────┐
                                        │ Show error msg  │ │ Find player  │
                                        └─────────────────┘ │ in user's    │
                                                            │ account      │
                                                            └──────┬───────┘
                                                                   │
                                                                   ├─────────────┐
                                                                   │             │
                                                                   ▼             ▼
                                                          ┌──────────────┐ ┌────────┐
                                                          │ NOT FOUND    │ │ FOUND  │
                                                          └──────┬───────┘ └───┬────┘
                                                                 │             │
                                                                 ▼             ▼
                                                        ┌─────────────────┐ ┌──────────────────┐
                                                        │ Show error msg  │ │ call verify_and_ │
                                                        └─────────────────┘ │ update_player()  │
                                                                            └────────┬─────────┘
                                                                                     │
                                                                                     ├────────────┐
                                                                                     │            │
                                                                                     ▼            ▼
                                                                            ┌──────────────┐ ┌────────┐
                                                                            │ VERIFIED     │ │ FAILED │
                                                                            └──────┬───────┘ └───┬────┘
                                                                                   │            │
                                                                                   ▼            ▼
                                                                        ┌────────────────────┐ ┌──────────────┐
                                                                        │ Save to cache      │ │ Show error   │
                                                                        └─────────┬──────────┘ │ message      │
                                                                                  │            │ (keep welcome│
                                                                                  │            │ message)     │
                                                                                  │            └──────────────┘
                                                                                  ▼
                                                                        ┌────────────────────┐
                                                                        │ call check_and_    │
                                                                        │ prompt_war_        │
                                                                        │ notifications()    │
                                                                        │ (keep welcome msg) │
                                                                        └────────────────────┘
```

---

## My Accounts Workflow

```
┌───────────────────────────────────────┐
│ User clicks "My Accounts"             │
└───────────────┬───────────────────────┘
                │
                ▼
┌───────────────────────────────────────┐
│ Check if user has any linked accounts │
└───────────────┬───────────────────────┘
                │
                ├──────────────────────┐
                │                      │
                ▼                      ▼
    ┌───────────────────┐    ┌────────────────────┐
    │ NO ACCOUNTS       │    │ HAS ACCOUNTS       │
    └────────┬──────────┘    └─────────┬──────────┘
             │                          │
             ▼                          ▼
┌──────────────────────────┐  ┌─────────────────────────┐
│ Show error: "No linked    │  │ Show AccountManagement  │
│ accounts yet"             │  │ overview + player       │
└──────────────────────────┘  │ selector dropdown        │
                              │                         │
                              │ Actions per player:     │
                              │ - Verify (opens         │
                              │   VerifyAccountModal)   │
                              │ - Set Primary           │
                              │ - Unlink (confirm flow) │
                              └─────────────────────────┘
```

---

## Key Decision Points

### Critical Flow Control Points

1. **Player Already Linked Check** (Link Accounts workflow)
   - Location: `_link_player_to_user()` in QBdiscocmdshelper.py
   - Branches: Already linked (check if api_token provided) OR New link (check security)
   - **BUG RISK**: Must respect `api_token` parameter to continue to verification

2. **API Token Response Mode** (All workflows)
   - Location: `complete_account_linking_flow()` in QBdiscocmdshelper.py
   - Critical: Must use `interaction.response.*` vs `interaction.followup.*` based on whether the interaction was already responded to/deferred
   - **BUG RISK**: Calling `response.send_message()` after a prior `defer()` (or calling `followup.send()` before responding) causes interaction errors

3. **registration message Preservation** (API Verification workflow)
   - Location: `VerifyAccountModal.on_submit()` and `ApiTokenEntryModal.on_submit()`
   - Critical: NEVER pass `action_view_interaction` to delete registration message
   - **BUG RISK**: Passing `player_selection_interaction` deletes registration message

4. **Notification Prompt Display** (All workflows)
   - Location: `check_and_prompt_war_notifications()` in QBdiscocmdshelper.py
   - Critical: Deletes `player_selection_interaction` if provided
   - **BUG RISK**: Passing wrong interaction reference deletes registration message

5. **Duplicate Error Messages** (Link Accounts)
   - Critical: `complete_account_linking_flow()` already sends errors when verification fails
   - **BUG RISK**: Sending error twice when verification fails
   - **FIXED**: Removed duplicate error sending in `process_player_registration()` errors when verification fails
   - **BUG RISK**: Sending error twice when verification fails

### Interaction Response Patterns

| Context | Correct Method | use_followup | Notes |
|---------|---------------|--------------|-------|
| Modal submission | Usually `interaction.response.defer(ephemeral=True)` → `interaction.followup.send()` | `True` | Use `response.send_message()` only for immediate validation errors before deferring |
| Button click | `interaction.response.send_modal()` or `send_message()` | Varies | Depends on immediate action |
| After sending message | `interaction.followup.send()` | `True` | Already responded |
| Command usage | `interaction.response.send_message()` first | Starts `False`, then `True` | First response, then followups |

### Message Deletion Rules

| Message Type | When to Delete | When to Keep |
|-------------|----------------|--------------|
| registration message | **NEVER** | Always |
| Player Selection Dropdown | After player selected | N/A |
| Clan Filter Dropdown | After clan selected (only shown when >25 matches) | N/A |
| API Verification Prompt | After button clicked | N/A |
| Action View (verify dropdown) | After success | On failure (for retry) |

### Common Regression Patterns

1. **Webhook Expiration**
   - Symptom: "Webhook expired" log entries
   - Root Cause: Interaction lifecycle mismatch (responding too late or using the wrong response method after deferring)
   - Prevention: Defer early for long work; after deferring, use `followup.send()` / `edit_original_response()`

2. **Duplicate Messages**
   - Symptom: Two identical error messages sent to user
   - Root Cause: Error handling at multiple levels without early return
   - Prevention: Check if callee already sent message

3. **registration message Deletion**
   - Symptom: "Ursprüngliche Nachricht wurde gelöscht" (Original message deleted)
   - Root Cause: Passing `action_view_interaction` or similar to functions that delete it
   - Prevention: Never pass registration message interaction for deletion

4. **Early Returns Breaking Flow**
   - Symptom: Verification or notification prompts not showing
   - Root Cause: Code returns early when it should continue to next step
   - Prevention: Trace full flow, ensure `api_token` presence doesn't break flow

---

## Testing Checklist

### Link Accounts Workflow
- [ ] **User has unverified accounts**: Show AccountActionView dropdown
  - [ ] Select "Verify existing" → VerifyAccountModal shown
  - [ ] Select "Link new account" → PlayerSubstringModal shown
- [ ] **User has no unverified accounts**: Directly show PlayerSubstringModal
- [ ] Single exact match (name)
- [ ] Single exact match (tag)
- [ ] Multiple matches (2-25) → Player selection dropdown
- [ ] Too many matches (>25) with multiple clans → Clan filter dropdown
- [ ] Too many matches (>25) with single clan or already filtered → Ask for more specific search
- [ ] No matches - invalid tag
- [ ] No matches - valid tag (CoC API lookup)
- [ ] Player already linked - no API token → Show success + notification prompt
- [ ] Player already linked - with correct API token → Verify and continue
- [ ] Player already linked - with wrong API token → Show error, stop flow
- [ ] Player belongs to different verified user (security check)
- [ ] API token in first modal - correct token
- [ ] API token in first modal - wrong token
- [ ] API prompt shown - Enter token (correct)
- [ ] API prompt shown - Enter token (wrong)
- [ ] API prompt shown - Skip verification
- [ ] Notifications already enabled (simple success)
- [ ] Notifications disabled (show prompt)

### War Notifications Workflow
- [ ] No accounts linked (error)
- [ ] Accounts linked - show UnifiedNotificationView
- [ ] Toggle Enable/Disable
- [ ] Change War Type (toggle All Wars ↔ CWL Only)
- [ ] Change Mode (toggle Once ↔ Repeated)

### API Verification Workflow
- [ ] No accounts linked (error)
- [ ] All accounts verified (success message)
- [ ] Has unverified - show dropdown (NO "Link new" option)
- [ ] Select player → VerifyAccountModal shown
- [ ] Enter API token - correct
- [ ] Enter API token - wrong
- [ ] Enter API token - empty (validation)
- [ ] registration message remains after success
- [ ] registration message remains after failure

---

## Architecture Notes

### Modal Class Pattern (CRITICAL)
```python
# CORRECT - TextInput as class attribute
class MyModal(discord.ui.Modal, title="Hardcoded Title"):
    my_input = discord.ui.TextInput(label="English Label", placeholder="Default")
    
    def __init__(self, guild_id=None):
        super().__init__()  # NO title parameter!
        # Only translate placeholder, NOT label
        self.my_input.placeholder = t('translated.placeholder', guild_id=guild_id)

# WRONG - Will cause "Unknown interaction" errors
class MyModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Title")  # WRONG!
        self.my_input = discord.ui.TextInput(...)  # WRONG!
```

### Modular Flow Design
- **Linking** happens immediately in `_link_player_to_user()`
- **Verification** is a separate step in `verify_and_update_player()`
- **Notifications** checked last in `check_and_prompt_war_notifications()`
- Each step can be entered independently or as part of full flow

### Cache Consistency
- `CACHE.persist_user(user_id)` must be called after any player data modification (write-through to database)
- Verification status updates persist immediately via write-through
- Notification settings updates persist immediately via write-through
