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

An empty starter slot in the **imminent week** scores zero, not replacement
level, which is where this model parts company with the draft engine. The
draft engine floors every slot at the waiver wire because it knows you will
stream someone eventually; for this week that is exactly what is being decided
now, so assuming it would score every pickup at nothing.

In the **weeks after it** every slot is floored at what the wire will still
offer: a share of the third-best free agent at the position that week - see
`stream_floors`. You do not have to hold a backup kicker through November to
cover one bye; you can sign one that week. Without the floor the model
charged the whole of a backup's points for dropping him, and credited the
whole of a fill-in's points for signing him weeks early. With it, each is
worth only his margin over the player you could pick up when the week comes.

Weekly projections are used unshrunk. `projections.SHRINKAGE` was fitted on
season-long projections against season-long results. The same measurement for
single weeks (`tests/calibrate_weekly.py`, 2024 and 2025) puts the slope of
actual on projected points between 0.82 and 1.06 at every position, kickers
and defences included: a weekly projection's spread is small, and it is real.

Expert consensus is blended in, though, because that is a second opinion
rather than a transform. FantasyPros publishes a weekly ranking for the next
week only, so the imminent week takes that one and the weeks after it take the
rest-of-season ranking, blended against each player's remaining total - see
`load_horizon`.

**Depth is priced by simulation.** Taken at face value, today's projections
say every starter plays every remaining week at his current number. In that
world a backup is seated only on byes, and dropping him costs almost nothing.
The real season is not like that: starters get hurt, and roles change. So the
weeks after the imminent one are played out `SIMS` times. In each run a player
can get hurt and miss weeks, part of an injured player's work passes to his
teammates at the same position, and every projection moves: part of the move
stays for the rest of the season and part belongs to that week alone. The
lineup is seated each week on what you would know by then. A backup earns what
he scores in the runs where he is needed. That is the cost of dropping him, and
a signing has to beat it. Every roster is scored on the same runs, so the gap
between two rosters carries far less noise than either total.

**A move has a date as well as a value.** Signing a player in week k changes
only weeks k onward, so the value of every possible signing date falls out of
the week-by-week gain. When the drop is still earning early weeks for you,
signing later is worth more. Whether to wait depends on whether the player
will still be there. If the wire holds other players about as good, waiting
costs nothing. If he is the only one, waiting risks losing him, so sign now if
the move pays even with the early weeks it gives up.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from .fantasypros import Consensus, load_ros_consensus, load_weekly_consensus
from .league import LeagueSettings
from .projections import (
    ROS_CONSENSUS_WEIGHT,
    WEEKLY_CONSENSUS_WEIGHT,
    Player,
    blend_ranks,
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

# Points a later signing date has to be worth before it is mentioned as the
# cost of signing now. Below this the gap is inside the simulation's noise.
TIMING_SLACK = 1.0

# What the wire is worth in a later week. Judgement calls.
#
# A slot you cannot fill from your roster is filled from the wire, by the
# STREAM_RANK-th best free agent at the position that week, at STREAM_SHARE of
# his projection. Third, not first: by the time the week comes the best one or
# two are often gone. The share pays for what streaming costs and the model
# cannot see: the player you cut to make room, the waiver claim that fails.
# Measured on today's wire, which is a low estimate of a later one - players
# break out, and some of them will still be free.
STREAM_RANK = 3
STREAM_SHARE = 0.85

# How many candidates survive the cheap first pass into the exact pairwise one.
# The shortlists only need to be wide enough to contain the real answer; every
# pair beyond that is arithmetic nobody reads.
ADD_SHORTLIST = 25
DROP_SHORTLIST = 6

# Simulated seasons behind every value. Rosters share the same runs, so a
# difference between two rosters settles long before either total does. At
# 1000 the same league gives the same advice from seed to seed; at 200 it did
# not. The first pass over the whole wire, which only has to find the
# shortlist, uses the first SCREEN_SIMS.
SIMS = 1000
SCREEN_SIMS = 200

# The injury model. Fitted to 2024 and 2025 by `tests/calibrate_weekly.py`,
# over players projected to matter in the week before.
#
# INJURY_HAZARD is the chance a player who played misses his team's next game.
# INJURY_PERSISTENCE is the chance a player who missed misses again, so the
# average absence is 1 / (1 - persistence): five weeks for a running back.
#
#   pos   hazard  persistence  weeks out, once it settles
#   QB    0.049   0.75         16%
#   RB    0.040   0.80         17%
#   WR    0.048   0.76         17%
#   TE    0.043   0.72         13%
#   K     0.037   0.89         24%
#
# These count every missed game, whatever the reason: an injury, a benching, a
# kicker who is cut. A kicker rarely gets hurt but often loses the job, and
# then he does not come back. Defences do not miss games. A player who is
# healthy today starts healthy, so over an eight-week horizon he misses fewer
# weeks than the settled share.
INJURY_HAZARD = {"QB": 0.049, "RB": 0.040, "WR": 0.048, "TE": 0.043, "K": 0.037}
INJURY_PERSISTENCE = {"QB": 0.75, "RB": 0.80, "WR": 0.76, "TE": 0.72, "K": 0.89}

# How a designation carried today plays out in later weeks. Judgement calls.
#
# `valuation.WEEKLY_STATUS_DISCOUNT` gives the chance a designated player
# misses the imminent game. STATUS_LINGER is the chance that, having missed a
# game, he misses the next one too, and it only applies to weeks Sleeper still
# projects him to play: Sleeper already leaves out the weeks a player is
# expected to miss. So this prices the return that slips, not the absence
# everyone knows about. A player ruled Out misses the following week half the
# time; one on injured reserve, who has to sit at least four games and often
# more, 85% of the time.
STATUS_LINGER = {"IR": 0.85, "PUP": 0.85, "NA": 0.8, "Sus": 0.8}
DEFAULT_LINGER = 0.5

# How far the rest-of-season blend may move a player's weeks, up or down. The
# blend sets his total and Sleeper's weeks carry it, so the experts may double
# Sleeper's number or halve it, and no more.
#
# The move is also scaled by how much of the remaining schedule Sleeper
# expects him to play - see `load_horizon`. A total says less about a weekly
# level the fewer weeks it covers. When Sleeper projects a fill-in quarterback
# for three starts and the experts rank him as if he gets six, the difference
# is starts, not points per start, and loading it onto the three starts made
# him a 25-point quarterback.
ROS_FACTOR_LIMIT = 2.0

# Share of an injured player's projection that passes to healthy teammates at
# his position, split in proportion to their own projections. This is what
# makes a handcuff worth holding. Fitted alongside the hazard: the median rise
# in his position-mates' combined projection the week he first misses, as a
# share of his own the week before.
INJURY_TRANSFER = {"QB": 0.81, "RB": 0.85, "WR": 0.62, "TE": 0.49}

# How far a player's projection wanders, on a log scale. Fitted on the same
# two seasons, as the spread between the projection for one week and the
# projection for a week k later, for players with the same relevant
# position-mates in both. The variance splits into two parts.
#
# PROJECTION_DRIFT is the part that stays: a job won or lost. It grows with
# the square root of the weeks ahead, and a player's direction is drawn once
# per run and kept. After eight weeks a running back's projection is typically
# about a third higher or lower than today's.
#
# PROJECTION_NOISE is the part that belongs to one week alone: a matchup, a
# game plan. It is drawn fresh each week. It is why a bench player close to
# your starter is worth something even if nobody gets hurt: some weeks he is
# the better play, and you will know it by then.
#
# A defence is nearly all noise and a quarterback is mostly drift. Sleeper's
# number for a later week may already hold some of that week's matchup, so the
# noise is, if anything, slightly overstated.
PROJECTION_DRIFT = {"QB": 0.057, "RB": 0.110, "WR": 0.062, "TE": 0.081, "K": 0.014, "DEF": 0.008}
PROJECTION_NOISE = {"QB": 0.053, "RB": 0.077, "WR": 0.101, "TE": 0.128, "K": 0.047, "DEF": 0.107}


@dataclass
class Horizon:
    """Every week left, and what each player is projected for in it."""

    weeks: list[int]
    # week -> player_id -> expected points: the projection times the chance he
    # plays, given today's injury designation.
    points: dict[int, dict[str, float]]
    # player_id -> (position, team). The simulation needs it to find who takes
    # over an injured player's work; a horizon without it simulates nobody.
    info: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    # Weeks whose projections carry no expert consensus, although it was asked
    # for: the FantasyPros ranking they needed failed or came back empty.
    sleeper_only: list[int] = field(default_factory=list)
    # week -> player_id -> points if he plays. The simulation starts from
    # these and decides for itself who is out. Empty means `points` already is
    # that number.
    healthy: dict[int, dict[str, float]] = field(default_factory=dict)
    # player_id -> today's injury designation, for players who carry one.
    status: dict[str, str] = field(default_factory=dict)
    # Everyone with a projection in any week left, by player_id.
    players: dict[str, Player] = field(default_factory=dict)
    # player_id -> FantasyPros positional rank, this week and rest of season.
    week_rank: dict[str, int] = field(default_factory=dict)
    ros_rank: dict[str, int] = field(default_factory=dict)
    # player_id -> (average expert ROS rank, how far the experts are apart).
    ros_ecr: dict[str, tuple[float, float]] = field(default_factory=dict)

    def week_points(self, week: int, player: Player) -> float:
        return self.points.get(week, {}).get(player.player_id, 0.0)

    def ranks(self, player: Player) -> str:
        """His FantasyPros ranks, e.g. "wk TE12 · ROS TE15". Empty if unranked."""
        parts = []
        if player.player_id in self.week_rank:
            parts.append(f"wk {player.position}{self.week_rank[player.player_id]}")
        if player.player_id in self.ros_rank:
            parts.append(f"ROS {player.position}{self.ros_rank[player.player_id]}")
        return " · ".join(parts)


@dataclass
class Move:
    """A signing, the drop it costs you, and when to make it."""

    add: Player | None
    drop: Player | None
    gain: float                              # net rest-of-season points, signing now
    by_week: dict[int, float] = field(default_factory=dict)
    add_value: float = 0.0                   # what he adds if nobody had to go
    starts: float = 0.0                      # weeks left he would start, on average
    first_week: int = 0                      # the week being decided now
    replaceable: bool = False                # the wire holds others about as good

    @property
    def drop_cost(self) -> float:
        """What the dropped player would have scored for you, given the signing.

        His byes, his injury cover and his chance of winning a bigger role. It
        is taken with the new player already on the roster, since the new
        player covers some of the same weeks.
        """
        return self.add_value - self.gain if self.drop else 0.0

    @property
    def sign_week(self) -> int:
        """The signing date worth the most. The latest, if several tie."""
        return self._best_date()[0]

    @property
    def sign_value(self) -> float:
        """Net points if you sign him on `sign_week`."""
        return self._best_date()[1]

    def _best_date(self) -> tuple[int, float]:
        # Signing in week k leaves every week before k exactly as it is, so
        # the move is worth the sum of its gains from week k on. A tie goes to
        # the later date: a roster spot held early for nothing is a spot you
        # cannot use for anyone else.
        weeks = sorted(self.by_week)
        if not weeks:
            return self.first_week, self.gain
        value = {weeks[0]: self.gain}
        for prev, wk in zip(weeks, weeks[1:]):
            value[wk] = value[prev] - self.by_week[prev]
        best = max(value.values())
        return max(wk for wk, v in value.items() if v >= best - 1e-9), best

    @property
    def act_now(self) -> bool:
        """Sign him today: he raises your season total and today is the time.

        Waiting is right only when the wire will still offer someone like him
        later, and a later date is worth at least as much. A player with no
        equal on the wire is signed as soon as the move pays, even if a later
        date would pay more, because waiting is how you lose him.
        """
        if self.gain < WORTH_DOING:
            return False
        if not self.replaceable:
            return True
        return self.sign_week == self.first_week

    @property
    def worth_doing(self) -> bool:
        return max(self.gain, self.sign_value) >= WORTH_DOING

    def reason(self) -> str:
        wait = self.sign_value - self.gain
        if self.act_now:
            if wait < TIMING_SLACK:
                return "sign now"
            return (f"sign now — week {self.sign_week} is worth {wait:+.1f} more, "
                    f"but nobody like him will be left")
        if self.worth_doing:
            return f"wait until week {self.sign_week} ({self.sign_value:+.1f} then)"
        return "not worth a move"


def _no_discount(_status: str | None) -> float:
    return 1.0


def availability(status: str | None, weeks_ahead: int) -> float:
    """Chance a player with this designation plays, `weeks_ahead` from now."""
    out_now = 1.0 - weekly_injury_multiplier(status)
    if weeks_ahead <= 0:
        return 1.0 - out_now
    return 1.0 - out_now * STATUS_LINGER.get(status or "", DEFAULT_LINGER) ** weeks_ahead


def _expert_ranks(players: list[Player], rows: list[Consensus]) -> dict[str, int]:
    """player_id -> positional rank on the experts' own list.

    Counted over every row of the ranking, not only the rows that matched a
    player here, so "TE15" means what it means on the FantasyPros page.
    """
    listed: dict[str, list[float]] = {}
    for r in rows:
        listed.setdefault(r.position, []).append(r.ecr)
    for ecrs in listed.values():
        ecrs.sort()
    return {
        p.player_id: bisect_left(listed[p.position], p.ecr) + 1
        for p in players
        if p.ecr is not None and p.position in listed
    }


def load_horizon(
    client: SleeperClient,
    league: LeagueSettings,
    season: str,
    weeks: list[int],
    workers: int = 6,
    consensus: bool = True,
) -> Horizon:
    """Fetch every remaining week's projections, expert consensus included.

    Each week starts as Sleeper's projection for that week. Two FantasyPros
    rankings are then blended in, each against the thing it ranks.

    **The imminent week** takes the weekly ranking, the way `lineup` does, and
    then the hard weekly injury discount: Out means he does not play.

    **The weeks after it** take the rest-of-season ranking. A rest-of-season
    rank is a claim about a total, so it is blended against a total. Every
    player's remaining weeks are summed, each weighted by the chance he plays
    it. The position is re-seated on that curve of totals by a blend of
    Sleeper's order and the experts'. Each of his weeks is then scaled by the
    same factor, so his total matches the blended one. Sleeper keeps the
    shape: byes, matchups and expected return dates. The experts move the
    level, within `ROS_FACTOR_LIMIT` and in proportion to how much of the
    remaining schedule he is expected to play. The total always spans the rest of the season, whatever `weeks`
    asks for, because that is what the experts ranked.

    Blending week by week instead would count an absence twice: the experts
    rank a player with a bye still ahead below an equal player without one,
    and the week-by-week seating then leaves him out of that week as well.

    Fetched in parallel because this is 6 positions per week and the weeks do
    not depend on each other. Each (week, position) is a distinct cache entry,
    so the threads never write the same file. The two FantasyPros rankings are
    fetched once, up front and in series, because they share a crawl delay.
    """
    if not weeks:
        return Horizon(weeks=[], points={})
    imminent = weeks[0]

    weekly_rows: list = []
    ros_rows: list = []
    if consensus:
        # A failed or blocked ranking must not take the whole waiver run with
        # it; without it the horizon is Sleeper-only for the weeks it covers.
        try:
            weekly_rows = load_weekly_consensus(league.scoring_format, client.cache_dir)
        except Exception:
            pass
        try:
            ros_rows = load_ros_consensus(league.scoring_format, client.cache_dir)
        except Exception:
            pass

    season_weeks = sorted(set(remaining_weeks(imminent)) | set(weeks))

    def one(week: int) -> tuple[int, list[Player]]:
        # A projection for the week being played moves with every practice
        # report; one for week 14 does not.
        return week, load_week_points(
            client, league, season, week,
            status_discount=_no_discount,
            ttl=3600 if week == imminent else 86400,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        raw = dict(pool.map(one, season_weeks))
    near = load_week_points(client, league, season, imminent,
                            consensus=weekly_rows or None, ttl=3600)

    players: dict[str, Player] = {p.player_id: p for p in near}
    for week in season_weeks:
        for p in raw[week]:
            players.setdefault(p.player_id, p)
    status = {pid: p.injury_status for pid, p in players.items() if p.injury_status}
    ahead = {week: week - imminent for week in season_weeks}

    def plays(pid: str, week: int) -> float:
        return availability(status.get(pid), ahead[week])

    # Sleeper's own expected total for the rest of the season.
    total: dict[str, float] = {}
    for week in season_weeks:
        for p in raw[week]:
            total[p.player_id] = total.get(p.player_id, 0.0) + p.points * plays(p.player_id, week)
    seats = [replace(players[pid], points=pts, ecr=None, rank_std=0.0)
             for pid, pts in total.items() if pts > 0]
    if ros_rows:
        blend_ranks(seats, ros_rows, ROS_CONSENSUS_WEIGHT, unranked_last=True)
    # Expected games, as a share of the games his team has left.
    games: dict[str, float] = {}
    team_weeks: dict[str | None, set[int]] = {}
    for week in season_weeks:
        for p in raw[week]:
            games[p.player_id] = games.get(p.player_id, 0.0) + plays(p.player_id, week)
            team_weeks.setdefault(p.team, set()).add(week)
    factor: dict[str, float] = {}
    for p in seats:
        moved = min(ROS_FACTOR_LIMIT, max(1.0 / ROS_FACTOR_LIMIT, p.points / total[p.player_id]))
        left = len(team_weeks.get(p.team) or season_weeks) if p.team else len(season_weeks)
        share = min(1.0, games.get(p.player_id, 0.0) / max(1, left))
        factor[p.player_id] = 1.0 + (moved - 1.0) * share

    healthy: dict[int, dict[str, float]] = {}
    points: dict[int, dict[str, float]] = {}
    for week in weeks:
        if week == imminent:
            points[week] = {p.player_id: p.points for p in near}
            continue
        healthy[week] = {p.player_id: p.points * factor.get(p.player_id, 1.0) for p in raw[week]}
        points[week] = {pid: pts * plays(pid, week) for pid, pts in healthy[week].items()}

    sleeper_only = [
        wk for wk in weeks
        if consensus and not (weekly_rows if wk == imminent else ros_rows)
    ]
    return Horizon(
        weeks=list(weeks),
        points=points,
        info={pid: (p.position, p.team) for pid, p in players.items()},
        sleeper_only=sleeper_only,
        healthy=healthy,
        status=status,
        players=players,
        week_rank=_expert_ranks(near, weekly_rows),
        ros_rank=_expert_ranks(seats, ros_rows),
        ros_ecr={p.player_id: (p.ecr, p.rank_std) for p in seats if p.ecr is not None},
    )


def remaining_weeks(current_week: int, limit: int | None = None) -> list[int]:
    weeks = list(range(max(1, current_week), SEASON_END_WEEK + 1))
    return weeks[:limit] if limit else weeks


@dataclass
class Runs:
    """Simulated seasons, stored compactly: run -> week -> one number per player."""

    weeks: list[int]
    index: dict[str, int]                    # player_id -> column
    points: list[list[list[float]]]

    @classmethod
    def single(cls, horizon: Horizon, player_ids: set[str]) -> "Runs":
        """The horizon at face value: one run, nobody hurt, nothing drifts."""
        index = {pid: i for i, pid in enumerate(sorted(player_ids))}
        rows = [[horizon.points.get(wk, {}).get(pid, 0.0) for pid in index]
                for wk in horizon.weeks]
        return cls(list(horizon.weeks), index, [rows])


def _geometric(rng: random.Random, p: float) -> int:
    """Weeks that pass before an event with weekly chance `p` happens."""
    if p >= 1.0:
        return 0
    return int(math.log(1.0 - rng.random()) / math.log(1.0 - p))


def simulate(
    horizon: Horizon, player_ids: set[str], sims: int = SIMS, seed: int = 0
) -> Runs:
    """Play the weeks after the imminent one out `sims` times.

    Each run holds the projection you would see in each week by the time you
    set that week's lineup. An injured player scores zero, and part of his
    projection passes to his healthy teammates at the same position. The
    imminent week is left as it is: its projections already carry this week's
    practice reports. A player who carries an injury designation today starts
    each run hurt as often as that designation says, and stays out for as long
    as `STATUS_LINGER` gives him.

    Each player draws from a random stream of his own, seeded by his id. Any
    two rosters therefore meet the same injuries in the same runs, which is
    what lets a move be measured as a difference between them.

    Only `player_ids` come back. Their teammates are simulated as well,
    because an injury to a teammate is how a backup gets his chance.
    """
    if sims <= 0 or len(horizon.weeks) < 2:
        return Runs.single(horizon, player_ids)
    imminent, later = horizon.weeks[0], horizon.weeks[1:]

    units: dict[tuple[str | None, str], list[str]] = {}
    for pid, (pos, team) in horizon.info.items():
        if team:
            units.setdefault((team, pos), []).append(pid)
    universe = set(player_ids)
    for pid in player_ids:
        pos, team = horizon.info.get(pid, ("", None))
        if team:
            universe.update(units.get((team, pos), ()))

    order = sorted(universe)
    col = {pid: i for i, pid in enumerate(order)}
    keep = sorted(player_ids)
    keep_cols = [col[pid] for pid in keep]
    base = [[horizon.healthy.get(wk, horizon.points.get(wk, {})).get(pid) for pid in order]
            for wk in later]
    # Chance each player misses the imminent game, and how sticky that is.
    out_now = [1.0 - weekly_injury_multiplier(horizon.status.get(pid)) for pid in order]
    linger = [STATUS_LINGER.get(horizon.status.get(pid, ""), DEFAULT_LINGER) for pid in order]
    info = [horizon.info.get(pid, ("", None)) for pid in order]
    mates = [[col[m] for m in units.get((team, pos), ()) if m != pid] if team else []
             for pid, (pos, team) in zip(order, info)]
    rngs = [random.Random(f"{seed}:{pid}") for pid in order]
    roots = [math.sqrt(n) for n in range(1, len(later) + 1)]
    first_row = [horizon.points.get(imminent, {}).get(pid, 0.0) for pid in keep]

    runs: list[list[list[float]]] = []
    for _run in range(sims):
        rows = [[0.0] * len(order) for _ in later]
        hurt: list[list[int]] = [[] for _ in later]
        for c, (pos, _team) in enumerate(info):
            rng = rngs[c]
            drift = PROJECTION_DRIFT.get(pos, 0.0)
            noise = PROJECTION_NOISE.get(pos, 0.0)
            hazard = INJURY_HAZARD.get(pos, 0.0)
            persistence = INJURY_PERSISTENCE.get(pos, 0.0)
            z = rng.gauss(0.0, 1.0) if drift else 0.0
            out = [False] * len(later)
            week = 0
            if out_now[c] and rng.random() < out_now[c]:
                # Hurt today. He stays out until the absence ends, and only
                # then is he exposed to a new injury.
                week = _geometric(rng, 1.0 - linger[c])
                for k in range(min(len(later), week)):
                    out[k] = True
            if hazard:
                week += _geometric(rng, hazard)
                while week < len(later):
                    length = 1 + _geometric(rng, 1.0 - persistence)
                    for k in range(week, min(len(later), week + length)):
                        out[k] = True
                    week += length + _geometric(rng, hazard)
            for k, root in enumerate(roots):
                b = base[k][c]
                if b is None:
                    continue                  # a bye: no game to miss
                e = rng.gauss(0.0, noise) if noise else 0.0
                # Centred so the average run keeps today's projection.
                spread = drift * drift * root * root + noise * noise
                rows[k][c] = b * math.exp(drift * root * z + e - 0.5 * spread)
                if out[k]:
                    hurt[k].append(c)

        for row, out in zip(rows, hurt):
            if not out:
                continue
            down = set(out)
            extra = [0.0] * len(order)
            for c in out:
                share = INJURY_TRANSFER.get(info[c][0], 0.0) * row[c]
                healthy = [m for m in mates[c] if m not in down and row[m] > 0]
                pool = sum(row[m] for m in healthy)
                for m in healthy:
                    extra[m] += share * row[m] / pool
            for c in out:
                row[c] = 0.0
            for m, pts in enumerate(extra):
                if pts:
                    row[m] += pts

        runs.append([first_row] + [[row[c] for c in keep_cols] for row in rows])
    return Runs(list(horizon.weeks), {pid: i for i, pid in enumerate(keep)}, runs)


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


def stream_floors(
    horizon: Horizon, free_agents: list[Player]
) -> dict[int, dict[str, float]]:
    """week -> position -> what a slot is worth filled from the wire that week.

    Every week after the imminent one. The imminent week has no floor: whether
    to sign somebody for it is the decision being made. `free_agents` is the
    whole wire, not a shortlist and not one position.
    """
    floors: dict[int, dict[str, float]] = {}
    for week in horizon.weeks[1:]:
        by_pos: dict[str, list[float]] = {}
        for fa in free_agents:
            pts = horizon.week_points(week, fa)
            if pts > 0:
                by_pos.setdefault(fa.position, []).append(pts)
        floors[week] = {
            pos: STREAM_SHARE * sorted(pts, reverse=True)[min(STREAM_RANK, len(pts)) - 1]
            for pos, pts in by_pos.items()
        }
    return floors


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


def _neg_points(entry: tuple) -> float:
    return -entry[0]


class _Lineups:
    """Your current roster seated in every week of every run, kept for reuse.

    A move changes a week only if the new player beats someone in that week's
    lineup, or the dropped player was in it. Most weeks neither happens, so
    the answer is read off without seating anyone. A cell's thresholds make
    that test safe: a player who scores no more than every starter he could
    displace, directly or by a flex slot shuffling, cannot improve the lineup.
    """

    def __init__(
        self,
        evaluator: LineupEvaluator,
        runs: Runs,
        roster: list[Player],
        floors: dict[int, dict[str, float]] | None = None,
        weights: dict[int, float] | None = None,
    ):
        self.evaluator = evaluator
        self.runs = runs
        self.n_weeks = len(runs.weeks)
        # What a point is worth in each week - see `standings`. Every change
        # this class reports is already multiplied by it.
        self.weights = [(weights or {}).get(wk, 1.0) for wk in runs.weeks]
        # One evaluator per week: each week has its own floor under every
        # slot, and the imminent week has none.
        self.evaluators = [
            replace(evaluator, replacement=(floors or {}).get(wk, {})) for wk in runs.weeks
        ]

        # Positions linked by a flex slot, directly or through a chain of them,
        # and the starter slots only that group can fill.
        related: dict[str, frozenset[str]] = {pos: frozenset([pos]) for pos in evaluator.dedicated}
        for _slot, _n, eligible in evaluator.flex:
            group = set(eligible)
            for pos in eligible:
                group |= related.get(pos, {pos})
            for pos in group:
                related[pos] = frozenset(group)
        self.groups = set(related.values())
        self.related = related
        self.slots = {
            group: sum(evaluator.dedicated.get(pos, 0) for pos in group)
            + sum(n for _s, n, eligible in evaluator.flex if set(eligible) <= group)
            for group in self.groups
        }
        # Released players' lineups, for the drops a pair may be built on.
        self._without: dict[str, dict[int, tuple]] = {}

        self.cells: list[tuple] = []
        cols = [(p.position, p.player_id, runs.index.get(p.player_id)) for p in roster]
        for run in runs.points:
            for row in run:
                by_pos: dict[str, list[tuple[float, float, str]]] = {}
                for pos, pid, c in cols:
                    v = row[c] if c is not None else 0.0
                    if v > 0:
                        by_pos.setdefault(pos, []).append((v, v, pid))
                for lst in by_pos.values():
                    lst.sort(key=_neg_points)
                wk = len(self.cells) % self.n_weeks
                self.cells.append(self._cell(by_pos, self.evaluators[wk], self.weights[wk]))

    def _cell(self, by_pos: dict, evaluator: LineupEvaluator, weight: float) -> tuple:
        """(pools, lineup total, who starts, a newcomer's bar, evaluator, week weight).

        The bar ignores the week's floor. A newcomer who clears it is seated
        again and scored exactly, floor included; one who does not would not
        have been seated, floor or no floor.
        """
        total, used = evaluator.seat(by_pos)
        seated: set[str] = set()
        lowest: dict[str, float] = {}
        for pos, lst in by_pos.items():
            k = used.get(pos, 0)
            seated.update(e[2] for e in lst[:k])
            if k:
                lowest[pos] = lst[k - 1][0]
        bar: dict[frozenset, float] = {}
        for group in self.groups:
            filled = sum(used.get(pos, 0) for pos in group)
            # An open slot the group can fill takes anyone who scores at all.
            bar[group] = (min((lowest[q] for q in group if q in lowest), default=0.0)
                          if filled >= self.slots[group] else 0.0)
        thresholds = {pos: bar[group] for pos, group in self.related.items()}
        return by_pos, total, seated, thresholds, evaluator, weight

    def _pools(self, by_pos: dict, drop: Player | None, add: tuple[str, float, str] | None) -> dict:
        pools = dict(by_pos)
        if drop is not None:
            pools[drop.position] = [e for e in pools.get(drop.position, ()) if e[2] != drop.player_id]
        if add is not None and add[1] > 0:
            pos, v, pid = add
            lst = list(pools.get(pos, ()))
            lst.append((v, v, pid))
            lst.sort(key=_neg_points)
            pools[pos] = lst
        return pools

    def _reseat(self, cell, drop: Player | None, add: tuple[str, float, str] | None) -> float:
        return (cell[4].seat(self._pools(cell[0], drop, add))[0] - cell[1]) * cell[5]

    def _points(self, player: Player) -> list[float]:
        """His points in every cell, in cell order."""
        c = self.runs.index.get(player.player_id)
        if c is None:
            return [0.0] * len(self.cells)
        return [row[c] for run in self.runs.points for row in run]

    def _gain(self, cell, player: Player, v: float) -> float:
        if v <= cell[3].get(player.position, 0.0):
            return 0.0
        return self._reseat(cell, None, (player.position, v, player.player_id))

    def add(self, player: Player, n_runs: int | None = None) -> list[float]:
        """What signing him changes in every cell, with a spare roster spot.

        `n_runs` stops after that many runs, for a quick first look.
        """
        cells = self.cells if n_runs is None else self.cells[: n_runs * self.n_weeks]
        return [self._gain(cell, player, v) for cell, v in zip(cells, self._points(player))]

    def drop(self, player: Player, keep: bool = False) -> list[float]:
        """What releasing him changes in every cell. Zero or negative.

        `keep` stores the lineups without him, so a pair built on this drop
        can use them.
        """
        out = []
        without: dict[int, tuple] = {}
        for i, cell in enumerate(self.cells):
            if player.player_id not in cell[2]:
                out.append(0.0)
                continue
            if not keep:
                out.append(self._reseat(cell, player, None))
                continue
            reduced = self._cell(self._pools(cell[0], player, None), cell[4], cell[5])
            out.append((reduced[1] - cell[1]) * cell[5])
            without[i] = reduced
        if keep:
            self._without[player.player_id] = without
        return out

    def swap(self, add: Player, add_cells: list[float], drop: Player) -> list[float]:
        """Signing `add` and releasing `drop` together. Needs `drop(drop, keep=True)`.

        Where the dropped player was not starting, the pair changes exactly
        what the signing alone does: adding a player never promotes a benched
        one. Where he was, the signing is measured against the lineup without
        him.
        """
        points = self._points(add)
        out = list(add_cells)
        for i, reduced in self._without[drop.player_id].items():
            lost = (reduced[1] - self.cells[i][1]) * reduced[5]
            out[i] = lost + self._gain(reduced, add, points[i])
        return out

    def by_week(self, cells: list[float]) -> dict[int, float]:
        """Average change per week across the runs."""
        n = len(self.runs.points)
        return {wk: sum(cells[i :: self.n_weeks]) / n for i, wk in enumerate(self.runs.weeks)}


def best_moves(
    league: LeagueSettings,
    horizon: Horizon,
    roster: list[Player],
    free_agents: list[Player],
    spare_spots: int = 0,
    limit: int = 10,
    droppable: set[str] | None = None,
    runs: Runs | None = None,
    floors: dict[int, dict[str, float]] | None = None,
    weights: dict[int, float] | None = None,
) -> list[Move]:
    """Rank signings by what they add to your rest-of-season total, net of the drop.

    Every free agent is scored as a straight addition first. The best
    `ADD_SHORTLIST` are then paired with each of the `DROP_SHORTLIST` cheapest
    drops, and each signing keeps his best pairing. An add and the drop that
    pays for it interact: dropping your third receiver is free until the
    receiver you signed is the one covering that bye.

    `runs` are the simulated seasons from `simulate`. Without them the horizon
    is taken at face value, as a single run with no injuries. The first pass
    looks at the first `SCREEN_SIMS` runs only.

    `droppable` limits who can pay for a signing - see `droppable_ids`. None
    means anyone on the roster.

    `floors` are the wire's worth in each later week, from `stream_floors`.
    Without them an unfilled slot scores zero in every week.

    `weights` are what a point is worth in each week, from
    `standings.week_weights`. Every value a `Move` carries is weighted by them.
    Without them every week counts the same.
    """
    evaluator = LineupEvaluator.from_league(league, {})
    if runs is None:
        runs = Runs.single(horizon, {p.player_id for p in roster + free_agents})
    first_week = horizon.weeks[0] if horizon.weeks else 0
    lineups = _Lineups(evaluator, runs, roster, floors, weights)
    n_runs = len(runs.points)

    screened = [(sum(lineups.add(fa, SCREEN_SIMS)), fa) for fa in free_agents]
    screened.sort(key=lambda e: -e[0])
    shortlist = [(fa, lineups.add(fa)) for gain, fa in screened[:ADD_SHORTLIST] if gain > 0]
    shortlist = [(fa, cells) for fa, cells in shortlist if sum(cells) > 0]

    def starts(cells: list[float]) -> float:
        # A week he improves is a week he was seated.
        return sum(1 for v in cells if v > 0.01) / n_runs

    moves: list[Move] = []
    if spare_spots > 0:
        # A free spot means no drop to pay for, so the solo pass is the answer.
        for fa, cells in shortlist:
            by_week = lineups.by_week(cells)
            gain = _total(by_week)
            moves.append(Move(add=fa, drop=None, gain=gain, by_week=by_week,
                              add_value=gain, starts=starts(cells), first_week=first_week))
    else:
        drops = [
            (-sum(lineups.drop(held)) / n_runs, held)
            for held in roster
            if droppable is None or held.player_id in droppable
        ]
        drops.sort(key=lambda d: d[0])
        drops = drops[:DROP_SHORTLIST]
        for _cost, held in drops:
            lineups.drop(held, keep=True)
        for fa, cells in shortlist:
            add_value = sum(cells) / n_runs
            best: Move | None = None
            for _cost, held in drops:
                by_week = lineups.by_week(lineups.swap(fa, cells, held))
                move = Move(add=fa, drop=held, gain=_total(by_week), by_week=by_week,
                            add_value=add_value, starts=starts(cells), first_week=first_week)
                if best is None or move.gain > best.gain:
                    best = move
            if best is not None:
                moves.append(best)

    # Replaceable: somebody else at his position is worth about as much on his
    # own best date. Judged on today's wire, which is the best guess at what
    # the wire will hold by then.
    for move in moves:
        rivals = [m.sign_value for m in moves
                  if m.add.position == move.add.position and m is not move]
        move.replaceable = any(v >= move.sign_value - WORTH_DOING for v in rivals)

    moves = [m for m in moves if m.worth_doing]
    moves.sort(key=lambda m: (not m.act_now, -(m.gain if m.act_now else m.sign_value)))

    # Players worth waiting for collapse to one per position: being
    # interchangeable is what made them worth waiting for, so listing eight
    # kickers for the same bye says nothing the first one did not.
    waiting: set[str] = set()
    best_list: list[Move] = []
    for move in moves:
        if not move.act_now:
            if move.add.position in waiting:
                continue
            waiting.add(move.add.position)
        best_list.append(move)
        if len(best_list) >= limit:
            break
    return best_list
