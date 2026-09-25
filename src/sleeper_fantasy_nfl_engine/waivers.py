"""Should you make a move on the wire, and which one.

One question, not two: signing a player and dropping one to make room is a
single decision, so adds and drops are scored as a *pair* and ranked by what
the pair is worth. Ranking them apart hides the case that matters - a good
free agent you cannot afford the roster spot for, and a droppable body you
have no use for the spot.

The yardstick is **rest-of-season points, week by week**. Your roster is
seated into a starting lineup separately for each week left, using that week's
own projections, and the weeks are summed. Everything the draft model has to
approximate falls out of that for free:

  - **Byes need no special case.** A player on bye has no stat line that week,
    so he is simply absent from it. If four of your receivers are out in week
    14, the week-14 lineup seats whoever is left and scores the hole honestly.
  - **Bench value stops being a fudge factor.** `valuation.BENCH_FACTOR` exists
    because a season-long model cannot see *when* a backup plays. Here a bench
    player earns his value in the weeks he is actually seated, and nowhere else.
  - **Games already played do not count.** A season projection includes weeks
    that are over. Only the weeks ahead of you can still be influenced by a
    move, so only those are summed.

An empty starter slot scores **zero** here, not replacement level, which is the
one place this model deliberately parts company with the draft engine. The
draft engine floors every slot at the waiver wire because it knows you will
stream someone eventually; that assumption is exactly what is being decided
now, so assuming it would score every pickup at nothing. Within one real week,
an unfilled slot scores what an unfilled slot scores.

Weekly projections are used unshrunk. `projections.SHRINKAGE` was fitted on
season-long projections against season-long results, and there is no
equivalent measurement for a single week; applying it here would be an
unvalidated transform dressed up as a correction.

Expert consensus is blended in, though, because that is a second opinion
rather than a transform. FantasyPros only publishes weekly rankings for the
next week, so the weeks after it borrow the rest-of-season ranking instead,
at a lower weight - see `load_horizon`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .fantasypros import load_ros_consensus, load_weekly_consensus
from .league import BENCH_SLOTS, LeagueSettings
from .projections import (
    ROS_CONSENSUS_WEIGHT,
    WEEKLY_CONSENSUS_WEIGHT,
    Player,
    injury_multiplier,
    load_week_points,
    weekly_injury_multiplier,
)
from .sleeper import SleeperClient
from .valuation import LineupEvaluator

# Last week worth planning for. The NFL plays 18, but week 18 is a
# rest-the-starters lottery that almost no league scores, and `valuation.py`
# already treats the fantasy season as 17 weeks long.
SEASON_END_WEEK = 17

# Rest-of-season points a move has to be worth before it is worth making.
# A judgement call, not a fitted number: below roughly this much the model is
# not distinguishing real gain from the noise in its own weekly projections,
# and churning a roster spot has costs this model does not see (FAAB, the
# waiver priority you burn, the player you cannot re-add next week).
WORTH_DOING = 3.0

# How many candidates survive the cheap first pass into the exact pairwise one.
# The shortlists only need to be wide enough to contain the real answer; every
# pair beyond that is arithmetic nobody reads.
ADD_SHORTLIST = 40
DROP_SHORTLIST = 8

# Share of the remaining weeks a player has to start in before he counts as a
# roster upgrade rather than cover for one week. Relative, not a flat count:
# starting two weeks out of seventeen is a bye fill-in, starting two out of the
# last three is a contributor.
STREAMER_SHARE = 0.2


@dataclass
class Horizon:
    """Every week left, and what each player is projected for in it."""

    weeks: list[int]
    points: dict[int, dict[str, float]]      # week -> player_id -> points

    def week_points(self, week: int, player: Player) -> float:
        return self.points.get(week, {}).get(player.player_id, 0.0)


@dataclass
class Move:
    """A signing, and the drop it costs you."""

    add: Player | None
    drop: Player | None
    gain: float                              # rest-of-season points
    by_week: dict[int, float] = field(default_factory=dict)
    starts: int = 0                          # weeks left he would actually start
    first_week: int = 0                      # the week being decided now

    @property
    def peak_week(self) -> int | None:
        """The single week the move helps most, if one dominates."""
        if not self.by_week:
            return None
        week, value = max(self.by_week.items(), key=lambda kv: kv[1])
        if value <= 0 or self.gain <= 0:
            return None
        return week if value >= 0.5 * self.gain else None

    @property
    def can_wait(self) -> bool:
        """True when this is a streamer for one future week, not an upgrade.

        A player whose value lands in one future week, and who would start in
        only a handful of the weeks left, is cover for a bye - and the wire
        will still be offering someone just like him the week before it.
        Acting now spends a roster spot early for nothing, so the honest advice
        is a date, not a signing. Somebody who walks into your lineup week
        after week is the opposite: he will not be there later, and waiting is
        how you lose him.
        """
        peak = self.peak_week
        if peak is None or peak <= self.first_week:
            return False
        return self.starts <= max(1, round(STREAMER_SHARE * len(self.by_week)))

    def reason(self) -> str:
        peak = self.peak_week
        if self.can_wait:
            return f"week {peak} cover — wait until week {max(self.first_week, peak - 1)}"
        if peak is not None:
            return f"mostly week {peak}"
        return "steady upgrade"


def load_horizon(
    client: SleeperClient,
    league: LeagueSettings,
    season: str,
    weeks: list[int],
    workers: int = 6,
    consensus: bool = True,
) -> Horizon:
    """Fetch every remaining week's projections, expert consensus included.

    Fetched in parallel because this is 6 positions per week and the weeks do
    not depend on each other. Each (week, position) is a distinct cache entry,
    so the threads never write the same file. The two FantasyPros rankings are
    fetched once, up front and in series, because they share a crawl delay.
    """
    imminent = weeks[0] if weeks else 0

    weekly_rows: list = []
    ros_rows: list = []
    if consensus:
        # A failed or blocked ranking must not take the whole waiver run with
        # it; without it the horizon is Sleeper-only, which is what it used to
        # be in every week.
        try:
            weekly_rows = load_weekly_consensus(league.scoring_format, client.cache_dir)
        except Exception:
            pass
        try:
            ros_rows = load_ros_consensus(league.scoring_format, client.cache_dir)
        except Exception:
            pass

    def one(week: int) -> tuple[int, dict[str, float]]:
        # Today's injury designation is a fact about this week's game and a
        # rumour about any later one, so only the imminent week gets the hard
        # weekly discount. TTL follows the same logic: a projection for the
        # week being played moves with every practice report, one for week 14
        # does not.
        #
        # The consensus splits on the same line. FantasyPros ranks the next
        # week and no further, so only the imminent week gets the weekly
        # ranking; the rest get the rest-of-season one, which is a durable
        # view of a player rather than a view of any particular fixture.
        near = week == imminent
        players = load_week_points(
            client, league, season, week,
            status_discount=weekly_injury_multiplier if near else injury_multiplier,
            ttl=3600 if near else 86400,
            consensus=weekly_rows if near else ros_rows,
            consensus_weight=WEEKLY_CONSENSUS_WEIGHT if near else ROS_CONSENSUS_WEIGHT,
        )
        return week, {p.player_id: p.points for p in players}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        boards = dict(pool.map(one, weeks))
    return Horizon(weeks=list(weeks), points=boards)


def remaining_weeks(current_week: int, limit: int | None = None) -> list[int]:
    weeks = list(range(max(1, current_week), SEASON_END_WEEK + 1))
    return weeks[:limit] if limit else weeks


def _seat_week(
    evaluator: LineupEvaluator, horizon: Horizon, week: int, players: list[Player]
) -> tuple[float, int]:
    """One week's starting lineup: (points, starter slots left empty)."""
    pools: dict[str, list[tuple[float, float]]] = {}
    for p in players:
        pts = horizon.week_points(week, p)
        if pts > 0:
            pools.setdefault(p.position, []).append((pts, pts))
    for pool in pools.values():
        pool.sort(key=lambda e: -e[0])
    total, used = evaluator.seat(pools)
    return total, sum(evaluator.dedicated.values()) + sum(
        n for _slot, n, _elig in evaluator.flex
    ) - sum(used.values())


def horizon_value(
    evaluator: LineupEvaluator, horizon: Horizon, players: list[Player]
) -> dict[int, float]:
    """Points this roster starts, week by week, over the rest of the season."""
    return {wk: _seat_week(evaluator, horizon, wk, players)[0] for wk in horizon.weeks}


def open_slots(
    evaluator: LineupEvaluator, horizon: Horizon, players: list[Player]
) -> dict[int, int]:
    """Starter slots this roster cannot fill, week by week. The bye-week view."""
    return {wk: _seat_week(evaluator, horizon, wk, players)[1] for wk in horizon.weeks}


def _total(by_week: dict[int, float]) -> float:
    return sum(by_week.values())


def _delta(after: dict[int, float], before: dict[int, float]) -> dict[int, float]:
    return {wk: after[wk] - before.get(wk, 0.0) for wk in after}


def _parked(roster_row: dict) -> set[str]:
    parked = {str(pid) for pid in (roster_row.get("reserve") or [])}
    return parked | {str(pid) for pid in (roster_row.get("taxi") or [])}


def open_roster_spots(league: LeagueSettings, roster_row: dict | None) -> int:
    """Active roster spots going spare. Players stashed on IR do not take one."""
    if not roster_row:
        return 0
    active_slots = sum(1 for slot in league.roster_positions if slot not in ("IR", "TAXI"))
    parked = _parked(roster_row)
    held = [pid for pid in (roster_row.get("players") or []) if str(pid) not in parked]
    return max(0, active_slots - len(held))


def droppable_ids(roster_row: dict | None) -> set[str]:
    """Players whose release frees an active roster spot.

    A player stashed on IR or taxi holds a slot of his own. Releasing him frees
    that slot, not the bench spot a signing needs, so Sleeper would refuse the
    add. He is never the price of a move.
    """
    if not roster_row:
        return set()
    parked = _parked(roster_row)
    return {str(pid) for pid in (roster_row.get("players") or []) if str(pid) not in parked}


def shortlist_free_agents(
    horizon: Horizon, free_agents: list[Player], per_position: int = 25
) -> list[Player]:
    """Trim the wire to the plausible before the expensive pass.

    Kept per position rather than overall: a tight end with 60 points left can
    matter more than a receiver with 90 if the tight end slot is the one you
    cannot fill, and an overall cut would have thrown him away first.
    """
    by_pos: dict[str, list[tuple[float, Player]]] = {}
    for fa in free_agents:
        total = sum(horizon.week_points(wk, fa) for wk in horizon.weeks)
        if total > 0:
            by_pos.setdefault(fa.position, []).append((total, fa))
    kept: list[Player] = []
    for group in by_pos.values():
        group.sort(key=lambda e: -e[0])
        kept.extend(fa for _total, fa in group[:per_position])
    return kept


def best_moves(
    league: LeagueSettings,
    horizon: Horizon,
    roster: list[Player],
    free_agents: list[Player],
    spare_spots: int = 0,
    limit: int = 10,
    droppable: set[str] | None = None,
) -> list[Move]:
    """Rank add/drop pairs by what they are worth over the rest of the season.

    Two passes. The first scores every free agent as a straight addition and
    every rostered player as a straight loss, which is linear and cheap. The
    second re-scores only the shortlisted combinations exactly, because an add
    and the drop that pays for it interact: dropping your third receiver is
    free until the receiver you signed is the one covering that bye.

    `droppable` limits who can pay for a signing - see `droppable_ids`. None
    means anyone on the roster.
    """
    # Every week is seated with its own evaluator, and every slot is floored at
    # zero rather than at replacement - see the module docstring.
    evaluator = LineupEvaluator.from_league(league, {})
    base = horizon_value(evaluator, horizon, roster)
    base_total = _total(base)
    first_week = horizon.weeks[0] if horizon.weeks else 0

    adds: list[tuple[float, Player, dict[int, float]]] = []
    for fa in free_agents:
        after = horizon_value(evaluator, horizon, roster + [fa])
        adds.append((_total(after) - base_total, fa, _delta(after, base)))
    adds.sort(key=lambda a: -a[0])

    # A week he improves is a week he was seated, so the count of weeks he
    # would actually start falls straight out of the solo pass. Read off here
    # rather than from a pair, where the drop muddies the same signal.
    starts = {
        fa.player_id: sum(1 for v in by_week.values() if v > 0.01)
        for _gain, fa, by_week in adds
    }

    if spare_spots > 0:
        # A free spot means no drop to pay for, so the first pass is the answer.
        return [
            Move(add=fa, drop=None, gain=gain, by_week=by_week,
                 starts=starts.get(fa.player_id, 0), first_week=first_week)
            for gain, fa, by_week in adds[:limit]
            if gain > 0
        ]

    drops: list[tuple[float, Player]] = []
    for held in roster:
        if droppable is not None and held.player_id not in droppable:
            continue
        rest = [p for p in roster if p.player_id != held.player_id]
        drops.append((base_total - _total(horizon_value(evaluator, horizon, rest)), held))
    drops.sort(key=lambda d: d[0])

    moves: list[Move] = []
    for _gain, fa, _by_week in adds[:ADD_SHORTLIST]:
        for _cost, held in drops[:DROP_SHORTLIST]:
            swapped = [p for p in roster if p.player_id != held.player_id] + [fa]
            after = horizon_value(evaluator, horizon, swapped)
            moves.append(
                Move(add=fa, drop=held, gain=_total(after) - base_total,
                     by_week=_delta(after, base),
                     starts=starts.get(fa.player_id, 0), first_week=first_week)
            )
    moves.sort(key=lambda m: -m.gain)

    # One row per signing: the same player paired with six different drops is
    # one decision, and only his best pairing is worth printing. Deferred
    # streamers collapse further, to one per position - being interchangeable
    # is what made them streamers, so listing eight kickers for the same bye
    # says nothing the first one did not.
    seen: set[str] = set()
    streaming: set[str] = set()
    best: list[Move] = []
    for move in moves:
        pid = move.add.player_id if move.add else ""
        if pid in seen or move.gain <= 0:
            continue
        if move.can_wait and move.add is not None:
            if move.add.position in streaming:
                continue
            streaming.add(move.add.position)
        seen.add(pid)
        best.append(move)
        if len(best) >= limit:
            break
    return best
