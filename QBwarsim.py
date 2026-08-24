"""
War simulation and prediction module for QapBot with Monte Carlo analysis and cache-centric integration.

This module provides Monte Carlo simulation capabilities for predicting Clash of Clans war outcomes based on remaining attacks, town hall
level matchups, and historical attack success probabilities. All simulation logic is integrated with cache-centric business rules and
defensive error handling.

Key Features:
- Dynamic attack assignment with optimal targeting strategy
- Town hall level-based attack success probability matrices
- Monte Carlo simulation with configurable iteration counts
- Deterministic analysis for certain outcomes
- Confidence calculations based on remaining attacks
- Defensive programming for simulation errors and edge cases

Simulation Strategy:
- Prioritizes attacking bases with maximum star potential
- Prevents double-attacks on same base by same player
- Uses realistic probability distributions based on TH level differences
- Accounts for both attack success and star gain optimization

Business Rules:
- All simulation logic is called from cache-centric business logic modules
- Defensive error handling for simulation failures and API data issues

Integration:
- Called by QBhelperfunctions during war status updates and leaderboard generation
- Results displayed in Discord war progress messages
- Supports both regular wars and CWL format differences
"""
import os
import random
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, wait as _cf_wait, ALL_COMPLETED
from typing import Tuple, List, Dict, Any, Optional

# ---------------------------------------------------------------------------
# CWL league star distribution data (source: clashspot.net, 2026-07)
#
# This dict's key set mirrors the current CoC league ladder — the actual single source of truth
# for that ladder is qapbot/constants.py's CWL_LEAGUE_ORDER (used to derive
# qapbot/ui_cwl_roster.py's CWL_LEAGUE_RANKS picker list, and duplicated as a TS constant in
# activity/client/src/playerPrefs.ts). If a future CoC league-system change adds/renames a tier,
# update CWL_LEAGUE_ORDER (and this dict) together — tracker #0047 was exactly this kind of drift,
# caught only in the picker list, not here.
#
# Rows ordered Legend → Bronze III.
# Columns: [p_0★+missed, p_1★, p_2★, p_3★]
# "missed" (player skipped attack) is merged into the 0★ bucket because
# both outcomes contribute 0 stars to the total.
# Derived from the 5-column breakdown: 3★%, 2★%, 1★%, 0★%, missed%.
#
# Usage: blend these league-level priors with the TH-diff probabilities to
# produce per-attack star distributions that are calibrated to the actual
# performance level of each CWL tier.
#
# Legend League had 0 participants in 2026-07 (still rolling out); it uses
# Titan League I as proxy (the highest tier with real data this month).
# Titan League I/II/III now have real 2026-07 data (11 / 37 / 82 groups).
# All other leagues use 2026-07 clashspot.net data.
# Source columns: [3★, 2★, 1★, 0★_with_dmg, 0★_missed]; the last two are
# combined into p_0★ to produce [p_0★, p_1★, p_2★, p_3★] stored here.
# ---------------------------------------------------------------------------
CWL_LEAGUE_STAR_DISTRIBUTION: Dict[str, List[float]] = {
    # Legend: 0 participants in 2026-07 — proxy from Titan League I
    "Legend League":       [0.0016, 0.0306, 0.3239, 0.6439],
    # Titan leagues (2026-07 data: 11 / 37 / 82 groups)
    "Titan League I":      [0.0016, 0.0306, 0.3239, 0.6439],
    "Titan League II":     [0.0035, 0.0275, 0.2530, 0.7160],
    "Titan League III":    [0.0043, 0.0407, 0.3145, 0.6405],
    # Champion leagues (2026-07 data)
    "Champion League I":   [0.0063, 0.0171, 0.1070, 0.8695],
    "Champion League II":  [0.0093, 0.0202, 0.1236, 0.8469],
    "Champion League III": [0.0142, 0.0299, 0.1562, 0.7998],
    # Master leagues (2026-07 data)
    "Master League I":     [0.0275, 0.0432, 0.2062, 0.7230],
    "Master League II":    [0.0419, 0.0515, 0.2562, 0.6504],
    "Master League III":   [0.0642, 0.0583, 0.2960, 0.5816],
    # Crystal leagues (2026-07 data)
    "Crystal League I":    [0.1045, 0.0650, 0.3082, 0.5223],
    "Crystal League II":   [0.1627, 0.0702, 0.2820, 0.4850],
    "Crystal League III":  [0.2382, 0.0722, 0.2335, 0.4561],
    # Gold leagues (2026-07 data)
    "Gold League I":       [0.3218, 0.0713, 0.1793, 0.4277],
    "Gold League II":      [0.3995, 0.0676, 0.1322, 0.4007],
    "Gold League III":     [0.4656, 0.0624, 0.0981, 0.3738],
    # Silver leagues (2026-07 data)
    "Silver League I":     [0.5294, 0.0557, 0.0717, 0.3433],
    "Silver League II":    [0.5906, 0.0482, 0.0531, 0.3080],
    "Silver League III":   [0.6855, 0.0336, 0.0324, 0.2485],
    # Bronze leagues (2026-07 data)
    "Bronze League I":     [0.6576, 0.0485, 0.0461, 0.2478],
    "Bronze League II":    [0.8146, 0.0158, 0.0136, 0.1560],
    "Bronze League III":   [0.8660, 0.0089, 0.0074, 0.1177],
}


# ---------------------------------------------------------------------------
# Star → destruction % estimate for tiebreaker simulation.
#
# CoC total destruction = average best-destruction across all enemy bases.
# Exact per-attack destruction is not simulated; instead we use a fixed
# mapping from the best-star count on each base to an estimated destruction
# percentage.  The values are medians from observed attack data:
#   0★ = 0%   (unattacked bases; failed attacks slightly underestimated)
#   1★ ≈ 45%  (TH sniped or partial clear)
#   2★ ≈ 75%  (most of the base cleared)
#   3★ = 100% (full destruction)
# Applied identically to both sides, so any systematic bias cancels out.
# ---------------------------------------------------------------------------
_DEST_FOR_STARS: Tuple[float, ...] = (0.0, 45.0, 75.0, 100.0)


def get_league_star_probs(cwl_league_name: str) -> Optional[List[float]]:
    """
    Look up CWL league star distribution by league name.

    Args:
        cwl_league_name: CoC API league name, e.g. "Crystal League I"

    Returns:
        [p_0★, p_1★, p_2★, p_3★] for the given league, or None if not found.
    """
    return CWL_LEAGUE_STAR_DISTRIBUTION.get(cwl_league_name)


# ---------------------------------------------------------------------------
# TH-diff probability table
#
# This table encodes the ABSOLUTE outcome distribution for an attack when
# attacker_th − defender_th == diff and no league context is available.
# When league data IS available, this table also serves as the source of
# "TH-diff deltas": the per-bucket shift relative to the equal-TH row
# (diff == 0), applied on top of the clashspot.net league baseline.
#
# Columns: [p_0★, p_1★, p_2★, p_3★]
# ---------------------------------------------------------------------------
def _th_star_probs_by_diff(diff: int) -> List[float]:
    """
    Return [p_0★, p_1★, p_2★, p_3★] based purely on the TH level difference.

    Dual purpose:
    - Standalone fallback model when no league data is available.
    - Source of per-bucket deltas relative to diff=0, applied as a modifier
      to the clashspot.net league baseline inside ``th_star_probabilities``.
    """
    if diff >= 3:
        return [0.00, 0.00, 0.05, 0.95]
    elif diff == 2:
        return [0.00, 0.00, 0.15, 0.85]
    elif diff == 1:
        return [0.00, 0.05, 0.20, 0.75]
    elif diff == 0:
        return [0.05, 0.10, 0.25, 0.60]
    elif diff == -1:
        return [0.05, 0.15, 0.50, 0.30]
    elif diff == -2:
        return [0.05, 0.25, 0.60, 0.10]
    else:  # diff <= -3
        return [0.29, 0.45, 0.25, 0.01]

def assign_attacks_to_bases_with_stars(war_data: Dict[str, Any], attacker_clan_tag: str) -> List[Tuple[str, int, str, int, int]]:
    """
    Assign remaining attacks to enemy bases with optimal targeting strategy.
    Accepts war_data as a dict (from cache or API, see cache_manager.py and war_data.json example), not a coc.ClanWar object.
    
    Implements a greedy algorithm that maximizes potential star gain by prioritizing
    bases with the most stars remaining to be earned. Each base can only be 3-starred
    once, and each attacker can only attack each base once.
    
    Args:
        war_data: War data dict containing keys 'clan', 'opponent', each with 'tag', 'members' (list of dicts), etc.
        attacker_clan_tag: Tag of the attacking clan (e.g., "#ABC123")
        
    Returns:
        List of attack assignments as tuples:
        (attacker_tag, attacker_th, defender_tag, defender_th, max_stars_to_gain)
        
    Algorithm:
        1. Calculate current stars on each enemy base from existing attacks
        2. Identify remaining attacks per clan member
        3. For each remaining attack, assign to base with maximum star potential
        4. Update base status to prevent over-attacking same target
        
    Optimization Strategy:
        - Prioritizes bases closest to being 3-starred (maximum efficiency)
        - Ensures no attacker wastes attacks on already-defeated bases
        - Accounts for attack limitations per war member
        
    Example:
        assignments = assign_attacks_to_bases_with_stars(war_data, "#L2J0C0PY")
        # Returns: [("#PLAYER1", 14, "#ENEMY1", 13, 2), ("#PLAYER2", 13, "#ENEMY2", 12, 3)]
    """
    # Identify attacking and defending clan
    clan = war_data.get('clan', {})
    opponent = war_data.get('opponent', {})
    if clan.get('tag', '') == attacker_clan_tag:
        attacking_clan = clan
        defending_clan = opponent
    else:
        attacking_clan = opponent
        defending_clan = clan

    # List all enemy bases and their current stars
    defending_clan_members = defending_clan.get('members', [])
    base_stars = {m.get('tag', ''): 0 for m in defending_clan_members}
    base_th_map = {m.get('tag', ''): m.get('townhall', 13) for m in defending_clan_members}
    # Calculate stars already earned on each base
    for member in attacking_clan.get('members', []) + defending_clan.get('members', []):
        for attack in member.get('attacks', []):
            base_tag = attack.get('defenderTag', '')
            stars_earned = attack.get('stars', 0)
            if base_tag in base_stars:
                base_stars[base_tag] = max(base_stars[base_tag], stars_earned)

    # List all attackers and which bases they've already attacked
    attacker_info = []
    for member in attacking_clan.get('members', []):
        attacks_done: set[str] = set()  # type: ignore[misc]
        for attack in member.get('attacks', []):
            base_tag = attack.get('defenderTag', '')
            if base_tag:
                attacks_done.add(base_tag)  # type: ignore[misc]
        max_attacks = war_data.get('attacks_per_member', 2)
        attacks_left_for_attacker = max_attacks - len(attacks_done)  # type: ignore[arg-type]
        attacker_info.append({  # type: ignore[misc]
            "tag": member.get('tag', ''),
            "th": member.get('townhall', 13),
            "attacks_left": attacks_left_for_attacker,
            "already_attacked": attacks_done.copy()
        })

    # Assign attacks: for each attacker, for each attack left, assign to a base not already attacked by this attacker and not already 3-starred
    assignments = []
    base_stars_left = base_stars.copy()
    for attacker in attacker_info:  # type: ignore[misc]
        for _ in range(attacker["attacks_left"]):  # type: ignore[arg-type]
            # Find a base not already attacked by this attacker and not already 3-starred
            possible_bases = [b for b in base_stars_left if b not in attacker["already_attacked"] and base_stars_left[b] < 3]
            if not possible_bases:
                break
            # Pick the base with the most stars left to gain
            base_tag = max(possible_bases, key=lambda x: 3 - base_stars_left[x])
            stars_to_gain = 3 - base_stars_left[base_tag]
            if stars_to_gain > 0:
                assignments.append((attacker["tag"], attacker["th"], base_tag, base_th_map[base_tag], stars_to_gain))  # type: ignore[misc]
                base_stars_left[base_tag] = 3  # After this assignment, base is considered 3-starred
                attacker["already_attacked"].add(base_tag)  # type: ignore[attr-defined]
    return assignments  # type: ignore[return-value]

def calculate_max_possible_stars(war_data: Dict[str, Any], attacker_clan_tag: str) -> int:
    """
    Calculate the maximum possible stars a clan can gain with remaining attacks.
    Accepts war_data as a dict (from cache or API), not a coc.ClanWar object.
    
    Uses the optimal attack assignment algorithm to determine the theoretical maximum
    number of stars that can be gained if all remaining attacks are perfectly executed.
    
    Args:
        war_data: War data dict containing clan/opponent/members info
        attacker_clan_tag: Tag of the attacking clan (e.g., "#ABC123")
        
    Returns:
        Maximum additional stars the clan can gain with perfect attack execution
        
    Algorithm:
        1. Uses assign_attacks_to_bases_with_stars() to get optimal assignments
        2. Sums the maximum star potential from all assigned attacks
        3. Assumes perfect execution (all attacks achieve maximum possible stars)
        
    Use Cases:
        - Deterministic outcome prediction (when max possible < current enemy score)
        - Confidence calculation for uncertain scenarios
        - Strategic planning for remaining attacks
        
    Example:
        max_stars = calculate_max_possible_stars(war_data, "#L2J0C0PY", 5)
        # Returns: 12 (if 5 remaining attacks could theoretically gain 12 stars)
    """
    assignments = assign_attacks_to_bases_with_stars(war_data, attacker_clan_tag)
    return sum(stars_to_gain for _, _, _, _, stars_to_gain in assignments)

def th_star_probabilities(attacker_th: int, defender_th: int, cwl_league: Optional[str] = None) -> List[float]:
    """
    Calculate attack success probabilities based on TH level difference, using
    the clashspot.net league distribution as the primary base model when available.

    Model (with league data):
        1. Start from the clashspot.net league star distribution as the base.
           This represents the actual observed attack outcomes for that CWL tier.
        2. Compute a TH-diff delta: the per-bucket shift of the TH-diff table
           relative to the equal-TH row (diff == 0).  At diff == 0 the delta is
           zero, so the league base is used as-is.
        3. Add the delta to the league base, clamp each bucket to ≥ 0, renormalize.

    Model (without league data):
        Falls back to the pure TH-diff table directly.  The prediction pipeline in
        ``QBhelperfunctions`` always supplies a non-None league via
        ``_ensure_clan_war_league`` (ultimate last-resort default: ``"Master League I"``),
        so the no-league path is exercised only by direct/test callers.

    Rationale:
        The league distribution captures the overall skill level of a tier.
        The TH-diff delta adjusts for the relative matchup advantage/disadvantage.
        Using the league as the base (rather than a 50/50 blend) correctly weights
        empirical data, while the delta preserves the directional effect of TH gaps.

    Args:
        attacker_th: Attacking player's town hall level (1–16).
        defender_th: Defending player's town hall level (1–16).
        cwl_league:  Optional CoC API CWL league name, e.g. "Crystal League I".
                     When None (default), falls back to the pure TH-diff model.

    Returns:
        List of probabilities: [p_0★, p_1★, p_2★, p_3★], summing to 1.0.

    TH-diff table (standalone / source of deltas):
        diff ≥ +3 → [0.00, 0.00, 0.05, 0.95]
        diff = +2 → [0.00, 0.00, 0.15, 0.85]
        diff = +1 → [0.00, 0.05, 0.20, 0.75]
        diff =  0 → [0.05, 0.10, 0.25, 0.60]   ← delta reference (all zeros)
        diff = −1 → [0.05, 0.15, 0.50, 0.30]
        diff = −2 → [0.05, 0.25, 0.60, 0.10]
        diff ≤ −3 → [0.29, 0.45, 0.25, 0.01]

    Examples:
        >>> th_star_probabilities(14, 14)
        # Pure TH model (no league): [0.05, 0.10, 0.25, 0.60]

        >>> th_star_probabilities(14, 13, cwl_league="Crystal League I")
        # Crystal I base [0.1077, 0.0743, 0.3543, 0.4638]
        # diff=+1 delta   [-0.05,  -0.05,  -0.05, +0.15]
        # adjusted (≥0)   [0.0577,  0.0243, 0.3043, 0.6138] → normalise to 1.0
        # → approx        [0.058,   0.024,  0.305,  0.613]

        >>> th_star_probabilities(13, 14, cwl_league="Crystal League I")
        # diff=−1 delta   [0.00,  +0.05, +0.25, −0.30]
        # adjusted        [0.1077, 0.1243, 0.6043, 0.1638] → normalise
        # → approx        [0.108,  0.125,  0.607,  0.164]
    """
    diff = attacker_th - defender_th
    th_probs = _th_star_probs_by_diff(diff)

    if cwl_league is not None:
        league_probs = get_league_star_probs(cwl_league)
        if league_probs is not None:
            # Compute per-bucket delta relative to equal-TH row (diff == 0)
            th_at_equal = _th_star_probs_by_diff(0)  # [0.05, 0.10, 0.25, 0.60]
            th_delta = [th_probs[i] - th_at_equal[i] for i in range(4)]
            # Apply delta to league base, clamp to ≥ 0, normalise
            adjusted = [max(0.0, league_probs[i] + th_delta[i]) for i in range(4)]
            total = sum(adjusted)
            if total > 0:
                return [p / total for p in adjusted]

    return th_probs


def compute_player_skill_factors_from_attacks(
    attack_records: List[Dict[str, Any]],
    cwl_league: str = "Master League I",
) -> Dict[str, float]:
    """
    Compute per-player skill factors from individual CWL attack records using a
    Bayesian 3★-rate estimator.

    For each player, a posterior 3★ probability is computed by combining an
    attack-count-dependent prior with their observed 3★ count::

        baseline_p3  = weighted avg of th_star_probabilities(atk_th, def_th, league)[3]
        posterior_p3 = (k * baseline_p3 + count_3stars) / (k + total_attacks)
        skill_factor = posterior_p3 / baseline_p3

    The prior k shrinks as evidence grows:
        n=1 attack  → k=1.0  (phantom = 50% of evidence; avoids 1/1 = 100%)
        n=2 attacks → k=0.5  (phantom = 20% of evidence; moderate damping)
        n≥3 attacks → k=0.0  (pure MLE; observed rate speaks for itself)

    This means that for ≥3 attacks, skill_factor = observed_3star_rate / baseline_p3,
    so Chunkzilla (3/4 three-stars against TH18, baseline 26.5%) gets
    skill_factor = 0.75 / 0.265 ≈ 2.83 → MC p3 ≈ 75%.

    Args:
        attack_records: List of dicts, each with:
                        'player_tag' (str), 'attacker_th' (int),
                        'defender_th' (int), 'stars' (int).
        cwl_league:     CWL league name for the probability baseline.

    Returns:
        Dict[player_tag, skill_factor] clamped to [0.2, 3.0].
        Players with no attacks are omitted; callers fall back to standard model.
    """
    # Attack-count-dependent prior: large prior when evidence is thin, zero when ≥3 attacks.
    #   n=1 → k=1.0  (phantom attack = 50% of evidence; avoids 1/1 = 100%)
    #   n=2 → k=0.5  (phantom = 20% of evidence; moderate damping)
    #   n≥3 → k=0.0  (pure MLE; observed rate speaks for itself)
    def _prior_k(n: int) -> float:
        if n <= 1:
            return 1.0
        if n == 2:
            return 0.5
        return 0.0

    # Per-player accumulators
    sum_baseline_p3: Dict[str, float] = {}
    count_3stars: Dict[str, int] = {}
    count_attacks: Dict[str, int] = {}

    for rec in attack_records:
        ptag = rec.get('player_tag', '')
        if not ptag:
            continue
        atk_th = int(rec.get('attacker_th', 0) or 0) or 13
        def_th = int(rec.get('defender_th', 0) or 0) or 13
        stars = int(rec.get('stars', 0) or 0)
        probs = th_star_probabilities(atk_th, def_th, cwl_league)
        sum_baseline_p3[ptag] = sum_baseline_p3.get(ptag, 0.0) + probs[3]
        count_attacks[ptag] = count_attacks.get(ptag, 0) + 1
        if stars == 3:
            count_3stars[ptag] = count_3stars.get(ptag, 0) + 1

    result: Dict[str, float] = {}
    for ptag, n in count_attacks.items():
        if n == 0:
            continue
        baseline_p3_avg = sum_baseline_p3[ptag] / n
        if baseline_p3_avg <= 0:
            continue
        n3 = count_3stars.get(ptag, 0)
        k = _prior_k(n)
        if k == 0.0:
            # Pure MLE: no prior, skill factor is directly the observed rate ratio.
            posterior_p3 = n3 / n
        else:
            posterior_p3 = (k * baseline_p3_avg + n3) / (k + n)
        skill_factor = posterior_p3 / baseline_p3_avg
        result[ptag] = max(0.2, min(3.0, skill_factor))
    return result


def th_star_probs_with_skill(
    attacker_th: int,
    defender_th: int,
    cwl_league: Optional[str],
    skill_factor: float,
) -> List[float]:
    """
    Return star probability distribution adjusted by a per-player skill factor.

    Scales p_3star by ``skill_factor`` relative to the standard TH model and
    redistributes the probability delta proportionally among p_0star to p_2star.

    Args:
        attacker_th:  Attacker TH level.
        defender_th:  Defender TH level.
        cwl_league:   Passed through to th_star_probabilities.
        skill_factor: Ratio of actual to expected avg-stars performance.

    Returns:
        Adjusted [p_0star, p_1star, p_2star, p_3star] summing to 1.0.
    """
    base = th_star_probabilities(attacker_th, defender_th, cwl_league)
    p0, p1, p2, p3 = base
    new_p3 = min(0.97, max(0.01, p3 * skill_factor))
    delta_p3 = new_p3 - p3
    others = p0 + p1 + p2
    if others > 1e-9:
        new_p0 = max(0.0, p0 - delta_p3 * p0 / others)
        new_p1 = max(0.0, p1 - delta_p3 * p1 / others)
        new_p2 = max(0.0, p2 - delta_p3 * p2 / others)
    else:
        new_p0, new_p1, new_p2 = 0.0, 0.0, max(0.0, 1.0 - new_p3)
    total = new_p0 + new_p1 + new_p2 + new_p3
    if total > 0:
        return [x / total for x in [new_p0, new_p1, new_p2, new_p3]]
    return base


# ---------------------------------------------------------------------------
# Optimised Monte Carlo inner-loop helpers
# ---------------------------------------------------------------------------

def _precompute_sim_state(
    war_data: Dict[str, Any],
    attacker_clan_tag: str,
    cwl_league: Optional[str],
    player_skill_factors: Optional[Dict[str, float]],
) -> Dict[str, Any]:
    """Pre-compute all static simulation inputs from war_data.

    Called ONCE per chunk (not per simulation).  The returned dict contains
    only primitive Python objects (lists of ints / lists of tuples) so that
    the hot inner loop in ``_run_sim_prebuilt`` operates with no per-iteration
    allocations beyond two fast C-level list copies.

    Returns a dict with keys:
        n_bases           int
        initial_base_stars list[int]  current best-star count per base index
        initial_masks      list[int]  bitmask per attacker; bit j set means
                                      attacker i already attacked base j
        attack_order       list[int]  flat sequence of attacker indices,
                                      one entry per remaining attack
        prob_table         list[list[tuple[float,float,float]]]
                           prob_table[atk_i][base_j] = (cum0, cum01, cum012)
                           where P(3★) = 1 - cum012 (implicit 4th value)
    """
    clan = war_data.get('clan', {})
    opponent = war_data.get('opponent', {})
    if clan.get('tag', '') == attacker_clan_tag:
        attacking_clan = clan
        defending_clan = opponent
    else:
        attacking_clan = opponent
        defending_clan = clan

    attacking_members = attacking_clan.get('members', [])
    defending_members = defending_clan.get('members', [])
    attacks_per_member = int(war_data.get('attacks_per_member', 2))

    # ── Defending bases ──────────────────────────────────────────────────
    n_bases = len(defending_members)
    base_tags = [m.get('tag', '') for m in defending_members]
    base_tag_to_idx: Dict[str, int] = {tag: i for i, tag in enumerate(base_tags)}
    base_th = [int(m.get('townhall', 13) or 13) for m in defending_members]

    # Current best-star count on each base from all attacks already logged
    initial_base_stars = [0] * n_bases
    for member in attacking_members + defending_members:
        for attack in member.get('attacks', []):
            btag = attack.get('defenderTag', '')
            if btag in base_tag_to_idx:
                idx = base_tag_to_idx[btag]
                s = int(attack.get('stars', 0) or 0)
                if s > initial_base_stars[idx]:
                    initial_base_stars[idx] = s

    # ── Attacking members ────────────────────────────────────────────────
    attacker_th_list: List[int] = []
    attacks_left_list: List[int] = []
    initial_masks: List[int] = []   # bitmask: bit j = attacker already attacked base j
    attacker_tags: List[str] = []
    for member in attacking_members:
        atk_th = int(member.get('townhall', 13) or 13)
        attacks_done = 0
        mask = 0
        for attack in member.get('attacks', []):
            btag = attack.get('defenderTag', '')
            if btag in base_tag_to_idx:
                mask |= 1 << base_tag_to_idx[btag]
            attacks_done += 1
        atk_left = max(0, attacks_per_member - attacks_done)
        attacker_th_list.append(atk_th)
        attacks_left_list.append(atk_left)
        initial_masks.append(mask)
        attacker_tags.append(member.get('tag', ''))

    # ── Attack order: one entry per remaining attack ─────────────────────
    attack_order: List[int] = []
    for i, atk_left in enumerate(attacks_left_list):
        attack_order.extend([i] * atk_left)

    # ── Pre-compute cumulative probability table ─────────────────────────
    # prob_table[atk_i][base_j] = (cum0, cum01, cum012)
    # P(0★)=cum0, P(≤1★)=cum01, P(≤2★)=cum012, P(3★)=1-cum012 (implicit).
    # Stored as 3-tuples to avoid repeated list construction in the hot loop.
    prob_table: List[List[Tuple[float, float, float]]] = []
    for atk_i in range(len(attacker_th_list)):
        atk_th = attacker_th_list[atk_i]
        sf = player_skill_factors.get(attacker_tags[atk_i]) if player_skill_factors else None
        row: List[Tuple[float, float, float]] = []
        for base_j in range(n_bases):
            def_th = base_th[base_j]
            if sf is not None:
                p = th_star_probs_with_skill(atk_th, def_th, cwl_league, sf)
            else:
                p = th_star_probabilities(atk_th, def_th, cwl_league=cwl_league)
            row.append((p[0], p[0] + p[1], p[0] + p[1] + p[2]))
        prob_table.append(row)

    return {
        'n_bases': n_bases,
        'initial_base_stars': initial_base_stars,
        'initial_masks': initial_masks,
        'attack_order': attack_order,
        'prob_table': prob_table,
    }


def _run_sim_prebuilt(prebuilt: Dict[str, Any]) -> tuple[int, float]:
    """Run a single Monte Carlo simulation from pre-computed state.

    Hot inner loop — called n_chunk times per worker with no per-call
    allocations beyond two fast C-level list copies (initial_base_stars
    and initial_masks).

    Args:
        prebuilt: dict returned by _precompute_sim_state.

    Returns:
        Tuple of (stars_gained, destruction_pct):
        - stars_gained: total additional stars gained by the attacking clan.
        - destruction_pct: estimated total destruction percentage (0-100)
          computed from the final per-base star state via _DEST_FOR_STARS.
    """
    base_stars: List[int] = list(prebuilt['initial_base_stars'])   # C-level copy
    masks: List[int] = list(prebuilt['initial_masks'])              # C-level copy
    prob_table: List[Any] = prebuilt['prob_table']
    n_bases: int = int(prebuilt['n_bases'])
    total = 0
    _rnd = random.random  # local name avoids repeated global dict lookup

    for atk_idx in prebuilt['attack_order']:
        mask: int = masks[atk_idx]  # type: ignore[index]
        # Greedy: pick the base with the most stars still to gain that this
        # attacker has not yet attacked (same strategy as simulate_attacks_dynamic).
        best_j = -1
        best_gain = 0
        for j in range(n_bases):
            if mask >> j & 1:
                continue        # already attacked this base
            s = base_stars[j]
            if s >= 3:
                continue        # already 3-starred
            gain = 3 - s
            if gain > best_gain:
                best_gain = gain
                best_j = j

        if best_j < 0:
            continue  # no available target

        # Draw star result using pre-computed cumulative thresholds.
        # Avoids random.choices list allocation by comparing a single float.
        r = _rnd()
        cum0, cum01, cum012 = prob_table[atk_idx][best_j]
        if r < cum0:
            stars_result = 0
        elif r < cum01:
            stars_result = 1
        elif r < cum012:
            stars_result = 2
        else:
            stars_result = 3

        gained = stars_result - base_stars[best_j]
        if gained > 0:
            base_stars[best_j] = stars_result
            total += gained
        masks[atk_idx] = mask | (1 << best_j)

    # Estimate total destruction from the final per-base star state.
    # Each base contributes equally (1/n_bases) to overall destruction.
    _d = _DEST_FOR_STARS
    dest = 0.0
    for j in range(n_bases):
        dest += _d[base_stars[j]]
    dest /= n_bases

    return total, dest


def simulate_attacks_dynamic(war_data: Dict[str, Any], attacker_clan_tag: str, sim_number: int = 0, cwl_league: Optional[str] = None, player_skill_factors: Optional[Dict[str, float]] = None) -> int:
    """
    Simulate remaining attacks for a clan using dynamic base selection.
    Accepts war_data as a dict (from cache or API, see cache_manager.py and war_data.json example), not a coc.ClanWar object.
    
    For each remaining attack, selects the optimal target base that maximizes
    potential star gain, then simulates the attack outcome based on TH probabilities.
    This function is the core of the Monte Carlo simulation system.
    
    Args:
        war_data: War data dict containing keys 'clan', 'opponent', each with 'tag', 'members' (list of dicts), etc.
        attacker_clan_tag: Tag of the attacking clan (e.g., "#L2J0C0PY")
        sim_number: Simulation number for debug logging (logs details for sim 1)
        cwl_league: Optional CoC API CWL league name (e.g. "Crystal League I"). When provided,
                    attack probabilities are blended with empirical league-tier star distributions.
        player_skill_factors: Optional per-player skill factors (from compute_player_skill_factors_from_attacks).
                              When set, adjusts 3-star probability for each clan member whose tag
                              is in the dict.  Members not present use the standard model.
        
    Returns:
        Total stars gained from all simulated attacks in this simulation run
        
    Algorithm:
        1. Identify remaining attacks for each clan member
        2. For each attack, dynamically select best available target
        3. Simulate attack outcome using TH-based probabilities
        4. Update base status and attacker constraints
        5. Continue until all attacks are assigned
        
    Dynamic Selection:
        - Each attack chooses the current best target (not predetermined)
        - Accounts for results of previous simulated attacks
        - Prevents wasteful attacks on already-3-starred bases
        
    Debug Logging:
        - Logs detailed attack outcomes for simulation #1
        - Includes attacker/defender info, probabilities, and results
        - Useful for validating simulation logic
        
    Example:
        stars = simulate_attacks_dynamic(war_data, "#L2J0C0PY", sim_number=1)
        # Returns: 8 (total stars gained in this simulation)
    """
    # Identify attacking and defending clan
    clan = war_data.get('clan', {})
    opponent = war_data.get('opponent', {})
    if clan.get('tag', '') == attacker_clan_tag:
        attacking_clan = clan
        defending_clan = opponent
    else:
        attacking_clan = opponent
        defending_clan = clan

    # Build tag_to_name mapping for all members
    tag_to_name = {m.get('tag', ''): m.get('name', '') for m in attacking_clan.get('members', []) + defending_clan.get('members', [])}

    # List all enemy bases and their current stars
    defending_clan_members = defending_clan.get('members', [])
    base_stars = {m.get('tag', ''): 0 for m in defending_clan_members}
    base_th_map = {m.get('tag', ''): m.get('townhall', 13) for m in defending_clan_members}
    for member in attacking_clan.get('members', []) + defending_clan.get('members', []):
        for attack in member.get('attacks', []):
            base_tag = attack.get('defenderTag', '')
            stars_earned = attack.get('stars', 0)
            if base_tag in base_stars:
                base_stars[base_tag] = max(base_stars[base_tag], stars_earned)

    attacker_info = []
    for member in attacking_clan.get('members', []):
        attacks_done: set[str] = set()  # type: ignore[misc]
        for attack in member.get('attacks', []):
            base_tag = attack.get('defenderTag', '')
            if base_tag:
                attacks_done.add(base_tag)  # type: ignore[misc]
        attacks_left = war_data.get('attacks_per_member', 2) - len(attacks_done)  # type: ignore[arg-type]
        attacker_info.append({  # type: ignore[misc]
            "tag": member.get('tag', ''),
            "th": member.get('townhall', 13),
            "attacks_left": attacks_left,
            "already_attacked": set(attacks_done)  # type: ignore[arg-type]
        })

    stars = 0
    attack_queue: list[int] = []  # type: ignore[misc]
    for idx, attacker in enumerate(attacker_info):  # type: ignore[arg-type, misc]
        for _ in range(attacker["attacks_left"]):  # type: ignore[arg-type]
            attack_queue.append(idx)  # type: ignore[misc]
    # For each attack, pick the best base at that moment
    for attacker_idx in attack_queue:  # type: ignore[misc]
        attacker = attacker_info[attacker_idx]  # type: ignore[misc]
        possible_bases = [b for b in base_stars if b not in attacker["already_attacked"] and base_stars[b] < 3]
        if not possible_bases:
            continue
        base_tag = max(possible_bases, key=lambda x: 3 - base_stars[x])
        _stars_to_gain = 3 - base_stars[base_tag]  # Used implicitly by max key calculation
        _sf = player_skill_factors.get(attacker["tag"]) if player_skill_factors else None  # type: ignore[union-attr]
        if _sf is not None:
            probs = th_star_probs_with_skill(attacker["th"], base_th_map[base_tag], cwl_league, _sf)  # type: ignore[arg-type]
        else:
            probs = th_star_probabilities(attacker["th"], base_th_map[base_tag], cwl_league=cwl_league)  # type: ignore[arg-type]
        # Draw stars for this attack
        stars_result = random.choices([0, 1, 2, 3], weights=probs, k=1)[0]
        # Only count stars that can still be gained on this base
        stars_gained = stars_result - base_stars[base_tag]
        if stars_gained < 0:
            stars_gained = 0
        base_stars[base_tag] += stars_gained
        attacker["already_attacked"].add(base_tag)  # type: ignore[attr-defined]
        stars += stars_gained
        # Optional: log details for debugging
        if sim_number == 1:
            attacker_name = tag_to_name.get(attacker["tag"], attacker["tag"])  # type: ignore[arg-type]
            defender_name = tag_to_name.get(base_tag, base_tag)
            logging.debug(
                f"Sim1 | Attacker: {attacker_name:<16} (TH{attacker['th']}) | "
                f"Defender: {defender_name:<16} (TH{base_th_map[base_tag]}) | "
                f"Base stars before: {base_stars[base_tag] - stars_gained} | "
                f"Attack result: {stars_result} | "
                f"Stars gained: {stars_gained} | "
                f"probs: {probs}"
            )
    return stars


# ---------------------------------------------------------------------------
# Parallel Monte Carlo helpers
# ---------------------------------------------------------------------------

# Resolved at startup by init_sim_pool(); default uses all cores (capped at 8).
# Lowercase names prevent Pylance from inferring these as Final constants,
# which would cause "cannot be redefined" errors in init_sim_pool().
_sim_enabled: bool = True
_sim_n_workers: int = min(os.cpu_count() or 2, 8)
# Minimum sims-per-worker to justify process-pool IPC overhead.
# Below this threshold the simulation runs in-process (also keeps monkeypatching
# in tests working, since worker processes don't inherit patched globals).
# Set to 2000 so the default n_sim=1000 always runs in-process: process pool
# spawn overhead on server-machine hardware (~500 ms–1 s per pool creation) exceeds computation
# time for 1000 optimised sims and would dominate wall time across 19 wars.
# Process pool is only worthwhile for explicit large-batch requests (≥8000 sims
# with 4 workers: 8000 >= 4×2000).
_sim_min_chunk: int = 2000


def _worker_initializer() -> None:
    """Run inside each worker process at startup.

    Ignores SIGINT so that Ctrl-C in the main process does not propagate to
    workers and cause noisy InterruptedError tracebacks.  The main process
    handles the signal and calls shutdown_sim_pool() during cleanup.
    """
    import signal as _signal
    _signal.signal(_signal.SIGINT, _signal.SIG_IGN)


def shutdown_sim_pool() -> None:
    """No-op kept for API compatibility.

    The pool is now ephemeral (created and destroyed inside each call to
    monte_carlo_war_prediction), so there is nothing persistent to shut down.
    Callers in async_cleanup() and the MAINTENANCE_START handler are harmless.
    """
    pass


def get_cpu_info() -> Dict[str, Any]:
    """Return a dict with CPU brand, logical core count, and sim worker count.

    platform.processor() is empty on Linux (including Synology DSM), so we
    fall back to /proc/cpuinfo 'model name' when needed.
    """
    import platform as _platform
    logical_cores = os.cpu_count() or 1
    cpu_brand = _platform.processor().strip()
    if not cpu_brand:
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as _f:
                for _line in _f:
                    if _line.startswith("model name"):
                        cpu_brand = _line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
    if not cpu_brand:
        cpu_brand = _platform.machine() or "unknown"
    return {
        "cpu": cpu_brand,
        "logical_cores": logical_cores,
        "sim_enabled": _sim_enabled,
        "sim_workers": _sim_n_workers if _sim_enabled else 0,
    }


def init_sim_pool(enabled: bool, max_workers: int) -> None:
    """Set Monte Carlo configuration globals from bot config.

    No persistent pool is created.  A fresh ProcessPoolExecutor is spun up
    (and immediately shut down via the context-manager) inside each call to
    monte_carlo_war_prediction(), so worker processes only exist for the
    duration of the computation and never idle in the background.

    Args:
        enabled:     If False, all simulations run in-process (single-threaded).
        max_workers: Maximum worker processes.  0 = use all logical CPUs
                     (capped at 8).  Positive value = use exactly that many
                     (still capped at logical core count).
    """
    global _sim_enabled, _sim_n_workers
    import logging as _log

    _sim_enabled = enabled
    logical_cores = os.cpu_count() or 1
    if not enabled:
        _sim_n_workers = 1
    elif max_workers <= 0:
        _sim_n_workers = min(logical_cores, 8)
    else:
        _sim_n_workers = min(max_workers, logical_cores)

    if _sim_enabled and _sim_n_workers > 1:
        _log.info(
            f"[SIM] Monte Carlo configured: ephemeral pool, "
            f"{_sim_n_workers} workers / {logical_cores} logical cores"
        )
    else:
        _log.info(
            f"[SIM] Monte Carlo running in-process "
            f"({'disabled by config' if not enabled else 'single worker'})"
        )


def _monte_carlo_chunk(
    args: Tuple[Dict[str, Any], str, str, int, int, int, int, Optional[str], Optional[str], Optional[Dict[str, float]]],
) -> Tuple[int, int, int, int, int]:
    """Worker function for one chunk of Monte Carlo simulations.

    Must be a module-level function so ProcessPoolExecutor can pickle it.
    Each worker independently simulates *n_chunk* iterations and returns
    aggregated (win, lose, draw, total_win_stars, total_lose_stars).

    Pre-computes simulation state ONCE per chunk via _precompute_sim_state,
    then calls the lean _run_sim_prebuilt for each iteration.  This eliminates
    O(n_members) dict/set allocation work that would otherwise happen on every
    individual simulation call.
    """
    (
        war_data, my_tag, enemy_tag, my_stars, enemy_stars,
        n_chunk, _offset, cwl_league_my, cwl_league_opp, player_skill_factors,
    ) = args

    # Pre-compute static war state ONE TIME per chunk, not per simulation.
    # Moves all dict-building, attack counting, and probability table
    # computation out of the hot inner loop.
    my_prebuilt = _precompute_sim_state(war_data, my_tag, cwl_league_my, player_skill_factors)
    opp_prebuilt = _precompute_sim_state(war_data, enemy_tag, cwl_league_opp, player_skill_factors)

    win = lose = draw = total_win_stars = total_lose_stars = 0
    for _ in range(n_chunk):
        my_stars_gained, my_dest_sim = _run_sim_prebuilt(my_prebuilt)
        opp_stars_gained, opp_dest_sim = _run_sim_prebuilt(opp_prebuilt)
        my_sim = my_stars + my_stars_gained
        enemy_sim = enemy_stars + opp_stars_gained
        if my_sim > enemy_sim:
            win += 1
            total_win_stars += my_sim - enemy_sim
        elif my_sim < enemy_sim:
            lose += 1
            total_lose_stars += enemy_sim - my_sim
        else:
            # Equal stars: tiebreak by simulated destruction.
            # Destruction is estimated from the final per-base star state,
            # so it dynamically reflects the simulated attacks — not the
            # static mid-war snapshot (which would bias toward the clan
            # that has used more attacks at prediction time).
            if my_dest_sim > opp_dest_sim:
                win += 1
            elif opp_dest_sim > my_dest_sim:
                lose += 1
            else:
                draw += 1
    return win, lose, draw, total_win_stars, total_lose_stars


def monte_carlo_war_prediction(war_data: Dict[str, Any], clan_tag: str, n_sim: int = 1000, cwl_league_my: Optional[str] = None, cwl_league_opp: Optional[str] = None, player_skill_factors: Optional[Dict[str, float]] = None) -> Tuple[int, int, int, int]:
    """
    Run Monte Carlo simulation to predict war outcome probabilities.
    Accepts war_data as a dict (from cache or API, see cache_manager.py and war_data.json example), not a coc.ClanWar object.
    
    Simulates remaining attacks for both clans multiple times to calculate
    win, lose, and draw probabilities along with confidence level.
    
    Args:
        war_data: War data dict containing keys 'clan', 'opponent', each with 'tag', 'members' (list of dicts), etc.
        clan_tag: Tag of the clan to predict for
        n_sim: Number of simulations to run (default: 1000)
        cwl_league_my:  Optional CoC API CWL league name for the tracked clan (e.g. "Champion League I").
        cwl_league_opp: Optional CoC API CWL league name for the opponent clan. Defaults to
                        cwl_league_my when not specified.
        
    Returns:
        Tuple of (win_probability%, lose_probability%, draw_probability%, confidence%)
        
    Algorithm:
        1. For each simulation iteration:
           - Simulate remaining attacks for both clans
           - Compare final star totals to determine outcome
        2. Calculate probability percentages from simulation results
        3. Determine confidence level based on remaining attacks
        
    Confidence Calculation:
        Based on the ratio of attacks remaining to total attacks.
        More attacks remaining = lower confidence in prediction.
        
    Probability Clamping:
        - Win probability clamped to 1-99% to avoid overconfidence
        - Ensures realistic probability ranges for display
        
    Debug Logging:
        - Logs first 10 simulation results for validation
        - Includes detailed statistics for analysis
        
    Example:
        win, lose, draw, conf = monte_carlo_war_prediction(war_data, "#L2J0C0PY")
        # Returns: (75, 20, 5, 85) - 75% win, 20% lose, 5% draw, 85% confidence
    """
    # Identify attacking and defending clan
    my_clan = war_data.get('clan', {})
    enemy_clan = war_data.get('opponent', {})
    my_stars = my_clan.get('stars', 0)
    enemy_stars = enemy_clan.get('stars', 0)

    attacks_per_member = war_data.get('attacks_per_member', 2)
    members_per_clan = len(my_clan.get('members', []))
    my_tag = my_clan.get('tag', '')
    enemy_tag = enemy_clan.get('tag', '')

    # Distribute n_sim across worker processes when the count is large enough
    # to justify IPC overhead. Small counts (tests / near-decided wars) run
    # in-process so monkeypatching in tests keeps working.
    _use_pool = _sim_enabled and _sim_n_workers > 1 and n_sim >= _sim_n_workers * _sim_min_chunk
    actual_workers = _sim_n_workers if _use_pool else 1
    base_chunk = n_sim // actual_workers
    remainder = n_sim % actual_workers
    chunks = [base_chunk + (1 if i < remainder else 0) for i in range(actual_workers)]
    offsets = [sum(chunks[:i]) for i in range(actual_workers)]
    # Use opponent's league for enemy simulation; fall back to my league when not provided.
    _cwl_opp = cwl_league_opp if cwl_league_opp is not None else cwl_league_my
    args_list = [
        (war_data, my_tag, enemy_tag, my_stars, enemy_stars,
         c, o, cwl_league_my, _cwl_opp, player_skill_factors)
        for c, o in zip(chunks, offsets)
    ]

    if _use_pool:
        # Ephemeral pool with 'spawn' start method.
        #
        # WHY spawn, not fork (Linux default):
        #   ProcessPoolExecutor runs inside asyncio.to_thread() — meaning the
        #   parent already has multiple threads active (event loop, HTTP
        #   sessions, DB pool).  fork() copies the entire parent address space
        #   including every mutex that happens to be locked at that instant
        #   (logging module internal lock is the most common culprit).  The
        #   child inherits the locked state but NOT the thread that holds the
        #   lock, so the worker deadlocks the first time it tries to write a
        #   log line.  pool.map() then waits forever, the asyncio thread never
        #   returns, cycle_idle_event is never set, and only kill -9 can break
        #   the chain.
        #   spawn starts a fresh Python interpreter with no inherited locks.
        #   It is ~50-100 ms slower to fork but completely deadlock-free.
        #
        # Timeout safety net: if a worker still hangs (shouldn't happen with
        #   spawn, but guards against unexpected future issues), we fall back
        #   to sequential rather than blocking forever.
        _spawn_ctx = multiprocessing.get_context('spawn')
        try:
            with ProcessPoolExecutor(
                max_workers=_sim_n_workers,
                initializer=_worker_initializer,
                mp_context=_spawn_ctx,
            ) as _pool:
                _futures = [_pool.submit(_monte_carlo_chunk, a) for a in args_list]
                _done, _not_done = _cf_wait(_futures, timeout=30.0, return_when=ALL_COMPLETED)
                if _not_done:
                    for _f in _not_done:
                        _f.cancel()
                    logging.warning(
                        f"Monte Carlo: {len(_not_done)} worker(s) timed out after 30 s, "
                        "falling back to sequential"
                    )
                    raise RuntimeError("worker timeout")
                chunk_results = [_f.result() for _f in _futures]  # preserves chunk order
        except Exception as _pool_err:
            logging.warning(f"Monte Carlo parallel pool failed, falling back to sequential: {_pool_err}")
            chunk_results = [_monte_carlo_chunk(
                (war_data, my_tag, enemy_tag, my_stars, enemy_stars,
                 n_sim, 0, cwl_league_my, _cwl_opp, player_skill_factors)
            )]
    else:
        chunk_results = [_monte_carlo_chunk(a) for a in args_list]

    win = sum(r[0] for r in chunk_results)
    lose = sum(r[1] for r in chunk_results)
    draw = sum(r[2] for r in chunk_results)
    total_win_stars = sum(r[3] for r in chunk_results)
    total_lose_stars = sum(r[4] for r in chunk_results)

    logging.debug(f"Monte Carlo ({actual_workers} workers, {n_sim} sims): wins={win}, loses={lose}, draws={draw}")
    win_prob = win / n_sim
    lose_prob = lose / n_sim
    draw_prob = draw / n_sim
    avg_win_stars = total_win_stars / win if win > 0 else 0
    avg_lose_stars = total_lose_stars / lose if lose > 0 else 0

    # Calculate confidence based on attacks left
    total_attacks = members_per_clan * attacks_per_member * 2
    my_clan_attacks_done = sum(len(m.get('attacks', [])) for m in my_clan.get('members', []))
    enemy_clan_attacks_done = sum(len(m.get('attacks', [])) for m in enemy_clan.get('members', []))
    my_clan_attacks_left = members_per_clan * attacks_per_member - my_clan_attacks_done
    enemy_clan_attacks_left = members_per_clan * attacks_per_member - enemy_clan_attacks_done
    confidence = 1 - (my_clan_attacks_left + enemy_clan_attacks_left) / total_attacks
    confidence = max(0.0, min(confidence, 1.0))
    confidence_rounded = round(confidence * 100)

    # Probability clamping: ensure rounded values sum to exactly 100% and none of the values is 0 or 100
    win_prob_clamped = max(1, round(win_prob * 100))
    lose_prob_clamped = max(1, round(lose_prob * 100))
    draw_prob_clamped = max(1, round(draw_prob * 100))
    if win_prob_clamped == 100: win_prob_clamped = 99
    if lose_prob_clamped == 100: lose_prob_clamped = 99
    if draw_prob_clamped == 100: draw_prob_clamped = 99
    if win_prob_clamped + lose_prob_clamped + draw_prob_clamped != 100:
        # Adjust probabilities so the highest is set to 100 minus the sum of the other two
        probs = [win_prob_clamped, lose_prob_clamped, draw_prob_clamped]
        max_idx = probs.index(max(probs))
        other_sum = sum(probs) - probs[max_idx]
        probs[max_idx] = 100 - other_sum
        win_prob_clamped, lose_prob_clamped, draw_prob_clamped = probs
    if win_prob_clamped == 0 or lose_prob_clamped == 0 or draw_prob_clamped == 0 or (win_prob_clamped + lose_prob_clamped + draw_prob_clamped != 100):
        logging.error(f"Monte Carlo: Clan wins={win}, loses={lose}, draws={draw} out of {n_sim}")
        logging.error(f"attacks_per_member={attacks_per_member}, total_attacks_per_clan={total_attacks/2}, my_clan_attacks_left={my_clan_attacks_left}, enemy_clan_attacks_left={enemy_clan_attacks_left}")
        logging.error(f"win_prob={win_prob:.3f}, lose_prob={lose_prob:.3f}, draw_prob={draw_prob:.3f}, avg_win_stars={avg_win_stars:.1f}, avg_lose_stars={avg_lose_stars:.1f}, confidence={confidence_rounded:.3f}")
        logging.error(f"win_prob_clamped={win_prob_clamped:.3f}, lose_prob_clamped={lose_prob_clamped:.3f}, draw_prob_clamped={draw_prob_clamped:.3f}, avg_win_stars={avg_win_stars:.1f}, avg_lose_stars={avg_lose_stars:.1f}, confidence={confidence_rounded:.3f}")
    return win_prob_clamped, lose_prob_clamped, draw_prob_clamped, confidence_rounded

def calculate_win_probability(
    war_data: Dict[str, Any], 
    clan_tag: str, 
    attacks_left: int, 
    my_stars: int, 
    enemy_stars: int, 
    enemy_attacks_left: int,
    my_clan_destruction: Optional[float] = None, 
    enemy_clan_destruction: Optional[float] = None,
    cwl_league_my: Optional[str] = None,
    cwl_league_opp: Optional[str] = None,
    player_skill_factors: Optional[Dict[str, float]] = None,
) -> Tuple[int, int, int, int]:
    """
    Calculate war win probability using deterministic analysis and Monte Carlo simulation.
    Accepts war_data as a dict (from cache or API), not a coc.ClanWar object.
    
    First checks for certain outcomes based on maximum possible stars.
    If outcome is uncertain, runs Monte Carlo simulation to estimate probabilities.
    
    Args:
        war_data: War data dict containing clan/opponent/members info
        clan_tag: Tag of the clan to calculate probability for
        attacks_left: Number of attacks remaining for the clan
        my_stars: Current stars for the clan
        enemy_stars: Current stars for the enemy
        enemy_attacks_left: Number of attacks remaining for the enemy
        my_clan_destruction: Optional destruction percentage for tiebreaker (0.0-100.0)
        enemy_clan_destruction: Optional destruction percentage for tiebreaker (0.0-100.0)
        cwl_league_my:  Optional CoC API CWL league name for the tracked clan.
        cwl_league_opp: Optional CoC API CWL league name for the opponent clan. Defaults to
                        cwl_league_my when not specified. Both enable league-calibrated attack
                        probabilities blended with the empirical clashspot.net distributions.
        
    Returns:
        Tuple of (win_probability%, lose_probability%, draw_probability%, confidence%)
        
    Decision Logic:
        1. If all attacks done and stars equal, use destruction percentages
        2. Calculate maximum possible stars for both clans
        3. If one clan cannot catch up (deterministic), return 100% certainty
        4. Otherwise, run Monte Carlo simulation for probability estimation
        
    Deterministic Cases:
        - Enemy's max possible < current clan stars → 100% win
        - Clan's max possible < current enemy stars → 100% loss
        - Equal stars with no attacks left → Use destruction tiebreaker
        
    Tiebreaker Rules:
        When stars are equal and no attacks remain, clan with higher
        destruction percentage wins (standard Clash of Clans rules).
        
    Example:
        win, lose, draw, conf = await calculate_win_probability(
            war_data, "#L2J0C0PY", 5, 45, 40, 3, 85.5, 82.1
        )
        # Returns: (65, 30, 5, 75) - 65% win chance based on simulation
    """
    # Special case: all attacks done and stars equal, use destruction
    if attacks_left == 0 and enemy_attacks_left == 0 and my_stars == enemy_stars:
        if my_clan_destruction is not None and enemy_clan_destruction is not None:
            if my_clan_destruction > enemy_clan_destruction:
                return 100, 0, 0, 100
            elif my_clan_destruction < enemy_clan_destruction:
                return 0, 100, 0, 100
            else:
                return 0, 0, 100, 100
    
    clan = war_data.get('clan', {})
    opponent = war_data.get('opponent', {})
    enemy_tag = opponent.get('tag', '') if clan.get('tag', '') == clan_tag else clan.get('tag', '')
    my_max_possible = my_stars + calculate_max_possible_stars(war_data, clan_tag)
    enemy_max_possible = enemy_stars + calculate_max_possible_stars(war_data, enemy_tag)
    logging.debug(f"{my_stars=}, {enemy_stars=}, {my_max_possible=}, {enemy_max_possible=}")

    # Only predict a certain win/loss if max_possible_stars(team a) > max_possible_stars(team b) or vice versa
    if my_stars > enemy_max_possible:
        return 100, 0, 0, 100  # Victory is certain
    elif enemy_stars > my_max_possible:
        return 0, 100, 0, 100  # Defeat is certain
    elif my_stars == enemy_stars and my_max_possible == my_stars and enemy_max_possible == enemy_stars:
        # Both clans already at maximum stars — no further stars can be gained.
        # All remaining attacks are irrelevant; destruction is 100 % on both sides.
        # Result is a certain draw regardless of how many attacks are left.
        return 0, 0, 100, 100
    else:
        # Use Monte Carlo simulation for undecided cases (including equal max possible)
        win_prob, lose_prob, draw_prob, confidence = monte_carlo_war_prediction(war_data, clan_tag, n_sim=1000, cwl_league_my=cwl_league_my, cwl_league_opp=cwl_league_opp, player_skill_factors=player_skill_factors)
        return win_prob, lose_prob, draw_prob, confidence
