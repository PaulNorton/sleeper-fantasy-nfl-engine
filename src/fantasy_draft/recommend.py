"""Turn draft state into a ranked list of picks.

The ranking metric is expected end-of-draft roster utility (see valuation.py),
estimated by simulating the rest of the draft (see simulate.py).  Everything
else the report shows - value over replacement, tiers, availability - is there
to explain that number, not to produce it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .draft import DraftState, open_starter_slots
from .league import LeagueSettings
from .projections import MISSING_ADP, Player, waiver_levels
from .simulate import RolloutConfig, availability, score_candidates
from .valuation import LineupEvaluator, Roster, roster_from_players


@dataclass
class Recommendation:
    player: Player
    score: float          # expected final roster utility if you take him
    edge: float           # points ahead of the next best choice
    vor: float            # projection minus replacement level at his position
    lineup_gain: float    # utility he adds to the roster you hold right now
    wait_cost: float      # value lost at his position if you wait a turn
    avail_next: float     # P(still on the board at your following pick)
    tier: int
    tier_left: int
    floor: float          # his 10th percentile season
    ceiling: float        # his 90th percentile season
    reason: str


# "Robust RB": prioritise running backs in the first few rounds beyond what
# their raw marginal value implies. Conventional fantasy wisdom, tested rather
# than assumed - see tests/hypotheses.py. Confirmed over 280 paired backtested
# drafts (2024 and 2025): +3.1 +/- 1.1 points per draft, t = 2.79.
ROBUST_RB_ROUNDS = 4
ROBUST_RB_BONUS = 0.15


def assign_tiers(players: list[Player]) -> tuple[dict[str, int], dict[tuple[str, int], int]]:
    """Gap-based tiers inside each position; a tier break is a real cliff."""
    tiers: dict[str, int] = {}
    sizes: dict[tuple[str, int], int] = {}
    by_pos: dict[str, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p)
    for pos, group in by_pos.items():
        group.sort(key=lambda p: -p.value)
        tier = 1
        for i, p in enumerate(group):
            if i:
                gap = group[i - 1].value - p.value
                if gap >= max(8.0, 0.045 * max(group[i - 1].value, 1.0)):
                    tier += 1
            tiers[p.player_id] = tier
            sizes[(pos, tier)] = sizes.get((pos, tier), 0) + 1
    return tiers, sizes


def expected_best_value(
    group: list[Player],
    target_pick: int,
    current_pick: int,
    skip: str | None = None,
) -> float:
    """Expected value of the best player at a position surviving to `target_pick`.

    `skip` leaves one player out, which is what you need to ask "if I pass on
    him, what else does this position still offer me next turn".
    """
    if not group:
        return 0.0
    ordered = [p for p in sorted(group, key=lambda p: -p.value) if p.player_id != skip][:40]
    if not ordered:
        return 0.0
    survive = 1.0
    expected = 0.0
    for p in ordered:
        prob = availability(p, target_pick, current_pick)
        expected += survive * prob * p.value
        survive *= 1.0 - prob
        if survive < 1e-4:
            break
    return expected + survive * ordered[-1].value


def _shortlist(
    available: list[Player],
    evaluator: LineupEvaluator,
    roster: Roster,
    league: LeagueSettings,
    width: int,
) -> list[int]:
    """Cheap prescreen so the simulator only rates plausible picks."""
    base = evaluator.utility(roster)
    scored = [
        (i, evaluator.marginal(roster, p.position, p.value, base, p.bye or 0), p.vorp)
        for i, p in enumerate(available)
    ]
    by_lineup = [i for i, _, _ in sorted(scored, key=lambda t: -t[1])[:width]]
    by_vor = [i for i, _, _ in sorted(scored, key=lambda t: -t[2])[:width]]

    picked = dict.fromkeys(by_lineup + by_vor)
    # Guarantee the best free body at every position, so a need is never invisible.
    for pos in {p.position for p in available}:
        best = max(
            (i for i, p in enumerate(available) if p.position == pos),
            key=lambda i: available[i].value,
            default=None,
        )
        if best is not None:
            picked.setdefault(best, None)
    return list(picked)


def recommend(
    league: LeagueSettings,
    state: DraftState,
    all_players: list[Player],
    replacement: dict[str, float],
    my_roster_id: int | None,
    my_picks: list[int],
    limit: int = 12,
    cfg: RolloutConfig | None = None,
) -> list[Recommendation]:
    cfg = cfg or RolloutConfig()
    drafted = state.drafted_ids()
    by_id = {p.player_id: p for p in all_players}
    available = [p for p in all_players if p.player_id not in drafted]

    my_players = (
        [by_id[pid] for pid in state.roster_player_ids(my_roster_id) if pid in by_id]
        if my_roster_id is not None
        else []
    )
    roster = roster_from_players(my_players)
    # The evaluator prices an unfilled slot at what waivers would give you, not
    # at replacement level; see waiver_levels for why those differ.
    evaluator = LineupEvaluator.from_league(
        league, waiver_levels(all_players, league.replacement_ranks(), league.teams)
    )

    current = my_picks[0] if my_picks else state.current_pick
    following = my_picks[1] if len(my_picks) > 1 else current + 2 * state.teams
    horizon = my_picks or [current + i * state.teams for i in range(cfg.horizon)]

    tiers, tier_sizes = assign_tiers(available)
    by_pos: dict[str, list[Player]] = {}
    for p in available:
        by_pos.setdefault(p.position, []).append(p)
    wait_value = {
        pos: expected_best_value(group, following, current) for pos, group in by_pos.items()
    }
    base_util = evaluator.utility(roster)

    # Beating the field can only be judged by simulating the other rosters.
    strategy = "simulate" if cfg.objective == "win" else cfg.strategy
    if strategy != "simulate":
        scores = _direct_scores(
            available, evaluator, roster, base_util, wait_value, strategy,
            by_pos, following, current, league.teams, league.superflex,
        )
        return _build(
            available, scores, evaluator, roster, base_util, wait_value, tiers,
            tier_sizes, league, my_players, current, following, len(horizon), limit,
        )

    owner_by_pick = None
    opponent_bases = None
    if cfg.objective == "win" and my_roster_id is not None and horizon:
        owner_by_pick = {
            n: state.roster_owning_pick(n)
            for n in range(state.current_pick, horizon[-1] + 1)
        }
        opponent_bases = {
            rid: [by_id[pid] for pid in state.roster_player_ids(rid) if pid in by_id]
            for rid in set(state.slot_to_roster.values())
            if rid != my_roster_id
        }

    shortlist = _shortlist(available, evaluator, roster, league, width=10)
    scores = score_candidates(
        shortlist, available, evaluator, my_players, horizon, state.current_pick, cfg,
        owner_by_pick=owner_by_pick, opponent_bases=opponent_bases, my_owner=my_roster_id,
    )

    return _build(
        available, scores, evaluator, roster, base_util, wait_value, tiers,
        tier_sizes, league, my_players, current, following, len(horizon), limit,
    )


def _direct_scores(
    available: list[Player],
    evaluator: LineupEvaluator,
    roster: Roster,
    base_util: float,
    wait_value: dict[str, float],
    strategy: str,
    by_pos: dict[str, list[Player]],
    following: int,
    current: int,
    teams: int,
    superflex: bool = False,
) -> dict[int, float]:
    """Rank without simulating the draft.

    `lineup` scores what a player adds to your starting lineup right now, with a
    running-back bonus in the first few rounds (see ROBUST_RB_BONUS below). The
    bonus is switched off in superflex: it was only backtested in 1QB leagues,
    where quarterbacks are not competing with runners for the same roster spot.
    Applying it blind there stacks an untested assumption on top of a format
    that already prices quarterbacks very differently.

    `vona` subtracts what you could still expect from that position at your next
    pick if you passed on him now. That expectation is per player, not per
    position: it blends his own chance of surviving with the best of whatever
    else would be left. Two receivers with identical projections therefore score
    differently if one goes in round 2 and the other in round 5, which is the
    whole point of the adjustment.

    Both answer the same question the draft rollout does, in closed form, so
    they carry none of its noise. Over 60 backtested drafts the two are
    indistinguishable from each other and both beat simulating the draft out.
    """
    scores: dict[int, float] = {}
    can_wait = following > current
    round_no = (current - 1) // teams + 1 if teams else 1
    rb_bonus = (
        ROBUST_RB_BONUS if round_no <= ROBUST_RB_ROUNDS and not superflex else 0.0
    )
    for i, p in enumerate(available):
        now = evaluator.marginal(roster, p.position, p.value, base_util, p.bye or 0)
        if rb_bonus and p.position == "RB":
            now *= 1.0 + rb_bonus
        if strategy == "vona" and can_wait:
            survives = availability(p, following, current)
            others = expected_best_value(
                by_pos.get(p.position, []), following, current, skip=p.player_id
            )
            # If he is certain to last, waiting costs nothing and this nets to zero.
            alternative = survives * p.value + (1.0 - survives) * others
            now -= evaluator.marginal(roster, p.position, alternative, base_util, p.bye or 0)
        scores[i] = now
    return scores


def _build(
    available: list[Player],
    scores: dict[int, float],
    evaluator: LineupEvaluator,
    roster: Roster,
    base_util: float,
    wait_value: dict[str, float],
    tiers: dict[str, int],
    tier_sizes: dict[tuple[str, int], int],
    league: LeagueSettings,
    my_players: list[Player],
    current: int,
    following: int,
    picks_left: int,
    limit: int,
) -> list[Recommendation]:
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[: max(limit * 3, 30)]
    best_alt = {
        idx: max((v for j, v in ranked if j != idx), default=score)
        for idx, score in ranked
    }

    recs: list[Recommendation] = []
    for idx, score in ranked:
        p = available[idx]
        tier = tiers.get(p.player_id, 1)
        recs.append(
            Recommendation(
                player=p,
                score=score,
                edge=score - best_alt[idx],
                vor=round(p.vorp, 1),
                lineup_gain=round(
                    evaluator.marginal(roster, p.position, p.value, base_util, p.bye or 0), 1
                ),
                wait_cost=round(max(0.0, p.value - wait_value.get(p.position, 0.0)), 1),
                avail_next=availability(p, following, current),
                tier=tier,
                tier_left=tier_sizes.get((p.position, tier), 1),
                floor=round(p.floor, 1),
                ceiling=round(p.ceiling, 1),
                reason="",
            )
        )
    for r in recs:
        r.reason = _reason(r, league, my_players, current, picks_left)
    return recs[:limit]


def _reason(
    rec: Recommendation,
    league: LeagueSettings,
    my_players: list[Player],
    current_pick: int,
    picks_left: int,
) -> str:
    p = rec.player
    bits: list[str] = []

    holes = open_starter_slots(league, my_players)
    fills = [slot for slot in league.slots_for_position(p.position) if holes.get(slot)]
    if fills:
        bits.append(f"fills {fills[0]}")
    elif rec.lineup_gain > 1:
        bits.append("upgrades a starter")
    else:
        bits.append("depth only")

    if rec.tier_left == 1:
        bits.append(f"last of {p.position} tier {rec.tier}")
    elif rec.tier_left <= 3:
        bits.append(f"{rec.tier_left} left in tier")

    if p.adp < MISSING_ADP:
        delta = p.adp - current_pick
        if delta <= -10:
            bits.append(f"fell to you, ADP {p.adp:.0f}")
        elif delta >= 12:
            bits.append(f"{delta:.0f} picks ahead of ADP {p.adp:.0f}")

    # Measured across the player pool, a typical range spans about 1.15x the
    # median season; rookies run near 1.6 and settled veterans near 1.1.
    span = (rec.ceiling - rec.floor) / max(p.median, 1.0)
    if span > 1.45:
        bits.append(f"wide range {rec.floor:.0f}-{rec.ceiling:.0f}")
    elif span < 0.85:
        bits.append("steady")
    if p.years_exp == 0:
        bits.append("rookie")
    if p.bye:
        bits.append(f"bye {p.bye}")

    if rec.avail_next >= 0.6:
        bits.append(f"{rec.avail_next:.0%} to last to your next pick")
    elif rec.wait_cost >= 20:
        bits.append(f"waiting costs ~{rec.wait_cost:.0f} pts here")

    if picks_left <= sum(open_starter_slots(league, my_players).values()):
        bits.append("starters still unfilled")
    return "; ".join(bits)
