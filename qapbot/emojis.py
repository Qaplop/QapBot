"""Centralized emoji definitions for Discord custom emojis.

This module provides a single source of truth for all Discord custom emoji IDs
used throughout QapBot. Centralizing emoji definitions makes updates easier and
ensures consistency across the codebase.

Usage:
    from qapbot.emojis import BotEmojis

    message = f"{BotEmojis.ENABLED} Notifications enabled"
"""
import re
from typing import Optional


class BotEmojis:
    """Custom emoji constants for QapBot.
    
    All Discord custom emoji strings are defined here. When Discord emojis need
    to be updated, changes only need to be made in this single location.
    """
    
    # Status indicators
    ENABLED = "<:enabled:1455513859715629076>"
    DISABLED = "<:disabled:1455509604145434705>"
    VERIFIED = "<:verified:1454869848206213226>"
    GCHECK = "<:gcheck:1455226636415930631>"
    REDX = "<:redx:1454442783128551424>"
    
    # Notification frequency
    ONCE = "<:once:1455219555600171130>"
    REPEATED = "<:repeated:1455221464314679480>"
    
    # War types
    CWL = "<:cwl:1455219160102473779>"
    ALLWARS = "<:allwars:1455283063432024125>"
    
    # Town Hall levels
    TH01 = "<:TH01:1470128897160118272>"
    TH02 = "<:TH02:1470128859646136554>"
    TH03 = "<:TH03:1470128825655496755>"
    TH04 = "<:TH04:1470128772216000533>"
    TH05 = "<:TH05:1470128740309667850>"
    TH06 = "<:TH06:1470128706096730165>"
    TH07 = "<:TH07:1470128660668481659>"
    TH08 = "<:TH08:1470128630742122527>"
    TH09 = "<:TH09:1470128592884338779>"
    TH10 = "<:TH10:1470128541306716301>"
    TH11 = "<:TH11:1470128500974289264>"
    TH12 = "<:TH12:1470128409723277333>"
    TH13 = "<:TH13:1470128319822561441>"
    TH14 = "<:TH14:1470128279691460893>"
    TH15 = "<:TH15:1470128241271640075>"
    TH16 = "<:TH16:1470128189111009382>"
    TH17 = "<:TH17:1470128119833821487>"
    TH18 = "<:TH18:1470128059771257115>"
    
    # Heroes
    HERO_KING = "<:hero_king:1470127404973428880>"
    HERO_QUEEN = "<:hero_queen:1470127547873235065>"
    HERO_WARDEN = "<:hero_warden:1470127843261546496>"
    HERO_RC = "<:hero_RC:1470127680698712084>"
    HERO_MP = "<:hero_MP:1470127934084743211>"
    HERO_DD = "<:hero_DD:1499710549322240200>"


_CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w+:(\d+)>$")


def emoji_cdn_url(emoji: str) -> Optional[str]:
    """Discord's own CDN URL for a `<:name:id>`/`<a:name:id>` custom emoji string (one of the
    BotEmojis constants above) — for contexts that can't render Discord's native emoji markup
    at all, e.g. plain HTML in the CWL "Manage Enrollment" web Activity board
    (CWL_ROSTER_PLANNING_PLAN.md). Returns None if `emoji` isn't a recognizable custom-emoji
    string (a plain unicode emoji, for instance)."""
    match = _CUSTOM_EMOJI_RE.match(emoji)
    if not match:
        return None
    return f"https://cdn.discordapp.com/emojis/{match.group(1)}.png"


def th_icon_url(th_level: int) -> Optional[str]:
    """CDN image URL for a Town Hall level's icon, or None if no BotEmojis.THxx constant exists
    for it (e.g. a level newer than the last one QapBot's emoji set covers)."""
    emoji = getattr(BotEmojis, f"TH{th_level:02d}", None)
    return emoji_cdn_url(emoji) if emoji else None
