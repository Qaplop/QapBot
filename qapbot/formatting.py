"""
Leaderboard rendering and Unicode-aware column alignment for QapBot statistics display.

This module provides advanced text formatting for leaderboards in Discord and terminal environments, with normalization and display width measurement for international and emoji-rich player names. All leaderboard output uses Unicode-aware width logic and normalization for consistent display.

Key Features:
- Player name normalization for consistent width calculations and display
- Discord-specific display width estimation using custom logic and wcwidth
- Dynamic column width adjustment based on content and mode
- Best-practice cell padding: overshoot, trim, micro-fill, and ideographic space compensation
- Comprehensive handling of zero-width, double-width, and special Unicode characters
- Multiple output styles (Discord code blocks, terminal plain text)

Business Rules:
- All leaderboard rendering uses Unicode-aware width logic for Discord and terminal
- Player name normalization is required before width measurement
- Column widths are dynamically adjusted for content and mode
- Defensive programming for all Unicode edge cases

Integration:
- Used by QBhelperfunctions.generate_leaderboard_text()
- Supports all leaderboard modes defined in qapbot/stats/modes
- Handles both current war data and historical leaderboard generation
"""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

import unicodedata
import math
from wcwidth import wcwidth, wcswidth  # type: ignore[import-untyped]
from typing import List, Dict, Any, Union, Callable, Optional, Set, cast
import logging

# ANSI escape codes for highlighting a row inside a Discord ```ansi code block.
# Discord supports a small subset of ANSI SGR codes there (bold/underline + 8
# foreground/8 background colors) — this is the only way to bold/color text
# without breaking the table's monospace column alignment. Not rendered by
# every mobile client (older ones show the raw escape sequence as text).
_ANSI_HIGHLIGHT = "[1;33m"  # bold + yellow foreground
_ANSI_RESET = "[0m"

# MODE_REGISTRY contains leaderboard mode definitions with lambda sort functions
# Type checker cannot infer types for lambda parameter 'p' (player dict), so we suppress warnings
MODE_REGISTRY: Dict[str, Dict[str, Any]] = cast(Dict[str, Dict[str, Any]], {
    "currentwar": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9), ("Ø🔥/Atk", 9)],
        "sort_key": lambda p: (-p.get("Stars", 0), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "Stats for current war incl. prediction of war outcome."
    },
    "attack": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9), ("Ø🔥/Atk", 9)],
        "sort_key": lambda p: (-p.get("Stars", 0), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "Most stars won."
    },
    "avgstars": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9), ("Ø🔥/Atk", 9)],
        "sort_key": lambda p: (  # type: ignore[misc, union-attr, arg-type]
            -round(p.get("Stars", 0) / (p.get("Attacks", 0) + p.get("Missed_Attacks", 0)), 2) if (p.get("Attacks", 0) + p.get("Missed_Attacks", 0)) > 0 else 0,
            -p.get("TH_lvl", 0),
            -p.get("Stars", 0),
            p.get("Player", "").lower()
        ),
        "description": "Average stars per attack."
    },
    "avgstarsbyth": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9), ("Ø🔥/Atk", 9)],
        "sort_key": lambda p: (  # type: ignore[misc, union-attr, arg-type]
            -round(p.get("Stars", 0) / (p.get("Attacks", 0) + p.get("Missed_Attacks", 0)), 2) if (p.get("Attacks", 0) + p.get("Missed_Attacks", 0)) > 0 else 0,
            -p.get("Stars", 0),
            p.get("Player", "").lower()
        ),
        "description": "Average stars per attack sorted by TH level.",
        "grouped_by_th": True
    },
    "attackdefratio": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9), ("Atk/Def", 7)],
        "sort_key": lambda p: (-(p.get("Stars", 0) / p.get("Defensive_Stars", 1) if p.get("Defensive_Stars", 0) else float('inf')), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "Ratio of stars won to stars suffered."
    },
    "defense": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Def-Stars", 9), ("Defs", 4), ("Stars/Def", 9)],
        "sort_key": lambda p: (p.get("Stars_per_Def", 0.0), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "Fewest stars per defense suffered."
    },
    "missedattacks": {
        "columns": [("Player", 17), ("TH", 2), ("Missed", 6), ("Attacks", 7), ("Stars", 5), ("Def-Stars", 9)],
        "sort_key": lambda p: (-p.get("Missed_Attacks", 0), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "Number of missed attacks in wars."
    },
    # Add CWL variants for compatibility
    "attack_cwl": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9), ("Ø🔥/Atk", 9)],
        "sort_key": lambda p: (-p.get("Stars", 0), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "Most stars won (CWL only)."
    },
    "avgstars_cwl": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9), ("Ø🔥/Atk", 9)],
        "sort_key": lambda p: (  # type: ignore[misc, union-attr, arg-type]
            -round(p.get("Stars", 0) / (p.get("Attacks", 0) + p.get("Missed_Attacks", 0)), 2) if (p.get("Attacks", 0) + p.get("Missed_Attacks", 0)) > 0 else 0,
            -p.get("TH_lvl", 0),
            -p.get("Stars", 0),
            p.get("Player", "").lower()
        ),
        "description": "Average stars per attack (CWL only)."
    },
    "attackdefratio_cwl": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9), ("Atk/Def", 7)],
        "sort_key": lambda p: (-(p.get("Stars", 0) / p.get("Defensive_Stars", 1) if p.get("Defensive_Stars", 0) else float('inf')), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "Ratio of stars won to stars suffered (CWL only)."
    },
    "defense_cwl": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Def-Stars", 9), ("Defs", 4), ("Stars/Def", 9)],
        "sort_key": lambda p: (p.get("Stars_per_Def", 0.0), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "Fewest stars per defense suffered (CWL only)."
    },
    "missedattacks_cwl": {
        "columns": [("Player", 17), ("TH", 2), ("Missed", 6), ("Attacks", 7), ("Stars", 5), ("Def-Stars", 9)],
        "sort_key": lambda p: (-p.get("Missed_Attacks", 0), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "Number of missed attacks in wars (CWL only)."
    },
    "cwlinfo": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9)],
        "sort_key": lambda p: (-p.get("Stars", 0), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "CWL season overview: all 7 rounds with rosters, scores, and win predictions."
    },
    "cwlinfo_comp": {
        "columns": [("Player", 17), ("TH", 2), ("Stars", 5), ("Attacks", 7), ("Ø Stars/Atk", 11), ("Def-Stars", 9)],
        "sort_key": lambda p: (-p.get("Stars", 0), -p.get("TH_lvl", 0), p.get("Player", "").lower()),  # type: ignore[misc, union-attr]
        "description": "CWL overview: standard vs. skill-adjusted win predictions."
    },
    "cwlgroup": {
        "columns": [("Rank", 4), ("Clan", 22), ("Stars", 6), ("Destr.%", 8)],
        "sort_key": lambda p: (p.get("group_rank", 9999),),  # type: ignore[misc, union-attr]
        "description": "CWL league-group standings: rank, stars, and destruction for all 8 clans."
    }
})  # Close cast() wrapper
DEFAULT_MODE = "attack"


# Suppress type warnings for MODE_REGISTRY dictionary structure with dynamic lambdas
MODE_REGISTRY: Dict[str, Dict[str, Any]] = MODE_REGISTRY  # type: ignore[misc, no-redef]


# Modes that are inherently CWL-specific (or, for "currentwar", have no CWL
# variant at all) and therefore have no "<mode>_cwl" counterpart in
# MODE_REGISTRY above.
NO_CWL_SUFFIX_MODES = frozenset({"cwlgroup", "cwlinfo", "cwlinfo_comp", "currentwar"})


def apply_cwl_mode_suffix(mode: str, cwl_only: bool) -> str:
    """Append "_cwl" to *mode* when cwl_only is requested, unless *mode*
    already ends with "_cwl" or has no "_cwl" variant (see NO_CWL_SUFFIX_MODES).

    Without this guard, forcing cwl_only=True (e.g. via the /leaderboard
    `season` option, which implies CWL) on an inherently-CWL mode like
    "cwlgroup" produces the unrecognised string "cwlgroup_cwl", which falls
    through the special-mode dispatch in QBdiscordcmds.leaderboard() and gets
    rendered as a generic image/text leaderboard instead of the intended CWL
    group-standings image.
    """
    if not cwl_only or mode.endswith("_cwl") or mode in NO_CWL_SUFFIX_MODES:
        return mode
    return f"{mode}_cwl"


def text_display_width_float(s: str) -> float:
    """
    Estimate display width for Discord, using custom logic for special Unicode characters, emoji, and micro spaces.
    - Uses empirical widths for East Asian, emoji, and Latin small capital chars.
    - Ignores zero-width and formatting controls.
    - Sums float widths for accurate Discord rendering.
    Args:
        s (str): Input string
    Returns:
        float: Estimated display width for Discord monospace font
    """
    # width helpers used in text_display_width_float to calculate accurate render widths in Discord
    #
    # UNICODE SPACE CHARACTER EMPIRICAL FINDINGS (2026-02-07):
    # Tested on both PC and iPad Discord with 40× repetitions to measure actual rendering widths.
    # 
    # KEY FINDINGS FOR WIDTH ADJUSTMENT:
    # 1. Ideographic Space (\u3000): Renders at width 1.665 on BOTH PC and iPad
    #    - Use for large width adjustments (adds ~0.665 per space replaced)
    # 2. Braille Pattern Blank (\u2800): Renders at width 1.113 on BOTH PC and iPad
    #    - Use for small width adjustments (adds ~0.113 per space replaced)
    # 3. All other Unicode space variants (hair, thin, en, em, etc.): Render at width 1.0 on iPad
    #    - These are USELESS for width adjustments on mobile devices
    #    - Since CoC is primarily mobile, optimize for iPad/mobile rendering
    # 4. STRATEGY: Use Ideographic Space and Braille Pattern Blank to fine-tune column alignment
    #    by replacing regular blanks in padding when needed to reach exact target width
    #
    SPECIAL_CHAR_WIDTHS = {
        '\u3000': 1.665,  # Ideographic space
        '\u2800': 1.113,  # Braille Pattern Blank
        '⚡': 2.25,      # Lightning symbol
        '⚔': 2.25,       # Crossed swords
        '☽': 0.97,       # U+263D Crescent Moon
        '๏': 1.11,       # U+0E4F Thai Character Fongman
        '☾': 0.97,       # U+263E Crescent Moon
        '⊙': 1.48,       # U+2299 Circled Dot Operator
        '☠': 1.48,       # U+2620 Skull and Crossbones
        '💀': 2.21,       # U+2620 Skull and Crossbones
        '☢': 1.5,        # U+2622 Radioactive sign
        'ʍ': 1.00,       # U+028D Latin Small Letter Turned W
        '♥': 1.00,       # Heart symbol
        '♠': 1.00,       # Spade symbol
        '♣': 1.00,       # Club symbol
        '♤': 1.07,       # Spade symbol
        '♡': 1.07,       # Heart symbol
        '♧': 1.07,       # Club symbol
        '❣': 1.165,      # Heavy heart exclamation mark ornament (U+2763)
        '⭐': 2.21,       # Star Emoji (U+2B50) - best cross-platform consistency for iPad/PC
        'ᗷ': 1.14,       # U+15F7
        'ᗩ': 1.33,       # U+15F9
        'ᑎ': 1.26,       # U+145E
        'ᘜ': 1.18,       # U+161C
        'ᇈ': 1.50,       # U+11C8 Hangul Jongseong Nieun-Pansios
        '✌': 2.25,      # Victory hand (U+270C)
        '✌️': 2.25,      # Victory hand with Variation Selector-16 (emoji style)
        '㉿': 1.67,       # U+32FF SQUARE CORPORATION - renders as full-width character
        '👑': 2.21,       # Crown Emoji (U+1F451) - best cross-platform consistency for iPad/PC
        'Ֆ': 1.20,       # U+0546 Armenian Capital Letter Feh
        'Λ': 1.67,       # U+039B Greek Capital Letter Lambda - Greek block, cat=Lu, not caught by any range
        'ω': 1.67,       # U+03C9 Greek Small Letter Omega - Greek block, cat=Ll, not caught by any range
        'Ξ': 1.67,       # U+039E Greek Capital Letter Xi - Greek block, cat=Lu, not caught by any range
        'σ': 1.67,       # U+03C3 Greek Small Letter Sigma - Greek block, cat=Ll, not caught by any range
        '⭕': 2.21,      # U+2B55 Large Red Circle (often used as "O" symbol in player names)
        '•': 1.00,       # U+2022 BULLET - appears in every BB •Name• player; cat=Po, not caught by any range
        'ʜ': 1.00,       # U+029C LATIN LETTER SMALL CAPITAL H - IPA Extensions (0x0250-0x02AF), outside 1D00-1D7F range
        'ʙ': 1.00,       # U+0299 LATIN LETTER SMALL CAPITAL B - IPA Extensions
        'ʟ': 1.00,       # U+029F LATIN LETTER SMALL CAPITAL L - IPA Extensions
        'ɴ': 1.00,       # U+0274 LATIN LETTER SMALL CAPITAL N - IPA Extensions
        'ꜱ': 0.75,       # U+A731 LATIN LETTER SMALL CAPITAL S - Unified Canadian Ext. (0xA700+), outside 1D00-1D7F
        'ϟ': 0.97,       # U+03DF GREEK SMALL LETTER KOPPA - Greek block, cat=Ll, not caught by any range
        '⍭': 1.13,       # U+236D APL FUNCTIONAL SYMBOL STILE TILDE - APL block (0x2300-0x23FF), cat=So but not in emoji ranges
        '➂': 1.367,          # U+2782 DINGBAT NEGATIVE CIRCLED DIGIT THREE - Dingbats block (0x2700-0x27BF), cat=No, not in emoji ranges
        '❆': 1.267,          # U+2746 SNOWFLAKE - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
        '✞': 1.083,          # U+271E LATIN CROSS - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
        '⚝': 1.467,          # U+269D OUTLINED WHITE STAR - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
        '➳': 1.34,          # U+2793 HEAVY WIDE-HEADED RIGHTWARDS ARROW - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
        '⚜': 1.2,          # U+269C FLEUR-DE-LIS - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
        '✯': 1.367,          # U+269C FLEUR-DE-LIS - Dingbats block (0x2700-0x27BF), cat=So, not in emoji ranges
    }
    ZERO_WIDTH_CATS = {"Mn", "Me", "Cf"}
    ZERO_WIDTH_CHARS = {"\u200d", "\ufe0f"}  # ZWJ, VS16
    def discord_wcwidth(ch: str) -> float:
        if ch in SPECIAL_CHAR_WIDTHS:
            return SPECIAL_CHAR_WIDTHS[ch]
        # Check if ch is an emoji (Unicode emoji block or presentation property)
        if unicodedata.category(ch) in {"So"} and (
            0x1F600 <= ord(ch) <= 0x1F64F or  # Emoticons
            0x1F300 <= ord(ch) <= 0x1F5FF or  # Misc Symbols and Pictographs
            0x1F680 <= ord(ch) <= 0x1F6FF or  # Transport and Map
            0x1F700 <= ord(ch) <= 0x1F77F or  # Alchemical Symbols
            0x1F780 <= ord(ch) <= 0x1F7FF or  # Geometric Shapes Extended
            0x1F800 <= ord(ch) <= 0x1F8FF or  # Supplemental Arrows-C
            0x1F900 <= ord(ch) <= 0x1F9FF or  # Supplemental Symbols and Pictographs
            0x1FA00 <= ord(ch) <= 0x1FA6F or  # Chess Symbols
            0x1FA70 <= ord(ch) <= 0x1FAFF or  # Symbols and Pictographs Extended-A
            0x2600 <= ord(ch) <= 0x26FF or    # Misc symbols (includes ⚡)
            0x2700 <= ord(ch) <= 0x27BF       # Dingbats
        ):
            return 2.25
        # Empirical: East Asian chars (Han, Katakana, Hiragana, Hangul, etc.)
        east_asian = unicodedata.east_asian_width(ch)
        if east_asian in ("F", "W"):
            return 1.67
        # Empirical: Latin small capital letters (Unicode block U+1D00–U+1D7F)
        codepoint = ord(ch)
        if 0x1D00 <= codepoint <= 0x1D7F and unicodedata.category(ch) == "Ll":
            return 1.00 #was 0.97
        return float(wcwidth(ch))
    total = 0.0
    for c in s:
        if c in ZERO_WIDTH_CHARS or unicodedata.category(c) in ZERO_WIDTH_CATS:  # Should not happen since player-names are already normalized
            continue
        total += discord_wcwidth(c)
    # DEBUG: Commented out to reduce log verbosity - uncomment for character width debugging
    # logging.debug(f"Total length of {s}: {total}")
    return total

def text_display_width(s: str) -> int:
    """
    Estimate display width for Discord, rounding up the float value from text_display_width_float.
    Args:
        s (str): Input string
    Returns:
        int: Integer display width for Discord monospace font
    """
    return int(math.ceil(text_display_width_float(s)))

# Truncate to target display width (reserving suffix width if truncated)
def truncate_to_width(name: str, target_width: int) -> str:
    """
    Truncate a string to fit within the target display width, preserving as much of the name as possible.
    Args:
        name (str): Input string
        target_width (int): Target display width
    Returns:
        str: Truncated string fitting within the target width
    """
    cur_w = text_display_width(name)
    if cur_w <= target_width:
        return name
    out: List[str] = []
    for ch in name:
        candidate = ''.join(out + [ch])
        if text_display_width(candidate) > target_width:
            break
        out.append(ch)
    return ''.join(out)

def right_pad_number(val: Union[str, int, float], width: int) -> str:
    """
    Right-align a numeric value within a specific display width, using Unicode-aware width calculation.
    Args:
        val (str|int|float): Numeric value to format
        width (int): Target display width
    Returns:
        str: Right-aligned string padded with leading spaces
        
    Example:
        right_pad_number(42, 5) -> "   42"
        right_pad_number(2.5, 6) -> "   2.5"
    """
    s = f"{val}" if isinstance(val, str) else f"{val}"
    cur = text_display_width(s)
    if cur >= width:
        return s
    return " " * (width - cur) + s

# Step 1.5: Player name normalization helper
def normalize_player_name(raw: str) -> str:
    """
    Normalize a raw player name prior to width measurement and truncation.
    Operations:
      1. Strip leading/trailing Unicode whitespace
      2. Remove zero-width/formatting controls (except RTL marks)
      3. Unicode compatibility normalization (NFKC)
      3.1 Map dash variants to ASCII hyphen
      4. Replace emoji + variation selector sequences with base glyph
      5. Collapse internal runs of whitespace
      6. Remove residual control characters
      7. Add LTR (left-to-right) marks to force LTR display for RTL text
      8. Final trim; if empty, fall back to original stripped form
    Returns:
        str: Canonical display string for deterministic width calculations
    """
    if not raw:
        return raw
    import unicodedata, re
    original = raw
    # 1. Trim broad whitespace (use regex capturing all Unicode spaces)
    SPACE_RE = re.compile(r"[\s\u00A0\u2000-\u200B\u202F\u205F\u3000]+")
    # Pre-trim using strip on common spaces then regex
    s = original.strip()
    # 2. Remove specific zero-width / format chars (but keep the string content)
    REMOVE_CHARS = {
        '\u200d','\u200c','\uFE0F','\uFE0E','\uFEFF','\u2060','\u200E','\u200F',
        '\u202A','\u202B','\u202C','\u202D','\u202E','\u0489'
    }
    s = ''.join(ch for ch in s if ch not in REMOVE_CHARS)
    # 3. NFKC normalization
    try:
        s = unicodedata.normalize('NFKC', s)
    except Exception:
        pass
    # 3.1 Dash normalization
    DASH_VARIANTS = {'\u2012','\u2013','\u2014','\u2015','\u2010','\u2212','\u2011'}  # figure, en, em, horiz bar, hyphen, minus sign, non-breaking hyphen
    if any(d in s for d in DASH_VARIANTS):
        s = ''.join('-' if ch in DASH_VARIANTS else ch for ch in s)
    # 4. Replace lightning variants (26A1 + variation selector) with base 26A1
    # After removal of VS16 above, sequence may already be normalized; still enforce single char mapping.
    LIGHTNING_BASE = '\u26A1'
    s = s.replace(LIGHTNING_BASE + '\uFE0F', LIGHTNING_BASE)
    s = s.replace(LIGHTNING_BASE + '\uFE0E', LIGHTNING_BASE)
    s = s.replace('⊙', '●')
    s = s.replace('✌', '✌️')
    s = s.replace('⚔', '⚔️')
    s = s.replace('☠', '💀')
    s = s.replace('❤️', '♥').replace('❤', '♥')
    # Consolidate old star characters to single star emoji for consistent cross-platform rendering
    s = s.replace('☆', '⭐').replace('★', '⭐').replace('✰', '⭐')
    # Consolidate crown/chess piece characters to crown emoji for consistent cross-platform rendering
    s = s.replace('♛', '👑').replace('♔', '👑').replace('♚', '👑').replace('♕', '👑').replace('♕', '👑')
    # 5. Collapse internal whitespace (including exotic spaces) to single space
    s = SPACE_RE.sub(' ', s)
    # 6. Remove residual control characters (category starting with 'C')
    filtered: List[str] = []
    for ch in s:
        try:
            if unicodedata.category(ch).startswith('C') and ch != '\t':
                continue
        except Exception:
            pass
        filtered.append(ch)
    s = ''.join(filtered)
    # 6.5 Prevent Discord spoiler-markdown trigger: || opens a spoiler in plain-text contexts.
    # Insert ZWSP (U+200B, category Cf) between the two pipes so the pair is broken.
    # ZWSP is already in ZERO_WIDTH_CATS so text_display_width_float counts it as 0 — no
    # column-alignment impact.  Works in both plain-text and code-block output.
    if '||' in s:
        s = s.replace('||', '|\u200B|')
    # 7. Final trim
    s = s.strip()
    if not s:
        # Fallback: at least return stripped original (without removed control chars)
        return original.strip()
    # 8. Wrap with LTR marks only if RTL characters detected
    # Detect RTL script characters (Arabic, Hebrew, Syriac, etc.)
    RTL_SCRIPT_RANGES = [
        (0x0590, 0x05FF),  # Hebrew
        (0x0600, 0x06FF),  # Arabic
        (0x0700, 0x074F),  # Syriac
        (0x0750, 0x077F),  # Arabic Supplement
        (0x0780, 0x07BF),  # Thaana
        (0x07C0, 0x07FF),  # N'Ko
        (0x0800, 0x083F),  # Samaritan
        (0x0840, 0x085F),  # Mandaic
        (0x0860, 0x086F),  # Syriac Supplement
        (0x08A0, 0x08FF),  # Arabic Extended-A
        (0xFB1D, 0xFB4F),  # Hebrew presentation forms
        (0xFB50, 0xFDFF),  # Arabic Presentation Forms-A
        (0xFE70, 0xFEFF),  # Arabic Presentation Forms-B
    ]
    
    has_rtl = False
    for ch in s:
        code_point = ord(ch)
        for start, end in RTL_SCRIPT_RANGES:
            if start <= code_point <= end:
                has_rtl = True
                break
        if has_rtl:
            break
    
    if has_rtl:
        # Wrap with LTR marks to force left-to-right display in tables
        LRM = '\u200E'  # Left-to-Right Mark
        return f"{LRM}{s}{LRM}"
    
    return s

def text_display_width_float_special(s: str, original_name: str = "") -> float:
    """
    Special width calculation for specific player names that render differently in Discord.
    
    Args:
        s (str): Normalized string to measure
        original_name (str): Original player name before normalization
    
    Returns:
        float: Estimated display width with special adjustments
    """
    # Special case for "صكار الشويلات" - Arabic characters render at 0.90 width
    if "صكار" in original_name and "الشويلات" in original_name:
        total = 0.0
        for c in s:
            codepoint = ord(c)
            # Apply 0.90 multiplier to Arabic characters only
            if (0x0600 <= codepoint <= 0x06FF or  # Arabic
                0x0750 <= codepoint <= 0x077F or  # Arabic Supplement
                0x08A0 <= codepoint <= 0x08FF or  # Arabic Extended-A
                0xFB50 <= codepoint <= 0xFDFF or  # Arabic Presentation Forms-A
                0xFE70 <= codepoint <= 0xFEFF):   # Arabic Presentation Forms-B
                total += 0.90
            else:
                # Use standard calculation for non-Arabic chars
                total += text_display_width_float(c)
        return total
    # Default: use standard calculation
    return text_display_width_float(s)

def _find_optimal_space_combination(cell: str, gap: float, target_width: int, width_float_func: Callable[[str], float]) -> str:
    """
    Find optimal combination of space characters to fill gap and reach target width.
    
    Strategy:
    - Count trailing regular spaces (can be replaced with wider spaces)
    - Try combinations of:
        - Replacing spaces: space→Ideographic (+0.665), space→Braille (+0.113)
        - Appending spaces: regular (+1.0), Braille (+1.113), Ideographic (+1.665)
    - Pick combination with minimal |final_width - target_width|
    
    Args:
        cell: Current cell string with potential trailing spaces
        gap: Remaining gap to fill (target_width - current_width)
        target_width: Target column width
        width_float_func: Function to calculate display width
        
    Returns:
        Optimally padded cell string
    """
    if gap < 0.05:  # Already close enough
        return cell
    
    # Space width constants
    SPACE = 1.0
    BRAILLE = 1.113
    IDEOGRAPHIC = 1.665
    BRAILLE_GAIN = 0.113   # Gain when replacing space with Braille
    IDEO_GAIN = 0.665      # Gain when replacing space with Ideographic
    
    # Count trailing regular spaces that can be replaced
    trailing_spaces = len(cell) - len(cell.rstrip(' '))
    base_cell = cell.rstrip(' ')
    
    best_cell = cell
    best_diff = abs(gap)
    
    # Limit search space for efficiency
    max_ideo_replace = min(trailing_spaces, int(gap / IDEO_GAIN) + 2)
    max_braille_replace = min(trailing_spaces, int(gap / BRAILLE_GAIN) + 3)
    max_ideo_append = min(2, int(gap / IDEOGRAPHIC) + 1)
    max_braille_append = min(3, int(gap / BRAILLE) + 1)
    max_space_append = min(4, int(gap) + 1)
    
    # Try combinations: replacements + appends
    for n_ideo_replace in range(max_ideo_replace + 1):
        for n_braille_replace in range(min(trailing_spaces - n_ideo_replace + 1, max_braille_replace + 1)):
            # Calculate remaining regular spaces after replacements
            remaining_spaces = trailing_spaces - n_ideo_replace - n_braille_replace
            if remaining_spaces < 0:
                continue
            
            # Width gained from replacements
            replace_gain = n_ideo_replace * IDEO_GAIN + n_braille_replace * BRAILLE_GAIN
            # remaining_gap = gap - replace_gain  # Calculated but not used; gap is used directly below
            
            # Try appending spaces
            for n_ideo_append in range(max_ideo_append + 1):
                for n_braille_append in range(max_braille_append + 1):
                    for n_space_append in range(max_space_append + 1):
                        # Calculate total width added
                        append_gain = (
                            n_ideo_append * IDEOGRAPHIC +
                            n_braille_append * BRAILLE +
                            n_space_append * SPACE
                        )
                        total_gain = replace_gain + append_gain
                        
                        # Check if this is better (closer to gap without much overshoot)
                        diff = abs(gap - total_gain)
                        overshoot = total_gain - gap
                        
                        # Prefer solutions that don't overshoot or have minimal overshoot
                        if diff < best_diff and overshoot < 0.5:
                            # Build candidate cell
                            candidate = (
                                base_cell +
                                ' ' * remaining_spaces +
                                '\u3000' * n_ideo_replace +
                                '\u2800' * n_braille_replace +
                                '\u3000' * n_ideo_append +
                                '\u2800' * n_braille_append +
                                ' ' * n_space_append
                            )
                            
                            # Verify width (sanity check)
                            actual_width = width_float_func(candidate)
                            actual_diff = abs(actual_width - target_width)
                            
                            if actual_diff < abs(width_float_func(best_cell) - target_width):
                                best_diff = diff
                                best_cell = candidate
    
    return best_cell

# Best-practice player cell padding strategy with custom-built width measurement:
# 1.) Take the name-string from the csv file.
# 1.5.) Normalize the name-string.
# 2.) Fill up with regular blanks a few digits (let's say 3) beyond the intended width of the column.
# 3.) Measure the actual width with custom built function text_display_width().
# 4.) Truncate to (intended_column_length - last_"incomplete"_digit) meaning actual_length_after_truncation is between (intended_column_length - 1) and (intended_column_length).
# 5.) If actual length is not matching intended width exactly, fill up with as many Ideographic Spaces as needed.
# 6.) Return the final padded cell for rendering.
def best_practice_player_cell(name: str, target_width: int) -> str:
    """
    Construct a player cell using overshoot, trim, and micro-fill strategy with prior normalization.
    Ensures display width matches intended column width for Discord and terminal.
    Args:
        name (str): Player name string
        target_width (int): Target display width
    Returns:
        str: Padded and normalized player cell string
    """
    original_name = name  # Keep original for special case detection
    name = normalize_player_name(name) # Step 1.5 Player-Name Normalization
    
    # Use special width function for specific names
    def width_float(s: str) -> float:
        return text_display_width_float_special(s, original_name)
    
    def width_int(s: str) -> int:
        return int(math.ceil(width_float(s)))
    
    if width_int(name) > target_width:
        # Use standard truncation (doesn't need special handling)
        name = truncate_to_width(name, target_width)
    base_w = width_int(name)
    float_w = width_float(name)
    if base_w == target_width and float_w == target_width:
        return name
    # Add overshoot if integer width is less OR if there's a fractional gap
    if base_w < target_width or float_w < target_width:
        name_overshoot = name + ' ' * (target_width - base_w + 3)  # Best-practice overshoot
    else:
        name_overshoot = name
    acc: List[str] = []
    for ch in name_overshoot:
        candidate = ''.join(acc + [ch])
        if width_int(candidate) > target_width:
            break
        acc.append(ch)
        if width_int(''.join(acc)) == target_width:
            break
    cell = ''.join(acc)

    # Optimal gap-filling algorithm using empirically measured space widths:
    # - Regular space: 1.0 width
    # - Braille Pattern Blank (\u2800): 1.113 width (adds +0.113 when replacing 1.0 space)
    # - Ideographic Space (\u3000): 1.665 width (adds +0.665 when replacing 1.0 space)
    gap = target_width - width_float(cell)
    
    # Skip gap filling for special Arabic name (already adjusted)
    if not ("صكار" in original_name and "الشويلات" in original_name) and gap > 0.05:
        cell = _find_optimal_space_combination(cell, gap, target_width, width_float)
    logging.debug(f"Processing player name: {name}, target_width: {target_width}")
    logging.debug(f"Final cell for '{name}': '{cell}', gap={gap}, width={width_float(cell)}")
    return cell

def _highlight_row(row_text: str, player_id: str, highlight_player_ids: Optional[Set[str]], style: str) -> str:
    """Wrap a rendered row in ANSI bold/color codes if its player is highlighted.

    Only applies for style="discord" — "terminal" output (console/log display) never
    gets ANSI codes injected, even if a caller passes highlight_player_ids for it.
    """
    if style == "discord" and highlight_player_ids and player_id in highlight_player_ids:
        return f"{_ANSI_HIGHLIGHT}{row_text}{_ANSI_RESET}"
    return row_text

def render_leaderboard(
    clan_tag: str,
    clan_name: str,
    month_label: str,
    war_info_line: str,
    stats_by_player: Dict[str, Dict[str, Any]],
    mode: str,
    *,
    style: str = 'discord',
    highlight_player_ids: Optional[Set[str]] = None,
    ) -> str:
    """
    Render a complete leaderboard with dynamic column sizing and Unicode compatibility.
    Uses best-practice cell padding and normalization for player names.
    Args:
        clan_tag (str): Clan tag for leaderboard context
        clan_name (str): Clan name for display
        month_label (str): Time period label
        war_info_line (str): War info/status line
        stats_by_player (Dict[str, Dict[str, Any]]): Player stats dict from calculate_leaderboard/_merge_entries
        mode (str): Leaderboard mode identifier (e.g., "attack", "avgstars", "defense")
        style (str): Output style - "discord" (code blocks) or "terminal" (plain text)
        highlight_player_ids: PlayerIDs to bold/color (via ANSI codes) in "discord" style;
            ignored for "terminal" style. Requires the message be posted in a ```ansi
            code block for the codes to render instead of showing as raw text.
    Returns:
        str: Formatted leaderboard text ready for display
    """
    mode = (mode or DEFAULT_MODE).lower()
    spec = MODE_REGISTRY.get(mode, MODE_REGISTRY[DEFAULT_MODE])  # type: ignore[misc]
    cols = spec["columns"]  # type: ignore[misc]
    sort_key = spec["sort_key"]  # type: ignore[misc]
    header_label_map = {
        "attack": "Attack Leaderboard",
        "avgstars": "Average Stars per Attack Leaderboard",
        "avgstarsbyth": "Average Stars per Attack by Townhall Level",
        "attackdefratio": "Attack/Defense-Ratio Leaderboard",
        "missedattacks": "Missed Attacks Leaderboard",
        "defense": "Defense (Fewest Defensive Stars per War) Leaderboard",
        "currentwar": "Current War"
    }
    header_title = header_label_map.get(mode, mode.title())
    if not clan_name:
        clan_name = clan_tag
    lines = [f"⭐ {clan_name} - {header_title}{month_label}:", ""]
    if war_info_line:
        lines.append(war_info_line)
    lines.append("")
    base_player_width = cols[0][1]  # type: ignore[misc]
    header_cells: List[str] = []
    for idx, (col, w) in enumerate(cols):  # type: ignore[misc]
        cur = text_display_width(col)  # type: ignore[arg-type]
        if idx == 0:
            if cur < w:
                col = col + " " * (w - cur)  # type: ignore[misc]
        else:
            if cur < w:
                col = " " * (w - cur) + col  # type: ignore[misc]
        header_cells.append(col)  # type: ignore[misc]
    lines.append(" ".join(header_cells))
    sep = " ".join(['-' * w for _, w in cols])  # type: ignore[misc]
    lines.append(sep)
    
    # Check if this mode requires grouping by TH level
    grouped_by_th = spec.get("grouped_by_th", False)  # type: ignore[misc]
    
    if grouped_by_th:
        # Group players by TH level
        from collections import defaultdict
        players_by_th: Dict[int, List[Any]] = defaultdict(list)  # type: ignore[misc]
        for p in stats_by_player.values():
            th_level = p.get("TH_lvl", 0)  # type: ignore[misc]
            players_by_th[th_level].append(p)  # type: ignore[misc]
        
        # Sort TH levels in descending order (TH18 first)
        sorted_th_levels = sorted(players_by_th.keys(), reverse=True)  # type: ignore[misc]
        
        first_th_group = True
        for th_level in sorted_th_levels:  # type: ignore[misc]
            # Add blank line before each TH group (except the first one)
            if not first_th_group:
                lines.append("")
            first_th_group = False
            
            # Add TH level header
            lines.append(f"TH{th_level}:")
            
            # Sort players within this TH group by the mode's sort_key
            sorted_th_players = sorted(players_by_th[th_level], key=sort_key)  # type: ignore[misc, arg-type]
            
            for p in sorted_th_players:  # type: ignore[misc]
                player_cell = best_practice_player_cell(p.get("Player", ""), base_player_width)  # type: ignore[misc, arg-type]
                if style == "terminal" and wcswidth(player_cell) > base_player_width:
                    player_cell = player_cell[:-1]
                    player_cell += " "
                
                # Calculate total attacks for display (attacks made + missed attacks)
                total_attacks = p.get("Attacks", 0) + p.get("Missed_Attacks", 0)  # type: ignore[misc]
                avg = p.get("Stars", 0) / total_attacks if total_attacks > 0 else 0.0  # type: ignore[misc]
                row_parts = [
                    player_cell,
                    right_pad_number(p.get("TH_lvl", 0), cols[1][1]),  # type: ignore[misc]
                    right_pad_number(p.get("Stars", 0), cols[2][1]),  # type: ignore[misc]
                    right_pad_number(total_attacks, cols[3][1]),  # type: ignore[misc]
                    right_pad_number(f"{avg:.2f}", cols[4][1]),  # type: ignore[misc]
                    right_pad_number(p.get("Defensive_Stars", 0), cols[5][1])  # type: ignore[misc]
                ]
                if len(cols) > 6:  # type: ignore[misc]
                    avg_dest = p.get("Avg_Dest_Pct", 0.0)  # type: ignore[misc]
                    row_parts.append(right_pad_number(f"{avg_dest:.1f}%", cols[6][1]))  # type: ignore[misc]
                lines.append(_highlight_row(" ".join(row_parts), p.get("PlayerID", ""), highlight_player_ids, style))  # type: ignore[misc]
    else:
        # Original non-grouped rendering
        sorted_players = sorted(stats_by_player.values(), key=sort_key)  # type: ignore[arg-type]
        for p in sorted_players:
            player_cell = best_practice_player_cell(p.get("Player", ""), base_player_width)  # type: ignore[misc, arg-type]
            if style == "terminal" and wcswidth(player_cell) > base_player_width:
                player_cell = player_cell[:-1]
                player_cell += " "
            # Calculate total attacks for display (attacks made + missed attacks)
            total_attacks = p.get("Attacks", 0) + p.get("Missed_Attacks", 0)  # type: ignore[misc]
            # Build row_parts based on mode
            if mode == "missedattacks":
                row_parts = [
                    player_cell,
                    right_pad_number(p.get("TH_lvl", 0), cols[1][1]),  # type: ignore[misc]
                    right_pad_number(p.get("Missed_Attacks", 0), cols[2][1]),  # type: ignore[misc]
                    right_pad_number(total_attacks, cols[3][1]),  # type: ignore[misc]
                    right_pad_number(p.get("Stars", 0), cols[4][1]),  # type: ignore[misc]
                    right_pad_number(p.get("Defensive_Stars", 0), cols[5][1])  # type: ignore[misc]
                ]
            elif mode == "attackdefratio":
                defensive = p.get("Defensive_Stars", 0)  # type: ignore[misc]
                ratio = f"{(p.get('Stars', 0) / defensive):.2f}" if defensive else "-"  # type: ignore[misc]
                row_parts = [
                    player_cell,
                    right_pad_number(p.get("TH_lvl", 0), cols[1][1]),  # type: ignore[misc]
                    right_pad_number(p.get("Stars", 0), cols[2][1]),  # type: ignore[misc]
                    right_pad_number(total_attacks, cols[3][1]),  # type: ignore[misc]
                    right_pad_number(f"{(p.get('Stars', 0) / total_attacks):.2f}" if total_attacks > 0 else "0.00", cols[4][1]),  # type: ignore[misc]
                    right_pad_number(defensive, cols[5][1]),  # type: ignore[misc]
                    right_pad_number(ratio, cols[6][1])  # type: ignore[misc]
                ]
            elif mode == "defense":
                row_parts = [
                    player_cell,
                    right_pad_number(p.get("TH_lvl", 0), cols[1][1]),  # type: ignore[misc]
                    right_pad_number(p.get("Stars", 0), cols[2][1]),  # type: ignore[misc]
                    right_pad_number(total_attacks, cols[3][1]),  # type: ignore[misc]
                    right_pad_number(p.get("Defensive_Stars", 0), cols[4][1]),  # type: ignore[misc]
                    right_pad_number(p.get("Defs_Count", 0), cols[5][1]),  # type: ignore[misc]
                    right_pad_number(f"{p.get('Stars_per_Def', 0.0):.2f}", cols[6][1])  # type: ignore[misc]
                ]
            else:  # attack, avgstars, currentwar
                avg = p.get("Stars", 0) / total_attacks if total_attacks > 0 else 0.0
                row_parts = [
                    player_cell,
                    right_pad_number(p.get("TH_lvl", 0), cols[1][1]),
                    right_pad_number(p.get("Stars", 0), cols[2][1]),
                    right_pad_number(total_attacks, cols[3][1]),
                    right_pad_number(f"{avg:.2f}", cols[4][1]),
                    right_pad_number(p.get("Defensive_Stars", 0), cols[5][1])
                ]
                if len(cols) > 6:
                    avg_dest = p.get("Avg_Dest_Pct", 0.0)
                    row_parts.append(right_pad_number(f"{avg_dest:.1f}%", cols[6][1]))
            lines.append(_highlight_row(" ".join(row_parts), p.get("PlayerID", ""), highlight_player_ids, style))
    body = "\n".join(lines)
    return body
