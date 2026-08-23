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
    │ show_api_prompt=True?  │      │ call _assign_and_sync_   │
    └───────┬────────────────┘      │ roles_for_link()         │
            │                       │ (STEP 2.5, see below —   │
            ├────────────┐          │ runs BEFORE the prompt   │
            │            │          │ is sent, so SIMPLE-mode  │
            ▼            ▼          │ users get roles now,     │
      ┌─────────┐  ┌─────────┐      │ not after clicking a     │
      │ YES     │  │ NO      │      │ button)                  │
      └────┬────┘  └────┬────┘      └──────────┬───────────────┘
           │            │                      │
           │            ▼                      ▼
           │     ┌─────────────┐   ┌──────────────────────────┐
           │     │ Continue to │   │ Show ApiVerification     │
           │     │ Step 2.5    │   │ PromptView with buttons: │
           │     └─────────────┘   │ - Enter API token        │
           │                       │ - Skip verification      │
           │                       └──────────┬────────────────┘
           │                                  │
           │                                  └──► RETURN EARLY
           │                                       (notification check
           │                                       happens in button
           │                                       callbacks; role sync
           │                                       already ran above)
           └──────► (Show API prompt above)

═══════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────┐
│ STEP 2.5: ROLE ASSIGNMENT + CoC/CLAN ROLE SYNC               │
│ call _assign_and_sync_roles_for_link() (nested helper in     │
│ complete_account_linking_flow(), QBdiscocmdshelper.py)       │
└─────────────────┬───────────────────────────────────────────┘
                  │
      Runs from TWO call sites, safely (both are idempotent):
      1) Inside the "show_api_prompt" branch above, BEFORE the
         prompt message is sent.
      2) Here, for the normal (no-prompt) path — already-verified
         or token-verified flows land here directly.
                  │
                  ▼
    ┌───────────────────────────┐
    │ assign_member_role():     │
    │ SIMPLE mode → always      │
    │ STRICT mode → only if     │
    │ verified/token/admin      │
    └───────────┬────────────────┘
                │
                ▼
    ┌───────────────────────────┐
    │ sync_roles_for_user():    │
    │ (guild_role_manager.py)   │
    │ assigns highest CoC role  │
    │ + all clan roles for the  │
    │ user's linked accounts.   │
    │ Internally strict-mode-   │
    │ aware — no premature      │
    │ grants in STRICT mode.    │
    │ No-ops entirely if the    │
    │ guild has no role feature │
    │ enabled.                  │
    └───────────────────────────┘

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

Both **Verify** (`VerifyAccountModal.on_submit()`) and **Unlink**
(`UnlinkConfirmView._on_confirm()`) call `sync_roles_for_user()` directly after their
respective data change, so clan/member/CoC roles are rechecked immediately — see Key Decision
Point 6 above.

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

6. **Role Sync Timing** (Link Accounts / Unlink / Verify)
   - Location: `_assign_and_sync_roles_for_link()` in `complete_account_linking_flow()`
     (QBdiscocmdshelper.py); mirrored by direct `sync_roles_for_user()` calls in
     `UnlinkConfirmView._on_confirm()` and `VerifyAccountModal.on_submit()` (ui_registration.py)
   - Critical: In SIMPLE (non-strict) mode, role assignment must NOT be gated on verification
     — it must run immediately after linking/unlinking/verifying, not deferred until a later
     button click.
   - **BUG RISK (fixed 2026-07-25)**: Role assignment originally sat only after the whole
     verification block, so the "show API prompt" branch returned early before it ran —
     SIMPLE-mode users had to click Skip/Verify just to get roles they should already have had.
     Similarly, unlinking and the "My Accounts" Verify button didn't call `sync_roles_for_user()`
     at all, so role removal/CoC-rank updates waited for the periodic background sync (up to 30
     min for role-enabled clans, per the `_ROLE_GATE` in `QBhelperfunctions.py`).

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
- [ ] SIMPLE mode: member/clan/CoC roles assigned immediately on link, BEFORE clicking
      Skip/Enter-token on the API prompt (not just after)
- [ ] STRICT mode: no roles assigned until verified (token or admin override)
- [ ] Restoring a player from the UNASSIGNED pool (unlink then relink) gets a correct,
      freshly-fetched CoC role — not a stale/missing one from before it was unassigned

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
- [ ] Verifying via this flow triggers an immediate `sync_roles_for_user()` call (not just
      `assign_member_role()`) — was a gap fixed 2026-07-25

### My Accounts Workflow
- [ ] No accounts linked (error)
- [ ] Has accounts - show AccountManagementView with player selector
- [ ] Verify button → VerifyAccountModal → success triggers member-role assignment AND
      `sync_roles_for_user()`
- [ ] Set Primary button updates primary flag
- [ ] Unlink button → confirmation → success triggers `sync_roles_for_user()` (e.g. removing
      a user's only account in a clan should immediately drop that clan's role)

---

## Architecture Notes

### Modal Class Pattern (CRITICAL)
See Cardinal Rule 10 (`.github/copilot-instructions.md`) and `../qapbot/docs/CODE_STRUCTURE.md`
§ Discord.py Patterns for the full pattern + code example. One nuance specific to this flow
worth calling out here: only the `TextInput.placeholder` gets translated after `super().__init__()`
— the `label` stays hardcoded English (discord.py's Modal lifecycle requires TextInput labels as
class attributes, set before any translation context is available).

### Modular Flow Design
- **Linking** happens immediately in `_link_player_to_user()`
- **Verification** is a separate step in `verify_and_update_player()`
- **Role assignment + sync** runs via `_assign_and_sync_roles_for_link()` (nested helper in
  `complete_account_linking_flow()`) — BEFORE the notification check, and before the optional
  API-verification prompt is even shown, so SIMPLE-mode users don't wait on a button click
- **Notifications** checked last, in `check_and_prompt_war_notifications()`
- Each step can be entered independently or as part of full flow

### Cache Consistency
- `CACHE.persist_user(user_id)` must be called after any player data modification (write-through to database)
- Verification status updates persist immediately via write-through
- Notification settings updates persist immediately via write-through

---

## CWL Enrollment DM Re-Route on Ownership Change (2026-08-22, tracker #0019)

An enrollment DM records **who it was sent to**, not who owns the account. When a CoC account
changes its Discord owner, a DM already sitting in the OLD owner's inbox keeps pointing at that
account — and its sign-up button still works for them, deliberately (tracker #0016 widened
`CwlSignupResponseButton`'s guard to accept the recorded recipient, because rejecting them would
have broken DMs that were legitimately delivered). The result was that the previous owner could
answer for an account they no longer owned, while the new owner was never asked at all.

### The rule

| DM state (`cwl_player_season_status.status`) | Action |
|---|---|
| `pending` (unanswered) | delete the old owner's DM, re-send to the new owner |
| `confirmed` / `declined` | **completely untouched** — a response is a real historical fact and must never be retracted or re-asked |
| account **unlinked** or in the **UNASSIGNED pool** | **completely untouched** — that is ownership *removal*, not a change; there is nobody to re-route to, and the old recipient's button keeps working (project owner's explicit call for the live `#LLV0Y9PQ` / `.zuurn` case) |

### The trigger is a periodic sweep, deliberately — plus an instant fire-and-forget nudge

`reroute_cwl_enrollment_dms_after_ownership_change()` (`QBdiscocmdshelper_cwl.py`) runs once per
update cycle from `main()` (`QapBot.py`), after the `is_discord_available()` guard because it does
DM I/O. Two alternatives were considered and rejected for being the *sole* trigger:

- **Awaiting the reroute inline from the link/unlink path**: that is account-protection code
  (Cardinal Rule 2), and a re-route means two Discord round trips (a delete and a send, each
  internally retryable) — neither belongs inside the synchronous user-facing linking flow.
- **A startup-only idempotent pass**: the bot runs for weeks while an enrollment window lasts days,
  so it would routinely miss the window. The sweep subsumes startup anyway (the first cycle runs
  shortly after boot), so there is no separate startup call site.

Idempotent by construction (Cardinal Rule 12): once re-routed, `dmed_discord_id` matches the live
owner and the row stops matching the detection query. That idempotency is also what makes the
2026-08-23 addition below safe: nothing needs to coordinate with it.

**`fire_cwl_dm_reroute_after_ownership_change()`** (`QBdiscocmdshelper_cwl.py`, 2026-08-23): called
from `_link_player_to_user()` (`QBdiscocmdshelper.py`) right after its own persistence of an
ownership-displacing link completes (API token override, admin override, or the unverified-
duplicate replace — all three set a local `previous_owner_changed` flag). It does
`asyncio.create_task(...)` and returns immediately, **without awaiting** — this is the "hook the
link/unlink path" option from above, but scheduled rather than inline, which sidesteps exactly the
objection that ruled it out: the linking flow's own latency and failure modes are untouched, since
it never waits on the DM round trips. The periodic sweep keeps running unconditionally regardless,
as the safety net for whenever the background task loses a race against a slower DB write (it must
run after BOTH sides of the ownership change are persisted — see the function's own docstring),
fails outright, or the bot restarts before it fires. Net effect: an ownership flip is normally
re-routed within the same second instead of waiting up to one `SLEEP_INTERVAL` (default 300s).

### Order of operations, and why

1. **Retract the old DM first** (`cleanup_stale_cwl_enrollment_dms()`) — the old owner must lose
   the ability to answer even if the re-send below fails. Best-effort; never fatal.
2. **Clear the global dm_sent record** (`clear_cwl_player_dm_sent_sync()`). Load-bearing:
   `_send_cwl_enrollment_dm_batch()`'s global dedup would otherwise count the player as already
   contacted this season and send nothing at all.
3. **Re-point `cwl_signups.dmed_discord_id`** (`set_cwl_signup_dmed_discord_id_sync()`). This is
   what closes the hole for a legacy row whose `dm_sent_via_message_id` is NULL (predating
   2026-08-19) and whose message therefore cannot be deleted — the button's guard is
   `{signup.dmed_discord_id, live_discord_id}`, so once both name the new owner, the old owner's
   surviving DM correctly rejects them with `not_your_signup`.
4. **Re-send** via `_send_cwl_enrollment_dm_batch()` — never a hand-rolled send (Pitfall 38: the
   batch seeds the `cwl_signups` row the button needs). It also re-stamps the global row's
   `dmed_discord_id` on success.

A failed re-send leaves the row `status='pending'` with `dm_sent=0` — exactly the state "Notify
New Pool Members" already picks up, so it recovers on its own instead of stranding the player.

### Two deliberate limits

- **The old owner is not told their DM was withdrawn.** Precedent: `cleanup_stale_cwl_enrollment_dms()`
  (Delete Season) already retracts enrollment DMs silently, recorded in its docstring as the
  project owner's stated preference. A notice would also be odd on its face — it would tell someone
  about an account they no longer own.
- **`_MAX_DM_REROUTES_PER_CYCLE = 25`** bounds the blast radius, so a mass re-link (or a bug in the
  detection query) can never become an unbounded DM burst in one cycle. The remainder is picked up
  by the next cycle, and hitting the cap is logged.

### Ownership is resolved in Python, not SQL

`find_cwl_enrollment_dms_needing_reroute_sync()` returns candidates only (pending + `dm_sent=1` +
event still `signup_open`). The old-vs-new comparison uses `get_player_links_sync()`, which already
owns the verified-wins / UNASSIGNED-last dedup that decides which `user_players` row is the real
owner. Re-deriving that in SQL would be a near-duplicate (Cardinal Rule 4) and would silently pick
the wrong owner for a tag holding both a real and a stray row (see
`_cleanup_stray_unassigned_duplicates`).
