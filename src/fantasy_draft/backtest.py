"""Backtest the pick engine against a finished season.

The evaluation problem is circularity. Scoring simulated drafts with the same
outcome model the engine optimises against would let it win by construction, and
would measure nothing. So this replays a real season instead:

  - teams draft knowing only what was knowable before the season - Sleeper's
    preseason projections and ADP for that year
  - rosters are then scored on what actually happened, using the league's own
    scoring applied to real stat lines

Nothing about the risk model, the value curve or the opponent model is used to
decide the answer. The season is the answer.

Two limits are worth stating. FantasyPros consensus is only published for the
current year, so the backtest runs on Sleeper projections alone and does not
test the consensus blend. And the simulated opponents draft to ADP, which is the
same assumption the engine makes about them, so the engine knows its opponents
better here than it would in your league.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .draft import DraftState
from .league import LeagueSettings
from .projections import (
    Player,
    waiver_levels,
    apply_reliability,
    apply_vorp,
    attach_risk,
    load_players,
    replacement_levels,
    score_stats,
)
from .recommend import (
    ROBUST_RB_BONUS,
    ROBUST_RB_ROUNDS,
    assign_tiers,
    expected_best_value,
    recommend,
)
from .simulate import RolloutConfig, adp_sigma, availability
from .sleeper import SleeperClient
from .valuation import HINDSIGHT, LineupEvaluator, add_to_roster

# Set once per backtest so the greedy baseline can build an evaluator.
_pool_cache: dict[str, list[Player]] = {}

# Stops a baseline drafting fifteen running backs.
CAPS = {"QB": 2, "RB": 6, "WR": 6, "TE": 2, "K": 1, "DEF": 1}

STRATEGIES = ("engine", "greedy", "blind", "vona", "adp", "vor", "need")

# Rejected hypotheses live on below, excluded from STRATEGIES so a normal
# backtest run does not include them. tests/hypotheses.py re-runs the full
# comparison, greedy included, and records why each was kept or dropped.
REACH_LAMBDA = 0.3

# H4: does Robust RB generalise, or is RB special? Same magnitude, same first
# four rounds, applied to another position instead.
POSBONUS_ROUNDS = ROBUST_RB_ROUNDS
POSBONUS_AMOUNT = ROBUST_RB_BONUS

# H5: tier-cliff bonus. Reward taking the last player in his tier - the one
# right before a real gap to the next echelon at his position - using the same
# gap-based tiers already shown in the "Tier" column. Distinct from vona: no
# pick-timing or availability estimate involved, just "is a cliff imminent".
CLIFF_BONUS = 0.20



@dataclass
class Team:
    slot: int
    strategy: str
    roster_id: int
    players: list[Player] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.players:
            out[p.position] = out.get(p.position, 0) + 1
        return out


def load_actuals(
    client: SleeperClient, league: LeagueSettings, season: str
) -> dict[str, float]:
    """What each player really scored that season, in this league's scoring."""
    actual: dict[str, float] = {}
    for pos in league.needed_positions:
        for row in client.stats(season, pos):
            stats = {k: float(v) for k, v in (row.get("stats") or {}).items() if v is not None}
            if not stats:
                continue
            points = score_stats(stats, league.scoring_settings)
            if points <= 0:
                points = float(stats.get("pts_ppr", 0.0) or 0.0)
            actual[str(row.get("player_id"))] = points
    return actual


def actual_replacement(
    players: list[Player], actual: dict[str, float], ranks: dict[str, int]
) -> dict[str, float]:
    """What a replacement-level player really scored, for scoring unfilled slots.

    Using the *projected* replacement here quietly rigs the comparison. Kickers
    projected 70 points in 2025 and the replacement-level kicker actually scored
    142, so a team that skipped the position was docked 72 points it would never
    really have lost - it would simply have streamed someone. The yardstick for
    what a roster was worth has to come from the same season it is scoring.
    """
    by_pos: dict[str, list[float]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(actual.get(p.player_id, 0.0))
    levels: dict[str, float] = {}
    for pos, values in by_pos.items():
        values.sort(reverse=True)
        rank = ranks.get(pos, max(1, len(values) // 4))
        levels[pos] = values[min(rank, len(values)) - 1]
    return levels


def market_order(players: list[Player], seed: int) -> list[Player]:
    """One draft's running order, sampled around ADP.

    Fixed per draft so that swapping one seat's strategy leaves every other
    seat's behaviour identical. Without that pairing the noise between drafts
    would swamp the difference between strategies.
    """
    rng = random.Random(seed)
    keyed = []
    for p in players:
        adp = p.adp if p.adp < 999 else 400.0
        keyed.append((adp + adp_sigma(adp) * rng.gauss(0.0, 1.0), p))
    keyed.sort(key=lambda kv: kv[0])
    return [p for _key, p in keyed]


def _allowed(team: Team, player: Player, league: LeagueSettings, rounds_left: int) -> bool:
    counts = team.counts()
    if counts.get(player.position, 0) >= CAPS.get(player.position, 6):
        return False
    # Do not burn an early pick on a kicker or defence.
    if player.position in ("K", "DEF") and rounds_left > 2:
        return False
    return True


def _forced_fill(team: Team, league: LeagueSettings, rounds_left: int) -> str | None:
    """A mandatory slot that must be filled before the draft ends."""
    counts = team.counts()
    for pos in ("K", "DEF"):
        need = league.starter_slots.get(pos, 0) - counts.get(pos, 0)
        if need > 0 and rounds_left <= need:
            return pos
    return None


def baseline_pick(
    team: Team,
    league: LeagueSettings,
    board: list[Player],
    taken: set[str],
    rounds_left: int,
    pick_no: int = 0,
    next_pick: int = 0,
) -> Player | None:
    """A pick from one of the simple strategies the engine is measured against."""
    forced = _forced_fill(team, league, rounds_left)
    pool = [p for p in board if p.player_id not in taken]
    if forced:
        candidates = [p for p in pool if p.position == forced]
        if candidates:
            return max(candidates, key=lambda p: p.vorp)

    legal = [p for p in pool if _allowed(team, p, league, rounds_left)]
    if not legal:
        legal = pool
    if not legal:
        return None

    lineup_family = (
        "greedy", "blind", "vona", "reach_averse", "ceiling_bench",
        "posbonus_wr", "posbonus_te", "posbonus_qb", "tier_cliff",
    )
    if team.strategy in lineup_family:
        # The engine's lineup model with no simulation: take whoever adds most to
        # the starting lineup right now. Isolates what the rollout is worth.
        evaluator = LineupEvaluator.from_league(
            league, waiver_levels(_pool_cache["players"], league.replacement_ranks(), league.teams)
        )
        roster: dict[str, list[float]] = {}
        for p in team.players:
            roster = add_to_roster(
                roster, p.position, p.value, 0 if team.strategy == "blind" else (p.bye or 0)
            )
        base = evaluator.utility(roster)
        round_no = (pick_no - 1) // league.teams + 1 if pick_no else 1
        rb_bonus = ROBUST_RB_BONUS if round_no <= ROBUST_RB_ROUNDS else 0.0

        def rb_adjusted(p: Player, value: float) -> float:
            # Folded into "greedy"/"blind" so the backtest measures the engine
            # as shipped, not the engine minus this feature. Confirmed over 280
            # paired drafts: +3.1 +/- 1.1 points, t = 2.79.
            now = evaluator.marginal(roster, p.position, value, base, p.bye or 0)
            return now * (1.0 + rb_bonus) if p.position == "RB" else now

        if team.strategy == "reach_averse":
            def score_reach(p: Player) -> float:
                now = evaluator.marginal(roster, p.position, p.value, base, p.bye or 0)
                reach = max(0.0, p.adp - pick_no) if pick_no else 0.0
                return now - REACH_LAMBDA * reach

            return max(legal[:60], key=score_reach)

        if team.strategy.startswith("posbonus_"):
            target_pos = team.strategy.rsplit("_", 1)[-1].upper()
            round_no = (pick_no - 1) // league.teams + 1 if pick_no else 1
            bonus = POSBONUS_AMOUNT if round_no <= POSBONUS_ROUNDS else 0.0

            def score_posbonus(p: Player) -> float:
                now = evaluator.marginal(roster, p.position, p.value, base, p.bye or 0)
                return now * (1.0 + bonus) if p.position == target_pos else now

            return max(legal[:60], key=score_posbonus)

        if team.strategy == "tier_cliff":
            tiers, tier_sizes = assign_tiers(pool)

            def score_cliff(p: Player) -> float:
                now = evaluator.marginal(roster, p.position, p.value, base, p.bye or 0)
                tier = tiers.get(p.player_id, 1)
                last_in_tier = tier_sizes.get((p.position, tier), 1) == 1
                return now * (1.0 + CLIFF_BONUS) if last_in_tier else now

            return max(legal[:60], key=score_cliff)

        if team.strategy == "ceiling_bench":
            starters_count = sum(league.starter_slots.values())
            bench_bound = len(team.players) >= starters_count

            def score_ceiling(p: Player) -> float:
                target = p.ceiling if bench_bound else p.value
                return evaluator.marginal(roster, p.position, target, base, p.bye or 0)

            return max(legal[:60], key=score_ceiling)

        if team.strategy in ("greedy", "blind"):
            # "blind" drafts ignoring bye weeks; it is scored with the same bye
            # penalty as everyone else, so the pair measures what byes are worth.
            if team.strategy == "blind":
                return max(
                    legal[:60],
                    key=lambda p: evaluator.marginal(roster, p.position, p.value, base, 0),
                )
            return max(legal[:60], key=lambda p: rb_adjusted(p, p.value))
        # vona: exactly what recommend._direct_scores does, so the backtest
        # measures the strategy that actually ships.
        by_pos: dict[str, list[Player]] = {}
        for p in legal:
            by_pos.setdefault(p.position, []).append(p)
        can_wait = next_pick > pick_no

        def score(p: Player) -> float:
            now = evaluator.marginal(roster, p.position, p.value, base, p.bye or 0)
            if not can_wait:
                return now
            survives = availability(p, next_pick, pick_no)
            others = expected_best_value(
                by_pos.get(p.position, []), next_pick, pick_no, skip=p.player_id
            )
            alternative = survives * p.value + (1.0 - survives) * others
            return now - evaluator.marginal(
                roster, p.position, alternative, base, p.bye or 0
            )

        return max(legal[:60], key=score)
    if team.strategy == "adp":
        return legal[0]  # board is already in market order
    if team.strategy == "vor":
        return max(legal, key=lambda p: p.vorp)
    if team.strategy == "need":
        open_slots = {
            slot: league.starter_slots[slot]
            for slot in league.starter_slots
        }
        for p in team.players:
            for slot in league.slots_for_position(p.position):
                if open_slots.get(slot, 0) > 0:
                    open_slots[slot] -= 1
                    break
        needed = {
            pos
            for pos in {p.position for p in legal}
            if any(open_slots.get(s, 0) > 0 for s in league.slots_for_position(pos))
        }
        wanted = [p for p in legal if p.position in needed] or legal
        return max(wanted, key=lambda p: p.vorp)
    return legal[0]


def _state(draft_id: str, league: LeagueSettings, rounds: int, picks: list[dict]) -> DraftState:
    draft = {
        "draft_id": draft_id,
        "type": "snake",
        "status": "drafting",
        "settings": {"teams": league.teams, "rounds": rounds},
        "slot_to_roster_id": {str(i): i for i in range(1, league.teams + 1)},
        "draft_order": {},
    }
    return DraftState.from_api(draft, picks)


def run_draft(
    league: LeagueSettings,
    players: list[Player],
    replacement: dict[str, float],
    seed: int,
    focal_slot: int,
    focal_strategy: str,
    opponent_strategy: str = "adp",
    rounds: int = 15,
    cfg: RolloutConfig | None = None,
) -> list[Team]:
    """One full draft. Only the focal seat's strategy varies between runs."""
    cfg = cfg or RolloutConfig(sims=120)
    _pool_cache["players"] = players
    board = market_order(players, seed)
    teams = {
        slot: Team(
            slot=slot,
            strategy=focal_strategy if slot == focal_slot else opponent_strategy,
            roster_id=slot,
        )
        for slot in range(1, league.teams + 1)
    }
    picks: list[dict] = []
    taken: set[str] = set()
    state = _state("backtest", league, rounds, picks)

    for pick_no in range(1, league.teams * rounds + 1):
        slot = state.slot_at_pick(pick_no)
        team = teams[slot]
        rounds_left = rounds - (pick_no - 1) // league.teams

        if team.strategy == "engine":
            state.picks = picks
            mine = [
                n for n in range(pick_no, league.teams * rounds + 1)
                if state.slot_at_pick(n) == slot
            ][:8]
            recs = recommend(
                league, state, players, replacement, slot, mine, limit=1, cfg=cfg
            )
            choice = recs[0].player if recs else None
        else:
            following = next(
                (n for n in range(pick_no + 1, league.teams * rounds + 1)
                 if state.slot_at_pick(n) == slot),
                0,
            )
            choice = baseline_pick(
                team, league, board, taken, rounds_left, pick_no, following
            )

        if choice is None:
            continue
        taken.add(choice.player_id)
        team.players.append(choice)
        picks.append(
            {
                "pick_no": pick_no,
                "round": (pick_no - 1) // league.teams + 1,
                "roster_id": slot,
                "player_id": choice.player_id,
                "metadata": {},
            }
        )
    return list(teams.values())


def score_team(
    evaluator: LineupEvaluator, team: Team, actual: dict[str, float]
) -> tuple[float, float, float]:
    """Points the roster really produced: as seated in hindsight, as projected, blended."""
    by_realised: dict[str, list[tuple[float, float]]] = {}
    by_projection: dict[str, list[tuple[float, float]]] = {}
    bye_roster: dict[str, list[tuple[float, int]]] = {}
    for p in team.players:
        got = actual.get(p.player_id, 0.0)
        by_realised.setdefault(p.position, []).append((got, got))
        by_projection.setdefault(p.position, []).append((p.value, got))
        bye_roster = add_to_roster(bye_roster, p.position, got, p.bye or 0)
    for pool in by_realised.values():
        pool.sort(key=lambda e: -e[0])
    for pool in by_projection.values():
        pool.sort(key=lambda e: -e[0])
    informed, _ = evaluator.seat(by_realised)
    naive, _ = evaluator.seat(by_projection)
    penalty = evaluator.bye_penalty(bye_roster, informed)
    blended = HINDSIGHT * informed + (1 - HINDSIGHT) * naive + penalty
    return informed, naive, blended


@dataclass
class Result:
    strategy: str
    points: list[float] = field(default_factory=list)
    ranks: list[int] = field(default_factory=list)

    @property
    def mean_points(self) -> float:
        return sum(self.points) / max(1, len(self.points))

    @property
    def mean_rank(self) -> float:
        return sum(self.ranks) / max(1, len(self.ranks))

    @property
    def win_rate(self) -> float:
        return sum(1 for r in self.ranks if r == 1) / max(1, len(self.ranks))

    @property
    def top3_rate(self) -> float:
        return sum(1 for r in self.ranks if r <= 3) / max(1, len(self.ranks))


def prepare(
    client: SleeperClient, league: LeagueSettings, season: str
) -> tuple[list[Player], dict[str, float], dict[str, float]]:
    players = load_players(client, league, season)
    # Sleeper's player metadata is live, not historical: it carries whatever
    # injury tag that player has *today*, regardless of which season is being
    # replayed. Applying it to a past season is look-ahead bias - a player on
    # injured reserve right now would get discounted for a season in which he
    # was healthy on draft day. There is no historical injury feed to replace
    # it with, so the honest choice is to drop it rather than use wrong
    # information.
    for p in players:
        p.injury_status = None
    replacement = replacement_levels(players, league.replacement_ranks())
    apply_reliability(players, replacement)
    attach_risk(players)
    apply_vorp(players, replacement)
    return players, replacement, load_actuals(client, league, season)


def run(
    league: LeagueSettings,
    players: list[Player],
    replacement: dict[str, float],
    actual: dict[str, float],
    reps: int = 2,
    slots: list[int] | None = None,
    strategies: tuple[str, ...] = STRATEGIES,
    rounds: int = 15,
    cfg: RolloutConfig | None = None,
    opponent: str = "adp",
    progress=None,
) -> dict[str, Result]:
    """Run every strategy through identical drafts and compare the focal seat.

    Each (slot, repetition) fixes a seed, so all strategies face the same board
    and the same opponents. Only the focal seat changes, which is what isolates
    the strategy from the luck of the draft.
    """
    evaluator = LineupEvaluator.from_league(
        league, actual_replacement(players, actual, league.replacement_ranks())
    )
    slots = slots or list(range(1, league.teams + 1))
    results = {s: Result(s) for s in strategies}

    for rep in range(reps):
        for slot in slots:
            seed = 1000 * rep + slot
            for strategy in strategies:
                teams = run_draft(
                    league, players, replacement, seed, slot, strategy,
                    opponent_strategy=opponent, rounds=rounds, cfg=cfg,
                )
                scored = [(t, score_team(evaluator, t, actual)[2]) for t in teams]
                scored.sort(key=lambda kv: -kv[1])
                for rank, (team, points) in enumerate(scored, start=1):
                    if team.slot == slot:
                        results[strategy].points.append(points)
                        results[strategy].ranks.append(rank)
                        break
                if progress:
                    progress(rep, slot, strategy)
    return results


def report(results: dict[str, Result], season: str) -> str:
    lines = [
        f"Backtest on the {season} season - drafted on preseason projections, "
        f"scored on real results",
        "",
        f"{'strategy':<10}{'drafts':>7}{'points':>9}{'vs ADP':>9}{'avg rank':>10}"
        f"{'wins':>7}{'top 3':>8}",
    ]
    baseline = results.get("adp")
    base_points = baseline.mean_points if baseline and baseline.points else 0.0
    for name, r in sorted(results.items(), key=lambda kv: -kv[1].mean_points):
        if not r.points:
            continue
        delta = r.mean_points - base_points
        lines.append(
            f"{name:<10}{len(r.points):>7}{r.mean_points:>9.0f}{delta:>+9.0f}"
            f"{r.mean_rank:>10.2f}{r.win_rate:>7.0%}{r.top3_rate:>8.0%}"
        )
    return "\n".join(lines)
