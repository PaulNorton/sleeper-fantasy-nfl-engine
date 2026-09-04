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

# Residual value of a backup. This used to carry all of a bench player's worth
# and no longer does, for two reasons: a bench player who breaks out is seated
# in the lineup by the optimiser anyway, and every slot is floored at the waiver
# wire, so depth you drafted competes with depth you could pick up for free.
#
# Swept against 2024 and 2025 over 60 paired drafts. Scaling these values by
# zero is worth +2.2 +/- 4.1 points, by a half +2.5 +/- 2.7, by three
# -5.1 +/- 3.1, by eight -13.6 +/- 5.3. So bench depth is close to worthless
# and certainly not worth more than this; the shipped values sit at the flat
# top of that curve.
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


# Discount for a single week's projection, distinct from the season-long
# `INJURY_DISCOUNT` above. That table softens a full season for a player who is
# banged up now but expected back; scoring one week is closer to a coin flip
# than a haircut, since "Out" means he does not play that game at all. Not
# fitted to data - a designation-to-probability guess, grouped here with the
# other one so the two are easy to tell apart and easy to argue with.
WEEKLY_STATUS_DISCOUNT = {
    "IR": 0.0,
    "PUP": 0.0,
    "NA": 0.0,
    "Sus": 0.0,
    "Out": 0.02,
    "Doubtful": 0.25,
    "Questionable": 0.85,
}


def weekly_injury_multiplier(status: str | None) -> float:
    if not status:
        return 1.0
    return WEEKLY_STATUS_DISCOUNT.get(status, 0.9)


# position -> [(-points, bye week)], sorted so the best player comes first.
Roster = dict[str, list[tuple[float, int]]]

# Games in a season. A bye costs you one week of one starter, so the penalty for
# a clash is scaled by this.
SEASON_WEEKS = 17


def empty_roster() -> Roster:
    return {}


def add_to_roster(roster: Roster, pos: str, points: float, bye: int = 0) -> Roster:
    """Return a shallow copy with one player added, keeping lists sorted."""
    new = dict(roster)
    lst = list(new.get(pos, ()))
    insort(lst, (-points, bye))
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

    def starter_points(
        self, roster: Roster, skip_bye: int = 0
    ) -> tuple[float, dict[str, int]]:
        """Optimal starting lineup total, plus how many of each position it used.

        `skip_bye` leaves out everyone on that bye week, which is how the bye
        penalty is worked out: seat the lineup again without them and see what
        the week costs.
        """
        used: dict[str, int] = {}
        total = 0.0
        for pos, count in self.dedicated.items():
            lst = roster.get(pos) or []
            if skip_bye:
                lst = [e for e in lst if e[1] != skip_bye]
            floor = self.replacement.get(pos, 0.0)
            take = min(count, len(lst))
            total += sum(max(-v, floor) for v, _b in lst[:take])  # stored negated
            total += (count - take) * floor
            used[pos] = take
        pools = {
            pos: ([e for e in lst if e[1] != skip_bye] if skip_bye else lst)
            for pos, lst in roster.items()
        }
        for _slot, count, eligible in self.flex:
            for _ in range(count):
                best_pos = None
                best_val = 0.0
                for pos in eligible:
                    lst = pools.get(pos)
                    if not lst:
                        continue
                    i = used.get(pos, 0)
                    if i < len(lst) and -lst[i][0] > best_val:
                        best_val = -lst[i][0]
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
            for value, _bye in lst[used.get(pos, 0) :]:
                gain = -value - floor
                if gain > 0:
                    total += factor * gain
        return total

    def bye_penalty(self, roster: Roster, full: float) -> float:
        """What sharing bye weeks costs you over a season.

        Two starters on the same bye means one week of starting a waiver body in
        a slot that should have held a starter. The cost is worked out exactly -
        the lineup is seated again for each bye week your roster actually has -
        rather than by a rule of thumb, because whether it hurts depends on
        whether your bench happens to cover that position.
        """
        weeks = {bye for lst in roster.values() for _v, bye in lst if bye}
        if not weeks:
            return 0.0
        penalty = 0.0
        for week in weeks:
            without, _ = self.starter_points(roster, skip_bye=week)
            penalty += (without - full) / SEASON_WEEKS
        return penalty

    def utility(self, roster: Roster, byes: bool = True) -> float:
        total, used = self.starter_points(roster)
        value = total + self.bench_value(roster, used)
        if byes:
            value += self.bye_penalty(roster, total)
        return value

    def marginal(
        self,
        roster: Roster,
        pos: str,
        points: float,
        base: float | None = None,
        bye: int = 0,
    ) -> float:
        """Utility gained by adding one player. `base` avoids recomputing utility(roster)."""
        if base is None:
            base = self.utility(roster)
        return self.utility(add_to_roster(roster, pos, points, bye)) - base

    def contribution(self, roster: Roster, pos: str, points: float, bye: int = 0) -> float:
        """What a specific rostered player is currently worth to you - what you
        lose by dropping him. Symmetric with `marginal`: computed on the roster
        with him removed, so a replaceable bench body scores near zero even
        though his own projection is not.
        """
        without = remove_from_roster(roster, pos, points, bye)
        return self.utility(roster) - self.utility(without)


def remove_from_roster(roster: Roster, pos: str, points: float, bye: int = 0) -> Roster:
    """Return a shallow copy with one matching player removed.

    A no-op if nothing matches, so a stale or rounded `points` fails quietly
    rather than raising - callers that need to know whether the drop landed
    should check the returned roster themselves.
    """
    new = dict(roster)
    lst = list(new.get(pos, ()))
    try:
        lst.remove((-points, bye))
    except ValueError:
        pass
    new[pos] = lst
    return new


def roster_from_players(players) -> Roster:
    roster: Roster = {}
    for p in players:
        roster = add_to_roster(roster, p.position, p.value, p.bye or 0)
    return roster


def outcome_utility(
    evaluator: LineupEvaluator,
    entries: list[tuple[str, float, float, int]],
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
    bye_roster: Roster = {}
    for pos, projection, realised, bye in entries:
        by_realised.setdefault(pos, []).append((realised, realised))
        by_projection.setdefault(pos, []).append((projection, realised))
        bye_roster = add_to_roster(bye_roster, pos, realised, bye)
    for pool in by_realised.values():
        pool.sort(key=lambda e: -e[0])
    for pool in by_projection.values():
        pool.sort(key=lambda e: -e[0])

    informed, used = evaluator.seat(by_realised)
    naive, _ = evaluator.seat(by_projection)
    total = hindsight * informed + (1.0 - hindsight) * naive
    total += evaluator.bye_penalty(bye_roster, informed)

    for pos, pool in by_realised.items():
        factor = BENCH_FACTOR.get(pos, 0.05)
        if not factor:
            continue
        floor = evaluator.replacement.get(pos, 0.0)
        for realised, _ in pool[used.get(pos, 0) :]:
            if realised > floor:
                total += factor * (realised - floor)
    return total
