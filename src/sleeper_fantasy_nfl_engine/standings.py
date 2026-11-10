"""What each remaining week decides, and so how much a point in it is worth.

A waiver move is paid in points, but the season is decided in games. A point
in a week whose game cannot change where you finish is worth less than a point
in a playoff week you will certainly play. This module turns your record, the
schedule and every team's projected lineup into a weight per week.

**The yardstick is title odds.** Every playoff game is taken as a coin flip,
so a team that makes the playoffs wins the title with probability one half per
round it has to play, and a first-round bye saves a round.

  - A **regular-season week** is worth how far winning it instead of losing it
    moves your title odds, through the playoff place and the bye. That comes
    from playing the rest of the regular season out a few thousand times. It is
    then scaled by how close the game is: a point matters most when the two
    lineups project level.
  - A **playoff week** is worth the chance you are still playing in it, times
    the chance you win every round after it.

A team already safe gets small weights on the regular season and large ones on
the playoffs. A team on the bubble gets the opposite. A team with a bye gets
nothing for the week it sits out.

The weights average one over the weeks being valued, up to the final, so a
weighted total is still read in points. They are also only half applied - see `PLAYOFF_FOCUS`.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field

# How far the weights lean towards title odds, from 0 (every week counts the
# same) to 1 (only title odds count). A judgement call. Points have a worth of
# their own - seeding tie-breaks, a points prize, a consolation bracket - and
# the playoff odds behind the weights are themselves a rough estimate. Halfway
# takes the direction and distrusts the size.
PLAYOFF_FOCUS = 0.5

# Standard deviation of one team's weekly score around its projection. Used
# only when the league has too few played weeks to measure its own.
TEAM_SD = 25.0

# Regular seasons played out to estimate the odds.
SEASONS = 4000

# Simulated seasons needed on each side of a win/loss split before the
# difference between them is believed.
MIN_SPLIT = 25


@dataclass
class Leverage:
    """A weight per week, and the odds they came from."""

    weights: dict[int, float]
    playoff_odds: float | None = None
    bye_odds: float | None = None
    playoff_weeks: list[int] = field(default_factory=list)
    # Why every week counts the same, when it does.
    reason: str = ""

    def weight(self, week: int) -> float:
        return self.weights.get(week, 1.0)

    @property
    def flat(self) -> bool:
        return all(abs(w - 1.0) < 1e-9 for w in self.weights.values())


def flat(weeks: list[int], reason: str = "") -> Leverage:
    return Leverage(weights={wk: 1.0 for wk in weeks}, reason=reason)


def score_spread(history: list[dict[int, float]]) -> float:
    """How far a team's weekly score sits from its own average, in this league.

    `history` is one dict per played week, roster_id -> points. With fewer than
    four weeks there is too little to measure and `TEAM_SD` stands in.
    """
    by_team: dict[int, list[float]] = {}
    for week in history:
        for rid, pts in week.items():
            if pts > 0:
                by_team.setdefault(rid, []).append(pts)
    resid = [
        pts - statistics.mean(scores)
        for scores in by_team.values() if len(scores) >= 4
        for pts in scores
    ]
    if len(resid) < 20:
        return TEAM_SD
    teams = sum(1 for scores in by_team.values() if len(scores) >= 4)
    return math.sqrt(sum(r * r for r in resid) / (len(resid) - teams))


def _density(margin: float, sd: float) -> float:
    """How much one point moves the chance of winning a game projected `margin` apart."""
    spread = sd * math.sqrt(2.0)
    return math.exp(-0.5 * (margin / spread) ** 2) / (spread * math.sqrt(2.0 * math.pi))


def week_weights(
    weeks: list[int],
    me: int,
    records: dict[int, tuple[float, float]],
    schedule: dict[int, list[tuple[int, int]]],
    means: dict[int, dict[int, float]],
    playoff_start: int,
    playoff_teams: int,
    sd: float = TEAM_SD,
    seasons: int = SEASONS,
    seed: int = 0,
) -> Leverage:
    """Weight each of `weeks` by what it decides for roster `me`.

    `records` is roster_id -> (wins so far with a tie as half, points for).
    `schedule` is week -> the games still to play in it. `means` is roster_id
    -> week -> projected lineup points; a week with no projection uses the
    team's average over the weeks that have one.
    """
    if not weeks:
        return flat(weeks)
    if me not in records or playoff_teams < 2 or playoff_start <= 0:
        return flat(weeks, "the league has no playoff settings to read")
    if weeks[0] >= playoff_start:
        return flat(weeks, "the playoffs are under way")
    teams = sorted(records)
    rounds = max(1, math.ceil(math.log2(playoff_teams)))
    byes = 2 ** rounds - playoff_teams
    final = playoff_start + rounds - 1
    regular = [wk for wk in range(weeks[0], playoff_start)]
    if any(not any(me in game for game in schedule.get(wk, [])) for wk in regular):
        return flat(weeks, "the schedule for the rest of the regular season is not published")

    def mean(rid: int, week: int) -> float:
        own = means.get(rid, {})
        if week in own:
            return own[week]
        return statistics.mean(own.values()) if own else 0.0

    with_bye, without = 0.5 ** (rounds - 1), 0.5 ** rounds
    rng = random.Random(seed)
    made = bye = 0
    won: dict[int, list[float]] = {wk: [] for wk in regular}
    lost: dict[int, list[float]] = {wk: [] for wk in regular}
    for _ in range(seasons):
        wins = {rid: records[rid][0] for rid in teams}
        scored = {rid: records[rid][1] for rid in teams}
        mine: dict[int, bool] = {}
        for wk in regular:
            score = {rid: rng.gauss(mean(rid, wk), sd) for rid in teams}
            for a, b in schedule.get(wk, []):
                winner = a if score[a] > score[b] else b
                wins[winner] += 1
                if me in (a, b):
                    mine[wk] = winner == me
            for rid in teams:
                scored[rid] += score[rid]
        order = sorted(teams, key=lambda rid: (-wins[rid], -scored[rid]))
        place = order.index(me) + 1
        title = with_bye if place <= byes else without if place <= playoff_teams else 0.0
        made += place <= playoff_teams
        bye += place <= byes
        for wk, result in mine.items():
            (won if result else lost)[wk].append(title)
    p_made, p_bye = made / seasons, bye / seasons

    raw: dict[int, float] = {}
    for wk in weeks:
        if wk in won:
            if min(len(won[wk]), len(lost[wk])) < MIN_SPLIT:
                raw[wk] = 0.0            # the game is not in doubt
                continue
            rival = next(b if a == me else a for a, b in schedule[wk] if me in (a, b))
            swing = statistics.mean(won[wk]) - statistics.mean(lost[wk])
            raw[wk] = max(0.0, swing) * _density(mean(me, wk) - mean(rival, wk), sd)
        elif wk <= final:
            rnd = wk - playoff_start + 1
            # Still playing in this round: a bye team sits out the first and is
            # certain to play the second.
            alive = (p_made - p_bye) * 0.5 ** (rnd - 1)
            if rnd >= 2:
                alive += p_bye * 0.5 ** (rnd - 2)
            raw[wk] = alive * 0.5 ** (rounds - rnd) * _density(0.0, sd)
        else:
            raw[wk] = 0.0                # the title is already decided

    # Averaged over the weeks that still count, so the weights there average one.
    level = statistics.mean([raw[wk] for wk in weeks if wk <= final] or [0.0])
    playoff_weeks = [wk for wk in weeks if playoff_start <= wk <= final]
    if level <= 0:
        return Leverage(weights={wk: 1.0 for wk in weeks}, playoff_odds=p_made, bye_odds=p_bye,
                        playoff_weeks=playoff_weeks,
                        reason="no week left can change where you finish")
    weights = {
        # A week after the final decides nothing at all, points included.
        wk: 0.0 if wk > final else (1.0 - PLAYOFF_FOCUS) + PLAYOFF_FOCUS * raw[wk] / level
        for wk in weeks
    }
    return Leverage(weights=weights, playoff_odds=p_made, bye_odds=p_bye,
                    playoff_weeks=playoff_weeks)


def league_leverage(client, league, rosters: list[dict], me: int, horizon) -> Leverage:
    """Read the standings and schedule from Sleeper and weight `horizon.weeks`.

    Any failure - a league with no playoffs, two-week playoff rounds, a
    schedule Sleeper has not published - returns flat weights and the reason.
    """
    from .valuation import LineupEvaluator
    from .waivers import horizon_value

    weeks = horizon.weeks
    settings = league.settings
    if not weeks:
        return flat(weeks)
    if int(settings.get("best_ball") or 0):
        return flat(weeks, "a best-ball league has no weekly games")
    if int(settings.get("playoff_round_type") or 0):
        return flat(weeks, "this league plays two-week playoff rounds")
    playoff_start = int(settings.get("playoff_week_start") or 0)
    playoff_teams = int(settings.get("playoff_teams") or 0)
    try:
        records: dict[int, tuple[float, float]] = {}
        for r in rosters:
            s = r.get("settings") or {}
            records[int(r["roster_id"])] = (
                float(s.get("wins") or 0) + 0.5 * float(s.get("ties") or 0),
                float(s.get("fpts") or 0) + float(s.get("fpts_decimal") or 0) / 100.0,
            )
        schedule: dict[int, list[tuple[int, int]]] = {}
        for wk in range(weeks[0], playoff_start):
            pairs: dict[int, list[int]] = {}
            for m in client.matchups(league.league_id, wk):
                if m.get("matchup_id") is not None:
                    pairs.setdefault(int(m["matchup_id"]), []).append(int(m["roster_id"]))
            schedule[wk] = [(p[0], p[1]) for p in pairs.values() if len(p) == 2]
        history = [
            {int(m["roster_id"]): float(m.get("points") or 0.0)
             for m in client.matchups(league.league_id, wk, ttl=86400)}
            for wk in range(int(settings.get("start_week") or 1), weeks[0])
        ]
        evaluator = LineupEvaluator.from_league(league, {})
        means = {
            int(r["roster_id"]): horizon_value(
                evaluator, horizon,
                [horizon.players[str(pid)] for pid in (r.get("players") or [])
                 if str(pid) in horizon.players],
            )
            for r in rosters
        }
        return week_weights(weeks, me, records, schedule, means, playoff_start,
                            playoff_teams, sd=score_spread(history))
    except Exception as exc:  # a network failure must not take the run with it
        return flat(weeks, f"the standings could not be read ({exc})")
