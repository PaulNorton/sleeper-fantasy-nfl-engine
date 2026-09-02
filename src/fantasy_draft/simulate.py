"""Monte Carlo rollout of the rest of the draft.

Opponents are modelled as ADP drafters: each remaining player is given a
sampled draft position drawn from a normal centred on his ADP and truncated to
"has not gone yet", and opponents take players in that order.  At each of your
own picks the simulation picks the player who adds the most lineup utility.

Scoring a candidate means forcing him at your current pick, playing the rest of
the draft out, and averaging your final roster utility across scenarios.  The
difference between two candidates is therefore expected points added to your
starting lineup, net of everything you would have got instead.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import NormalDist

from .outcomes import outcome
from .projections import MISSING_ADP, Player
from .valuation import LineupEvaluator, Roster, add_to_roster, outcome_utility

NORM = NormalDist()

# Spread of a player's real draft slot around his ADP.
ADP_SIGMA_FLOOR = 4.0
ADP_SIGMA_SCALE = 0.26

# A player with no ADP is a deep sleeper: model him as going far in the future.
UNDRAFTED_LAG = 120.0
UNDRAFTED_SIGMA = 60.0


def adp_sigma(adp: float) -> float:
    return max(ADP_SIGMA_FLOOR, ADP_SIGMA_SCALE * adp)


def pick_distribution(adp: float, current_pick: int) -> tuple[float, float]:
    """Where a still-available player is expected to go, and how wide that is.

    A player who is still on the board well past his ADP is not going to last
    forever: the market has simply not taken him yet, so his expected landing
    spot moves up to the current pick.  Without this, one faller poisons every
    downstream estimate by looking permanently available.
    """
    if adp >= MISSING_ADP:
        return current_pick + UNDRAFTED_LAG, UNDRAFTED_SIGMA
    mu = max(adp, float(current_pick))
    return mu, adp_sigma(mu)


def survival(mu: float, sigma: float, pick: float) -> float:
    """P(a player with this landing distribution is still on the board at `pick`)."""
    return max(1e-6, 1.0 - NORM.cdf((pick - 0.5 - mu) / sigma))


def availability(player: Player, target_pick: int, current_pick: int) -> float:
    """P(still there at `target_pick`), conditioned on being there right now."""
    if target_pick <= current_pick:
        return 1.0
    mu, sigma = pick_distribution(player.adp, current_pick)
    return min(1.0, survival(mu, sigma, target_pick) / survival(mu, sigma, current_pick))


@dataclass
class RolloutConfig:
    sims: int = 300
    horizon: int = 7        # how many of your own future picks to simulate
    window: int = 12        # candidates weighed at each simulated pick
    lookahead: int = 60     # how far to scan for the best free player per position
    pool: int = 220         # players considered draftable in the horizon
    seed: int = 12345
    risk: float | None = None  # None scores the average season; 0.25 the floor, 0.75 the upside
    strategy: str = "vona"     # "vona", "lineup" or "simulate"; see recommend.recommend
    objective: str = "points"  # "points" maximises your total; "win" maximises beating the field
    title_scale: float = 40.0  # points of roster edge that meaningfully shifts title odds


def build_scenarios(
    players: list[Player], current_pick: int, cfg: RolloutConfig
) -> list[list[int]]:
    """Sample `cfg.sims` orderings of the remaining players, indices into `players`."""
    rng = random.Random(cfg.seed)
    scenarios: list[list[int]] = []
    # Each player's landing spot, truncated to "has not gone yet".
    params = [pick_distribution(p.adp, current_pick) for p in players]
    lows = [
        min(NORM.cdf((current_pick - 0.5 - mu) / sigma), 0.999) for mu, sigma in params
    ]
    for _ in range(cfg.sims):
        keys: list[tuple[float, int]] = []
        for i, (mu, sigma) in enumerate(params):
            low = lows[i]
            u = low + (1.0 - low) * rng.random()
            keys.append((mu + sigma * NORM.inv_cdf(u), i))
        keys.sort()
        scenarios.append([i for _, i in keys])
    return scenarios


def realisation_matrix(players: list[Player], cfg: RolloutConfig, stream: int = 0) -> list[list[float]]:
    """One realised season per player per scenario.

    Two variance reductions matter here, because simulating seasons on top of
    draft order roughly doubles the noise in a candidate's score:

    - **Common random numbers.** The matrix is built once and reused for every
      candidate, so candidates are compared against identical imagined seasons
      rather than against their own luck.
    - **Antithetic pairs.** Every odd scenario mirrors the previous one's draws.
      If a player got a lucky season in one, he gets the matching unlucky season
      in its partner, so position-level luck cancels across the pair.
    """
    rng = random.Random(cfg.seed ^ (0x5EED + stream))
    rows: list[list[float]] = []
    previous: list[float] = []
    for s in range(cfg.sims):
        if s % 2 == 1 and previous:
            draws = [-z for z in previous]
        else:
            draws = [rng.gauss(0.0, 1.0) for _ in players]
        previous = draws
        rows.append(
            [
                outcome(p.curve, p.pos_rank, p.spread, z, p.value)
                for p, z in zip(players, draws)
            ]
        )
    return rows


def _best_candidates(
    order: list[int], start: int, taken: set[int], players: list[Player], cfg: "RolloutConfig"
) -> list[int]:
    """The next `window` undrafted players in this scenario, plus the best free
    player at every position so a needed position is never invisible."""
    out: list[int] = []
    seen_pos: set[str] = set()
    i = start
    n = len(order)
    while i < n and len(out) < cfg.window:
        idx = order[i]
        if idx not in taken:
            out.append(idx)
            seen_pos.add(players[idx].position)
        i += 1
    limit = min(n, i + cfg.lookahead)
    while i < limit:
        idx = order[i]
        p = players[idx]
        if idx not in taken and p.position not in seen_pos:
            out.append(idx)
            seen_pos.add(p.position)
        i += 1
    return out


def rollout(
    order: list[int],
    players: list[Player],
    evaluator: LineupEvaluator,
    base_entries: list[tuple[str, float, float]],
    base_roster: Roster,
    my_picks: list[int],
    board_pick: int,
    forced: int | None,
    cfg: RolloutConfig,
    realised: list[float],
) -> float:
    """Play out one scenario and score the roster you end with.

    `board_pick` is the pick the draft is on right now, which may be several
    picks before your own turn; opponents in between take players first. The
    forced candidate is reserved, since the question being asked is what he is
    worth to you if you do get him.

    Picks are made on projections, because that is all you know on draft day.
    The finished roster is then scored on realised seasons, which is what prices
    a player's range of outcomes rather than just his mean.
    """
    roster = base_roster
    picked: list[int] = []
    taken: set[int] = set()
    if forced is not None:
        taken.add(forced)
    ptr = 0
    consumed = 0
    n = len(order)

    for j, pick_no in enumerate(my_picks):
        # Let the opponents ahead of this pick take their players.
        need = (pick_no - board_pick) - j
        while consumed < need and ptr < n:
            if order[ptr] not in taken:
                consumed += 1
            ptr += 1
        while ptr < n and order[ptr] in taken:
            ptr += 1

        if j == 0 and forced is not None:
            choice = forced
        else:
            base = evaluator.utility(roster)
            best_gain = None
            choice = None
            for idx in _best_candidates(order, ptr, taken, players, cfg):
                p = players[idx]
                gain = evaluator.marginal(roster, p.position, p.value, base)
                if best_gain is None or gain > best_gain:
                    best_gain = gain
                    choice = idx
            if choice is None:
                break
        p = players[choice]
        roster = add_to_roster(roster, p.position, p.value)
        picked.append(choice)
        taken.add(choice)

    entries = list(base_entries)
    entries += [(players[i].position, players[i].value, realised[i]) for i in picked]
    return outcome_utility(evaluator, entries)


def score_candidates(
    candidates: list[int],
    players: list[Player],
    evaluator: LineupEvaluator,
    base_players: list[Player],
    my_picks: list[int],
    board_pick: int,
    cfg: RolloutConfig,
    owner_by_pick: dict[int, int] | None = None,
    opponent_bases: dict[int, list[Player]] | None = None,
    my_owner: int | None = None,
) -> dict[int, float]:
    """Expected end-of-draft roster value for each candidate at your next pick.

    With `cfg.risk` set, the aggregate is that percentile of your simulated
    seasons instead of the average: 0.25 asks which pick protects your floor,
    0.75 which pick gives you the best shot at a league-winning roster.
    """
    horizon_picks = my_picks[: cfg.horizon]
    pool_idx = sorted(
        range(len(players)), key=lambda i: (players[i].adp, -players[i].value)
    )[: cfg.pool]
    pool = sorted(set(pool_idx) | set(candidates))
    remap = {old: new for new, old in enumerate(pool)}
    sub = [players[i] for i in pool]

    scenarios = build_scenarios(sub, board_pick, cfg)
    pool_seasons = realisation_matrix(sub, cfg, stream=0)
    base_seasons = realisation_matrix(base_players, cfg, stream=1)
    base_roster: Roster = {}
    for p in base_players:
        base_roster = add_to_roster(base_roster, p.position, p.value)

    beating_field = (
        cfg.objective == "win"
        and owner_by_pick is not None
        and opponent_bases is not None
        and my_owner is not None
    )
    opponent_seasons = {}
    if beating_field:
        for owner, roster in opponent_bases.items():
            opponent_seasons[owner] = realisation_matrix(roster, cfg, stream=2 + owner)

    results: dict[int, list[float]] = {c: [] for c in candidates}
    field: list[list[float]] = []
    for s, order in enumerate(scenarios):
        base_entries = [
            (p.position, p.value, base_seasons[s][k]) for k, p in enumerate(base_players)
        ]
        if beating_field:
            opp_entries = {
                owner: [
                    (p.position, p.value, opponent_seasons[owner][s][k])
                    for k, p in enumerate(roster)
                ]
                for owner, roster in opponent_bases.items()
            }
            field.append(
                field_utilities(
                    order, sub, evaluator, owner_by_pick, opp_entries, my_owner,
                    horizon_picks, board_pick, cfg, pool_seasons[s],
                )
            )
        for c in candidates:
            results[c].append(
                rollout(
                    order, sub, evaluator, base_entries, base_roster, horizon_picks,
                    board_pick, remap[c], cfg, pool_seasons[s],
                )
            )

    out: dict[int, float] = {}
    for c, values in results.items():
        if beating_field and field:
            out[c] = _title_share(values, field, cfg.title_scale)
            continue
        ordered = sorted(values)
        if cfg.risk is None:
            out[c] = sum(ordered) / max(1, len(ordered))
        else:
            i = min(len(ordered) - 1, max(0, int(round(cfg.risk * (len(ordered) - 1)))))
            out[c] = ordered[i]
    return out


def field_utilities(
    order: list[int],
    players: list[Player],
    evaluator: LineupEvaluator,
    owner_by_pick: dict[int, int],
    base_entries: dict[int, list[tuple[str, float, float]]],
    my_owner: int,
    my_picks: list[int],
    board_pick: int,
    cfg: RolloutConfig,
    realised: list[float],
) -> list[float]:
    """Realised roster values for everyone except you, in one scenario.

    Variance is worth nothing when you are maximising your own points: a roster
    averages fifteen players, so one player's range barely moves the total. It
    is worth a great deal when you have to *beat nine other people*. Finishing
    with the most points is a tail event, and a steady roster that projects
    third gets you third. So the field has to be simulated too.

    This runs once per scenario rather than once per candidate: which player you
    take barely changes what nine opponents end up with, and pretending it does
    not lets the whole objective come almost free.
    """
    rosters: dict[int, list[tuple[str, float, float]]] = {
        owner: list(entries) for owner, entries in base_entries.items() if owner != my_owner
    }
    if not rosters:
        return []
    taken: set[int] = set()
    ptr = 0
    n = len(order)
    last = my_picks[-1] if my_picks else board_pick

    for pick_no in range(board_pick, last + 1):
        while ptr < n and order[ptr] in taken:
            ptr += 1
        if ptr >= n:
            break
        owner = owner_by_pick.get(pick_no)
        idx = order[ptr]
        ptr += 1
        taken.add(idx)
        if owner is None or owner == my_owner or owner not in rosters:
            continue
        p = players[idx]
        rosters[owner].append((p.position, p.value, realised[idx]))

    return [outcome_utility(evaluator, entries) for entries in rosters.values()]


def _title_share(mine: list[float], field: list[list[float]], scale: float) -> float:
    """Your share of the title odds, averaged over scenarios.

    A hard "did you finish top two" indicator saturates: one pick out of fifteen
    rarely flips a discrete finish, so every candidate scores the same. This is
    the smooth version - each roster's weight falls off exponentially behind the
    scenario's best, and your share is your weight over the league's.

    It is deliberately convex in your own total. That is the entire point: a
    roster that finishes third is worth little more than one that finishes
    fifth, so upside that occasionally wins outright is worth more than the
    same points delivered reliably. This is the only place variance pays.
    """
    total = 0.0
    for s, value in enumerate(mine):
        rivals = field[s] if s < len(field) else []
        if not rivals:
            continue
        best = max(max(rivals), value)
        weights = [math.exp((r - best) / scale) for r in rivals]
        own = math.exp((value - best) / scale)
        total += own / (own + sum(weights))
    return total / max(1, len(mine))
