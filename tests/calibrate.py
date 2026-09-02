"""Measure the model's spread and reliability constants against finished seasons.

Run this to reproduce the numbers commented in outcomes.POSITION_SPREAD and
projections.RELIABILITY, or to re-fit them once another season is in the books:

    PYTHONPATH=tests .venv/bin/python tests/calibrate.py
"""

import math
import statistics

from fantasy_draft.backtest import load_actuals
from fantasy_draft.league import LeagueSettings
from fantasy_draft.outcomes import POSITION_SPREAD, RANK_OFFSET
from fantasy_draft.projections import SHRINKAGE, load_players
from fantasy_draft.sleeper import SleeperClient

SEASONS = ("2024", "2025")
MAX_ADP = 250  # deep projections are noise and would distort the fit


def measure(league: LeagueSettings, players, actual):
    out = {}
    by_pos = {}
    for p in players:
        if p.adp < MAX_ADP:
            by_pos.setdefault(p.position, []).append(p)
    for pos, group in by_pos.items():
        if len(group) < 12:
            continue
        group.sort(key=lambda p: -p.points)
        proj_rank = {p.player_id: i + 1 for i, p in enumerate(group)}
        real = sorted(group, key=lambda p: -actual.get(p.player_id, 0.0))
        act_rank = {p.player_id: i + 1 for i, p in enumerate(real)}
        logs = [
            math.log((act_rank[p.player_id] + RANK_OFFSET) / (proj_rank[p.player_id] + RANK_OFFSET))
            for p in group
        ]
        xs = [p.points for p in group]
        ys = [actual.get(p.player_id, 0.0) for p in group]
        corr = statistics.correlation(xs, ys) if len(set(ys)) > 1 else float("nan")
        sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
        slope = corr * sy / sx if sx else 0.0
        out[pos] = (len(group), corr, statistics.pstdev(logs), slope)
    return out


def main() -> None:
    with SleeperClient() as client:
        league = LeagueSettings.from_api(client.league("YOUR_LEAGUE_ID"))
        seasons = {}
        for season in SEASONS:
            players = load_players(client, league, season)
            seasons[season] = measure(league, players, load_actuals(client, league, season))

    print(f"{'pos':<5}{'season':>7}{'n':>5}{'corr':>8}{'slope':>8}{'spread':>9}")
    agg: dict[str, list[tuple[float, float, float]]] = {}
    for season, table in seasons.items():
        for pos, (n, corr, sd, slope) in sorted(table.items()):
            print(f"{pos:<5}{season:>7}{n:>5}{corr:>8.2f}{slope:>8.2f}{sd:>9.2f}")
            agg.setdefault(pos, []).append((corr, sd, slope))

    print(
        f"\n{'pos':<5}{'corr':>8}{'slope':>8}{'shrink':>9}{'spread':>9}{'in model':>10}"
    )
    for pos, vals in sorted(agg.items()):
        corr = sum(c for c, _, _ in vals) / len(vals)
        sd = sum(s for _, s, _ in vals) / len(vals)
        slope = sum(b for _, _, b in vals) / len(vals)
        print(
            f"{pos:<5}{corr:>8.2f}{slope:>8.2f}{SHRINKAGE.get(pos, 0):>9.2f}"
            f"{sd:>9.2f}{POSITION_SPREAD.get(pos, 0):>10.2f}"
        )
    print(
        "\nA near-zero correlation makes the slope meaningless; K and DEF are "
        "shrunk on the correlation instead."
    )
    print(
        "\nMeasured spreads are lower bounds: rank movement is capped by the size "
        "of the ranked pool."
    )


if __name__ == "__main__":
    main()
