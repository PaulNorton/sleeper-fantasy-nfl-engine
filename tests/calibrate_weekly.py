"""Measure the in-season model's constants against finished seasons.

Reproduces the numbers commented in `waivers.py`: how much of a weekly
projection to believe, how often players miss games and for how long, and how
far a projection wanders from one week to a later one.

    PYTHONPATH=tests .venv/bin/python tests/calibrate_weekly.py

Sleeper keeps one projection per player per week, the last one published
before kickoff. That is exactly "what you would know by then", which is the
quantity the waiver simulation draws.
"""

from __future__ import annotations

import math
import statistics

from sleeper_fantasy_nfl_engine.league import LeagueSettings
from sleeper_fantasy_nfl_engine.projections import POSITION_ALIAS, score_stats
from sleeper_fantasy_nfl_engine.sleeper import SleeperClient
from harness import league_id

SEASONS = ("2024", "2025")
WEEKS = range(1, 18)
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

# A player counts as fantasy-relevant in a week when his projection is at
# least this many points. Below it the projections are for players nobody
# would start, and their noise would swamp the fit.
RELEVANT = {"QB": 15.0, "RB": 7.0, "WR": 7.0, "TE": 5.0, "K": 5.0, "DEF": 4.0}


def _points(row: dict, scoring: dict[str, float]) -> float:
    stats = {k: float(v) for k, v in (row.get("stats") or {}).items() if v is not None}
    return score_stats(stats, scoring) if stats else 0.0


def load(client: SleeperClient, league: LeagueSettings, season: str):
    """week -> player_id -> (position, team, projected, actual, played).

    Built from the stat lines as well as the projections: a player with a
    stat line and no projection still played, and a player with a projection
    and no stat line did not.
    """
    scoring = league.scoring_settings
    table: dict[int, dict[str, tuple]] = {}
    for week in WEEKS:
        rows: dict[str, tuple] = {}
        for pos in POSITIONS:
            for row in client.stats(season, pos, week=week):
                stats = row.get("stats") or {}
                # `gp`, not `gms_active`: a player on injured reserve still
                # gets a stat line with `gms_active` set and nothing else.
                played = bool(stats.get("gp") or pos == "DEF")
                rows[str(row.get("player_id"))] = (
                    pos, row.get("team"), 0.0, _points(row, scoring), played)
            for row in client.projections(season, pos, week=week, ttl=604800):
                pid = str(row.get("player_id"))
                info = row.get("player") or {}
                position = (info.get("position") or pos).upper()
                if POSITION_ALIAS.get(position, position) != pos:
                    continue
                seen = rows.get(pid)
                team = (seen[1] if seen else None) or row.get("team") or info.get("team")
                rows[pid] = (pos, team, _points(row, scoring),
                             seen[3] if seen else 0.0, bool(seen and seen[4]))
        table[week] = rows
    return table


def _cast(rows: dict[str, tuple], pos: str) -> dict[str, set]:
    """team -> the players at this position projected to matter this week."""
    out: dict[str, set] = {}
    for pid, (p, team, proj, _a, _pl) in rows.items():
        if p == pos and team and proj >= RELEVANT[pos]:
            out.setdefault(team, set()).add(pid)
    return out


def slope(xs: list[float], ys: list[float]) -> tuple[float, float]:
    if len(xs) < 30 or len(set(xs)) < 2:
        return float("nan"), float("nan")
    corr = statistics.correlation(xs, ys)
    return corr, corr * statistics.pstdev(ys) / statistics.pstdev(xs)


def main() -> None:
    with SleeperClient() as client:
        league = LeagueSettings.from_api(client.league(league_id()))
        data = {season: load(client, league, season) for season in SEASONS}

    # 1 - reliability. The slope of actual on projected weekly points, among
    # players who played. Injury is measured separately below.
    print("weekly reliability: slope of actual on projected, players who played")
    print(f"{'pos':<5}{'season':>7}{'n':>7}{'corr':>7}{'slope':>7}")
    for pos in POSITIONS:
        both_x: list[float] = []
        both_y: list[float] = []
        for season, table in data.items():
            xs, ys = [], []
            for rows in table.values():
                for p, _team, proj, act, played in rows.values():
                    if p == pos and proj >= RELEVANT[pos] and played:
                        xs.append(proj)
                        ys.append(act)
            corr, b = slope(xs, ys)
            print(f"{pos:<5}{season:>7}{len(xs):>7}{corr:>7.2f}{b:>7.2f}")
            both_x += xs
            both_y += ys
        corr, b = slope(both_x, both_y)
        print(f"{pos:<5}{'both':>7}{len(both_x):>7}{corr:>7.2f}{b:>7.2f}")

    # 2 - injuries. A player who was relevant and played is "hurt" when his
    # team plays its next game and he does not. Hazard is how often that
    # happens. Persistence is how often a player who missed misses again.
    # Transfer is how much of his projection turns up on his teammates at the
    # same position the week he first misses.
    print("\ninjuries: relevant players, team's next game")
    print(f"{'pos':<5}{'played':>8}{'hazard':>8}{'missed':>8}{'persist':>9}"
          f"{'out share':>11}{'transfer':>10}")
    for pos in ("QB", "RB", "WR", "TE", "K"):
        healthy = hurt = stay = back = 0
        moved: list[float] = []
        for table in data.values():
            games: dict[int, set] = {
                wk: {r[1] for r in rows.values() if r[4] and r[1]} for wk, rows in table.items()
            }
            unit: dict[int, dict[str, float]] = {}
            for wk, rows in table.items():
                for r in rows.values():
                    if r[0] == pos and r[1]:
                        unit.setdefault(wk, {})
                        unit[wk][r[1]] = unit[wk].get(r[1], 0.0) + r[2]
            for week in WEEKS:
                for pid, (p, team, proj, _a, played) in table[week].items():
                    if p != pos or proj < RELEVANT[pos] or not played or not team:
                        continue
                    following = [w for w in range(week + 1, 18) if team in games[w]]
                    if not following:
                        continue
                    nxt = following[0]
                    row = table[nxt].get(pid)
                    if row and row[1] and row[1] != team:
                        continue                   # traded or cut and signed elsewhere
                    healthy += 1
                    if row and row[4]:
                        continue
                    hurt += 1
                    before = unit[week].get(team, 0.0) - proj
                    after = unit.get(nxt, {}).get(team, 0.0) - (row[2] if row else 0.0)
                    moved.append((after - before) / proj)
                    for prev, wk in zip(following, following[1:]):
                        row = table[wk].get(pid)
                        if row and row[4]:
                            back += 1
                            break
                        stay += 1
        hazard = hurt / max(1, healthy)
        persist = stay / max(1, stay + back)
        share = hazard / (hazard + 1 - persist)
        transfer = statistics.median(moved) if moved else float("nan")
        print(f"{pos:<5}{healthy:>8}{hazard:>8.3f}{hurt:>8}{persist:>9.2f}"
              f"{share:>11.3f}{transfer:>10.2f}")

    # 3 - drift. How far the projection for a later week sits from the one for
    # this week, on a log scale, for players relevant now. The variance is fitted
    # as noise + drift * k: `noise` is the part that belongs to one week
    # alone (a matchup, a game script) and `drift` is the part that stays (a
    # job won or lost).
    print("\nprojection drift: sd of log(projection k weeks on / projection now)")
    ks = (1, 2, 3, 4, 5, 6, 7, 8)
    print(f"{'pos':<5}" + "".join(f"{f'k={k}':>7}" for k in ks) + f"{'drift':>8}{'noise':>8}")
    for pos in POSITIONS:
        var = []
        for k in ks:
            logs = []
            for table in data.values():
                for week in WEEKS:
                    later = table.get(week + k)
                    if not later:
                        continue
                    cast_now = _cast(table[week], pos)
                    cast_later = _cast(later, pos)
                    for pid, (p, team, proj, _a, _pl) in table[week].items():
                        if p != pos or proj < RELEVANT[pos]:
                            continue
                        # Same relevant teammates in both weeks. A teammate's
                        # injury moves this projection too, and the simulation
                        # models that separately as the injury transfer.
                        if cast_now.get(team, set()) - {pid} != cast_later.get(team, set()) - {pid}:
                            continue
                        nxt = later.get(pid)
                        # Floor at a point: a player who lost the job outright
                        # still has a projection, and log(0) is not a number.
                        if nxt and nxt[2] > 0:
                            logs.append(math.log(max(nxt[2], 1.0) / proj))
            var.append(statistics.pvariance(logs))
        mk, mv = statistics.mean(ks), statistics.mean(var)
        b = sum((k - mk) * (v - mv) for k, v in zip(ks, var)) / sum((k - mk) ** 2 for k in ks)
        a = mv - b * mk
        # Both weeks carry their own noise, so the intercept is twice it.
        print(f"{pos:<5}" + "".join(f"{math.sqrt(v):>7.3f}" for v in var)
              + f"{math.sqrt(max(b, 0.0)):>8.3f}{math.sqrt(max(a, 0.0) / 2):>8.3f}")

    print(
        "\nReliability is conditional on playing; the injury model removes the "
        "weeks a player misses. Drift excludes weeks with no projection at all, "
        "which are byes and absences."
    )


if __name__ == "__main__":
    main()
