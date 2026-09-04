"""Season projections scored with the league's own scoring settings."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .league import LeagueSettings
from .sleeper import SleeperClient
from .fantasypros import Consensus
from .outcomes import base_spread, expert_spread, percentile, total_spread
from .valuation import injury_multiplier, weekly_injury_multiplier

# Stat keys that are metadata, not scoreable production.
NON_SCORING_PREFIXES = ("adp_", "pts_")
NON_SCORING_KEYS = {"gp", "gms_active", "cmp_pct"}

MISSING_ADP = 999.0

# How much of a projection's distance from replacement to believe.
#
# The right factor is the regression slope of actual points on projected points,
# not their correlation. Correlation answers "does this rank players correctly";
# the slope answers "is the spread real", which is what a value model needs.
# Using correlation here shrank quarterbacks by half and made the engine skip
# them entirely in superflex.
#
# Measured over 2024 and 2025 for players with an ADP inside 250:
#
#   pos   corr        slope        verdict
#   RB    0.76 0.75   1.16  1.07   spread is real, believe it
#   TE    0.63 0.54   0.94  0.81   believe most of it
#   WR    0.74 0.59   0.95  0.77   believe most of it
#   QB    0.55 0.41   1.10  0.50   noisier, but the spread is real
#   DEF   0.05 0.36   0.21  1.27   correlation near zero; slope meaningless
#   K    -0.13 0.27  -0.67  2.19   correlation near zero; slope meaningless
#
# Kicker and defence slopes are unidentifiable: their projected spread is tiny
# (sd 5-7 points) while their actual spread is large (sd 20-56), so the ratio is
# noise, and the correlation says the ordering carries no information anyway.
# For those two the slope is ignored and the projections are shrunk hard, which
# is what stops the engine spending a sixth-round pick on a kicker who projects
# ten points clear of the field.
#
# RB is capped at 1.0 rather than its measured 1.11. Amplifying a projection on
# two seasons of evidence is not worth the risk.
SHRINKAGE = {
    "QB": 0.80,
    "RB": 1.00,
    "WR": 0.86,
    "TE": 0.87,
    "K": 0.15,
    "DEF": 0.35,
}
DEFAULT_SHRINKAGE = 0.85

# Positions Sleeper reports that no lineup slot accepts under their own name.
POSITION_ALIAS = {"FB": "RB", "HB": "RB", "OLB": "LB", "ILB": "LB", "CB": "DB", "S": "DB", "DE": "DL", "DT": "DL"}


@dataclass
class Player:
    player_id: str
    name: str
    position: str
    team: str | None
    points: float
    adp: float
    injury_status: str | None = None
    years_exp: int | None = None
    stats: dict[str, float] = field(default_factory=dict)
    positional_rank_std: float = 0.0

    pos_rank: int = 0
    vorp: float = 0.0

    # Consensus and risk. Expert fields fill in when FantasyPros data is loaded.
    ecr: float | None = None
    rank_std: float = 0.0
    bye: int | None = None
    shrunk: float | None = None               # projection pulled toward replacement
    spread: float = 0.0                       # log-scale of rank movement
    curve: list[float] = field(default_factory=list, repr=False)

    @property
    def floor(self) -> float:
        """A bad but not catastrophic season: the 10th percentile."""
        return percentile(self.curve, self.pos_rank, self.spread, 0.10, self.value)

    @property
    def median(self) -> float:
        return percentile(self.curve, self.pos_rank, self.spread, 0.50, self.value)

    @property
    def ceiling(self) -> float:
        """A season that wins your league: the 90th percentile."""
        return percentile(self.curve, self.pos_rank, self.spread, 0.90, self.value)

    @property
    def upside(self) -> float:
        """Points of headroom above the median. High for unsettled young players."""
        return self.ceiling - self.median

    @property
    def value(self) -> float:
        """The number the engine decides on.

        The raw projection, shrunk toward replacement by how predictive
        projections at this position have proven to be, then discounted for any
        injury designation. `points` stays untouched for display.
        """
        base = self.points if self.shrunk is None else self.shrunk
        return base * injury_multiplier(self.injury_status)

    @property
    def is_rookie(self) -> bool:
        return self.years_exp == 0

    @property
    def label(self) -> str:
        tag = ""
        if self.injury_status and self.injury_status not in ("Questionable",):
            tag = f" [{self.injury_status[:3].upper()}]"
        return f"{self.name}{tag}"

    @property
    def team_pos(self) -> str:
        return f"{self.position}{self.pos_rank} {self.team or 'FA'}"


def score_stats(stats: dict[str, float], scoring: dict[str, float]) -> float:
    """Apply league scoring to a projected stat line."""
    total = 0.0
    for key, value in stats.items():
        if key in NON_SCORING_KEYS or key.startswith(NON_SCORING_PREFIXES):
            continue
        weight = scoring.get(key)
        if weight:
            total += weight * value
    return total


def _fallback_points(stats: dict[str, float], fmt: str) -> float:
    return float(stats.get(f"pts_{fmt}", 0.0) or 0.0)


def bye_weeks(schedule: list[dict]) -> dict[str, int]:
    """Each team's bye, derived from the weeks it does not play.

    Taken from the schedule rather than from FantasyPros, which only publishes
    the current season. This works for any year, so byes are available when
    backtesting too.
    """
    played: dict[str, set[int]] = {}
    weeks: set[int] = set()
    for game in schedule:
        try:
            week = int(game.get("week"))
        except (TypeError, ValueError):
            continue
        weeks.add(week)
        for side in ("home", "away"):
            team = game.get(side)
            if team:
                played.setdefault(str(team), set()).add(week)
    byes: dict[str, int] = {}
    for team, seen in played.items():
        missing = sorted(weeks - seen)
        if len(missing) == 1:
            byes[team] = missing[0]
    return byes


def _row_to_player(
    row: dict, pos: str, scoring: dict[str, float], fmt: str,
    byes: dict[str, int] | None = None, adp_key: str | None = None,
) -> Player | None:
    """Turn one Sleeper projection row into a scored Player, or None if empty.

    Shared by the season loader and the weekly one below - the row shape is
    identical, only the ADP and bye lookups differ, and neither applies to a
    single week.
    """
    info = row.get("player") or {}
    stats = {k: float(v) for k, v in (row.get("stats") or {}).items() if v is not None}
    if not stats:
        return None
    points = score_stats(stats, scoring)
    if points <= 0:
        points = _fallback_points(stats, fmt)
    if points <= 0:
        return None
    position = (info.get("position") or pos or "").upper()
    position = POSITION_ALIAS.get(position, position)
    name = " ".join(x for x in (info.get("first_name"), info.get("last_name")) if x).strip()
    team = row.get("team") or info.get("team")
    adp = MISSING_ADP
    if adp_key is not None:
        adp = float(stats.get(adp_key) or stats.get(f"adp_{fmt}") or MISSING_ADP)
    return Player(
        player_id=str(row.get("player_id")),
        name=name or str(row.get("player_id")),
        position=position,
        team=team,
        points=round(points, 1),
        adp=adp,
        bye=(byes or {}).get(str(team or "")),
        injury_status=info.get("injury_status"),
        years_exp=info.get("years_exp"),
        stats=stats,
    )


def _dedupe(players: list[Player]) -> list[Player]:
    """A player can appear under more than one position query; keep the best."""
    unique: dict[str, Player] = {}
    for p in players:
        prior = unique.get(p.player_id)
        if prior is None or p.points > prior.points:
            unique[p.player_id] = p
    return list(unique.values())


def load_players(
    client: SleeperClient, league: LeagueSettings, season: str
) -> list[Player]:
    """Fetch season-long projections for every position the league rosters."""
    scoring = league.scoring_settings
    adp_key = league.adp_key
    fmt = league.scoring_format
    byes = bye_weeks(client.schedule(season))
    players: list[Player] = []

    for pos in league.needed_positions:
        for row in client.projections(season, pos):
            p = _row_to_player(row, pos, scoring, fmt, byes=byes, adp_key=adp_key)
            if p is not None:
                players.append(p)

    result = _dedupe(players)
    _assign_ranks(result)
    attach_risk(result)
    return result


def load_week_points(
    client: SleeperClient, league: LeagueSettings, season: str, week: int
) -> list[Player]:
    """This week's projected points, scored with the league's own settings.

    Distinct from `load_players`: no ADP, no bye, no risk curve. A single
    week is not a season, and the risk model in `outcomes.py` is calibrated to
    season-long rank movement, not week-to-week swing - grafting it onto one
    week would report a false floor and ceiling. `points` here already carries
    `weekly_injury_multiplier`'s game-status discount, since a player ruled Out
    for the week is close to a hard zero rather than a season-long haircut.
    """
    scoring = league.scoring_settings
    fmt = league.scoring_format
    players: list[Player] = []

    for pos in league.needed_positions:
        for row in client.projections(season, pos, week=week):
            p = _row_to_player(row, pos, scoring, fmt)
            if p is None:
                continue
            p.points = round(p.points * weekly_injury_multiplier(p.injury_status), 1)
            players.append(p)

    return _dedupe(players)


def _assign_ranks(players: list[Player]) -> None:
    by_pos: dict[str, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p)
    for group in by_pos.values():
        group.sort(key=lambda p: -p.value)
        for i, p in enumerate(group, start=1):
            p.pos_rank = i


def replacement_levels(
    players: list[Player], ranks: dict[str, int]
) -> dict[str, float]:
    """Projected points of the last startable player at each position."""
    by_pos: dict[str, list[float]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p.points)
    levels: dict[str, float] = {}
    for pos, pts in by_pos.items():
        pts.sort(reverse=True)
        rank = ranks.get(pos, max(1, len(pts) // 4))
        idx = min(rank, len(pts)) - 1
        levels[pos] = pts[idx]
    return levels


def apply_reliability(players: list[Player], replacement: dict[str, float]) -> None:
    """Scale each projection's distance from replacement by how much to believe it."""
    for p in players:
        anchor = replacement.get(p.position)
        if anchor is None:
            continue
        factor = SHRINKAGE.get(p.position, DEFAULT_SHRINKAGE)
        p.shrunk = anchor + factor * (p.points - anchor)


# How much deeper than the last starter the waiver wire sits, in rounds.
#
# Swept against 2024 and 2025 over 60 paired drafts. Points lost per draft
# relative to this value: 0 rounds -14.3 +/- 4.6, a quarter -7.6 +/- 4.2, one
# round -6.5 +/- 3.7, two rounds -29.5 +/- 11.4. A local optimum, not a guess.
WAIVER_DEPTH_ROUNDS = 0.5

# Per-position override, empty by default. Lets a hypothesis test give one
# position a different waiver depth than the shared constant above without
# touching everyone else; see tests/hypotheses.py H9.
WAIVER_DEPTH_BY_POSITION: dict[str, float] = {}


def waiver_levels(
    players: list[Player], ranks: dict[str, int], teams: int
) -> dict[str, float]:
    """What you get at each position if you never draft one.

    Distinct from replacement level, and the distinction matters. Replacement is
    the last *starter* league-wide - the right yardstick for value over
    replacement. But a manager who drafts no quarterback does not end up with
    the tenth-best quarterback; he ends up with whatever nobody drafted, half a
    round deeper. Valuing an empty slot at replacement makes filling it look
    worthless and the engine simply never fills it.
    """
    by_pos: dict[str, list[float]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p.value)
    levels: dict[str, float] = {}
    for pos, values in by_pos.items():
        values.sort(reverse=True)
        depth = WAIVER_DEPTH_BY_POSITION.get(pos, WAIVER_DEPTH_ROUNDS)
        extra = max(1, round(depth * teams))
        rank = ranks.get(pos, max(1, len(values) // 4)) + extra
        levels[pos] = values[min(rank, len(values)) - 1]
    return levels


def apply_vorp(players: list[Player], levels: dict[str, float]) -> None:
    for p in players:
        p.vorp = round(p.points - levels.get(p.position, 0.0), 1)


# ---------- optional overrides from your own projection source ----------


def _norm(name: str) -> str:
    keep = "".join(ch for ch in name.lower() if ch.isalnum() or ch == " ")
    for suffix in (" jr", " sr", " ii", " iii", " iv", " v"):
        if keep.endswith(suffix):
            keep = keep[: -len(suffix)]
    return " ".join(keep.split())


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return [
            {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(fh)
        ]


def _column(row: dict[str, str], *names: str) -> str | None:
    for n in names:
        if row.get(n):
            return row[n]
    return None


def apply_overrides(players: list[Player], path: Path) -> int:
    """Overlay your own projections and/or ADP from a CSV.

    Recognised columns: player/name, pos/position, points/proj/fpts, adp.
    Rows are matched on normalised name, and on position when it is given.
    """
    index: dict[tuple[str, str], Player] = {}
    by_name: dict[str, list[Player]] = {}
    for p in players:
        index[(_norm(p.name), p.position)] = p
        by_name.setdefault(_norm(p.name), []).append(p)

    matched = 0
    for row in _read_csv(path):
        name = _column(row, "player", "name", "player_name")
        if not name:
            continue
        key = _norm(name)
        pos = (_column(row, "pos", "position") or "").upper()
        target = index.get((key, pos)) if pos else None
        if target is None:
            group = by_name.get(key) or []
            target = group[0] if len(group) == 1 else None
        if target is None:
            continue
        pts = _column(row, "points", "proj", "projection", "fpts", "fantasy_points")
        adp = _column(row, "adp", "avg_pick", "rank")
        try:
            if pts is not None:
                target.points = float(pts)
                # Your own projection replaces ours; do not re-shrink it. Leaving
                # a stale shrunk value here would silently value a different
                # player than the one you supplied.
                target.shrunk = None
            if adp is not None:
                target.adp = float(adp)
        except ValueError:
            continue
        matched += 1

    attach_risk(players)
    return matched


# ---------- expert consensus and risk ----------


def attach_risk(players: list[Player]) -> None:
    """Give every player his position's value curve and his rank spread."""
    by_pos: dict[str, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p)
    for pos, group in by_pos.items():
        group.sort(key=lambda p: -p.value)
        curve = [p.value for p in group]
        for i, p in enumerate(group, start=1):
            p.pos_rank = i
            p.curve = curve
            p.spread = total_spread(
                base_spread(p.position, p.years_exp, p.injury_status),
                expert_spread(p.positional_rank_std, i),
            )


def _density(rows: list[Consensus]) -> dict[str, float]:
    """Share of the ranking each position occupies.

    Expert disagreement is published in overall rank units. Converting it to
    positional rank units needs to know how densely the position is packed:
    ten overall ranks might be four receivers but only one tight end.
    """
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.position] = counts.get(r.position, 0) + 1
    total = max(1, len(rows))
    return {pos: max(0.04, n / total) for pos, n in counts.items()}


def apply_consensus(
    players: list[Player], rows: list[Consensus], weight: float = 0.5
) -> int:
    """Blend expert consensus into the projections, then recompute risk.

    Value is blended as a *ranking*, not as points: the league-scored points
    curve is kept and players are re-seated on it according to a blend of
    Sleeper's projection order and the expert consensus order. That adopts the
    crowd's ordering while keeping a realistic distribution of points.
    """
    index = {(_norm(p.name), p.position): p for p in players}
    matched = 0
    density = _density(rows)
    for r in rows:
        target = index.get((_norm(r.name), r.position))
        if target is None:
            continue
        target.ecr = r.ecr
        target.rank_std = r.rank_std
        target.bye = target.bye or r.bye
        target.positional_rank_std = r.rank_std * density.get(r.position, 0.2)
        matched += 1

    by_pos: dict[str, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p)
    for group in by_pos.values():
        group.sort(key=lambda p: -p.points)
        curve = [p.points for p in group]
        sleeper_rank = {p.player_id: i + 1 for i, p in enumerate(group)}
        rated = sorted([p for p in group if p.ecr is not None], key=lambda p: p.ecr or 0.0)
        ecr_rank = {p.player_id: i + 1 for i, p in enumerate(rated)}

        def blended(p: Player, _s=sleeper_rank, _e=ecr_rank) -> float:
            e_rank = _e.get(p.player_id)
            if e_rank is None:
                return _s[p.player_id]
            return weight * e_rank + (1 - weight) * _s[p.player_id]

        for player, points in zip(sorted(group, key=blended), curve):
            player.points = points

    attach_risk(players)
    return matched
