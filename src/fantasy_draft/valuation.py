"""Roster utility: what a player is actually worth to *your* lineup.

The objective every other module optimises is:

    utility(roster) = projected points of the optimal starting lineup
                    + bench insurance (discounted value of backups)

A player's worth is the increase in that number, not his raw projection.
That is why a third quarterback in a one-QB league scores near zero: he never
enters the starting lineup and backs up a position that rarely needs cover.
"""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass

from .league import FLEX_MAP, LeagueSettings

# Residual value of a backup once outcomes are simulated. This used to carry
# all of a bench player's worth. It no longer does: when the simulation draws
# realised seasons, a bench player who breaks out is seated in the lineup by
# the optimiser, so his upside is already counted. What is left here is the
# part season totals miss - bye-week fill-ins and mid-season streaming.
BENCH_FACTOR = {
    "QB": 0.04,
    "RB": 0.09,
    "WR": 0.08,
    "TE": 0.04,
    "K": 0.0,
    "DEF": 0.01,
    "DL": 0.04,
    "LB": 0.04,
    "DB": 0.04,
}

# How much of a breakout you actually capture. Seating a lineup by realised
# season totals assumes perfect hindsight, which flatters upside: you do not
# know in week 1 who the breakout is. Seating by projection assumes you never
# adjust, which is too pessimistic. Real managers learn within a few weeks and
# capture most of it, so the two are blended.
HINDSIGHT = 0.70

# Multiplier applied to a projection given the player's injury designation.
INJURY_DISCOUNT = {
    "IR": 0.70,
    "PUP": 0.75,
    "NA": 0.80,
    "Sus": 0.80,
    "Out": 0.88,
    "Doubtful": 0.94,
    "Questionable": 1.0,
}


def injury_multiplier(status: str | None) -> float:
    if not status:
        return 1.0
    return INJURY_DISCOUNT.get(status, 0.95)


Roster = dict[str, list[float]]  # position -> points, descending


def empty_roster() -> Roster:
    return {}


def add_to_roster(roster: Roster, pos: str, points: float) -> Roster:
    """Return a shallow copy with one player added, keeping lists sorted."""
    new = dict(roster)
    lst = list(new.get(pos, ()))
    insort(lst, -points)
    new[pos] = lst
    return new


@dataclass
class LineupEvaluator:
    """Scores a roster for one league's starting requirements."""

    dedicated: dict[str, int]
    flex: list[tuple[str, int, tuple[str, ...]]]
    bench_size: int
    replacement: dict[str, float]

    @classmethod
    def from_league(
        cls, league: LeagueSettings, replacement: dict[str, float]
    ) -> "LineupEvaluator":
        dedicated: dict[str, int] = {}
        flex: list[tuple[str, int, tuple[str, ...]]] = []
        for slot, count in league.starter_slots.items():
            if slot in FLEX_MAP:
                flex.append((slot, count, FLEX_MAP[slot]))
            else:
                dedicated[slot] = count
        # Fill the most restrictive flex first; with nested eligibility that is optimal.
        flex.sort(key=lambda f: len(f[2]))
        return cls(dedicated, flex, league.bench_size, replacement)

    def seat(self, pools: dict[str, list[tuple[float, float]]]) -> tuple[float, dict[str, int]]:
        """Fill the lineup choosing by each entry's first value, scoring its second.

        Entries are (ranking_key, payoff). Passing the same number twice scores a
        lineup on its own projections; passing (projection, realised) scores the
        lineup a manager would have set, on what actually happened.
        Each pool must be sorted by ranking_key, descending.

        Every slot is floored at replacement level, whether or not you drafted
        someone for it. An empty slot valued at zero makes drafting any kicker
        look worth his whole 78 points, when every team streams one for about
        the same. And a drafted starter who collapses is not worth his 40 points
        either - you drop him in October and stream the position.

        Flooring the downside is not a detail. It is most of what makes upside
        worth paying for: a boom-or-bust player's bad seasons cost you only the
        gap down to the waiver wire, while his good ones are yours in full.
        """
        used: dict[str, int] = {}
        total = 0.0
        for pos, count in self.dedicated.items():
            pool = pools.get(pos) or []
            floor = self.replacement.get(pos, 0.0)
            take = min(count, len(pool))
            total += sum(max(payoff, floor) for _key, payoff in pool[:take])
            total += (count - take) * floor
            used[pos] = take
        for _slot, count, eligible in self.flex:
            for _ in range(count):
                best_pos = None
                best_key = float("-inf")
                for pos in eligible:
                    pool = pools.get(pos)
                    if not pool:
                        continue
                    i = used.get(pos, 0)
                    if i < len(pool) and pool[i][0] > best_key:
                        best_key = pool[i][0]
                        best_pos = pos
                floor = max(
                    (self.replacement.get(pos, 0.0) for pos in eligible), default=0.0
                )
                if best_pos is None:
                    total += floor
                    continue
                total += max(pools[best_pos][used.get(best_pos, 0)][1], floor)
                used[best_pos] = used.get(best_pos, 0) + 1
        return total, used

    def starter_points(self, roster: Roster) -> tuple[float, dict[str, int]]:
        """Optimal starting lineup total, plus how many of each position it used."""
        used: dict[str, int] = {}
        total = 0.0
        for pos, count in self.dedicated.items():
            lst = roster.get(pos) or []
            floor = self.replacement.get(pos, 0.0)
            take = min(count, len(lst))
            total += sum(max(-v, floor) for v in lst[:take])  # stored negated
            total += (count - take) * floor
            used[pos] = take
        for _slot, count, eligible in self.flex:
            for _ in range(count):
                best_pos = None
                best_val = 0.0
                for pos in eligible:
                    lst = roster.get(pos)
                    if not lst:
                        continue
                    i = used.get(pos, 0)
                    if i < len(lst) and -lst[i] > best_val:
                        best_val = -lst[i]
                        best_pos = pos
                floor = max(
                    (self.replacement.get(pos, 0.0) for pos in eligible), default=0.0
                )
                if best_pos is None:
                    total += floor
                    continue
                total += max(best_val, floor)
                used[best_pos] = used.get(best_pos, 0) + 1
        return total, used

    def bench_value(self, roster: Roster, used: dict[str, int]) -> float:
        """Insurance value of everyone who did not make the starting lineup."""
        total = 0.0
        for pos, lst in roster.items():
            factor = BENCH_FACTOR.get(pos, 0.1)
            if not factor:
                continue
            floor = self.replacement.get(pos, 0.0)
            for value in lst[used.get(pos, 0) :]:
                gain = -value - floor
                if gain > 0:
                    total += factor * gain
        return total

    def utility(self, roster: Roster) -> float:
        total, used = self.starter_points(roster)
        return total + self.bench_value(roster, used)

    def marginal(self, roster: Roster, pos: str, points: float, base: float | None = None) -> float:
        """Utility gained by adding one player. `base` avoids recomputing utility(roster)."""
        if base is None:
            base = self.utility(roster)
        return self.utility(add_to_roster(roster, pos, points)) - base


def roster_from_players(players) -> Roster:
    roster: Roster = {}
    for p in players:
        roster = add_to_roster(roster, p.position, p.value)
    return roster


def outcome_utility(
    evaluator: LineupEvaluator,
    entries: list[tuple[str, float, float]],
    hindsight: float = HINDSIGHT,
) -> float:
    """Score a finished roster of (position, projection, realised season) players.

    The lineup is scored twice on the same realised seasons: once seated the way
    a manager who reacted to the season would have set it, once seated the way
    the preseason projections said. Blending the two is what stops a boom/bust
    bench player being credited with upside nobody could have timed.
    """
    by_realised: dict[str, list[tuple[float, float]]] = {}
    by_projection: dict[str, list[tuple[float, float]]] = {}
    for pos, projection, realised in entries:
        by_realised.setdefault(pos, []).append((realised, realised))
        by_projection.setdefault(pos, []).append((projection, realised))
    for pool in by_realised.values():
        pool.sort(key=lambda e: -e[0])
    for pool in by_projection.values():
        pool.sort(key=lambda e: -e[0])

    informed, used = evaluator.seat(by_realised)
    naive, _ = evaluator.seat(by_projection)
    total = hindsight * informed + (1.0 - hindsight) * naive

    for pos, pool in by_realised.items():
        factor = BENCH_FACTOR.get(pos, 0.05)
        if not factor:
            continue
        floor = evaluator.replacement.get(pos, 0.0)
        for realised, _ in pool[used.get(pos, 0) :]:
            if realised > floor:
                total += factor * (realised - floor)
    return total
