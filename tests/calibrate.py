"""Measure the model's spread and reliability constants against finished seasons.

Run this to reproduce the numbers commented in outcomes.POSITION_SPREAD and
projections.RELIABILITY, or to re-fit them once another season is in the books:

    PYTHONPATH=tests .venv/bin/python tests/calibrate.py

Reads scoring settings from a real league: $FDRAFT_LEAGUE_ID, or the league
saved in fdraft.json.
"""

import math
import statistics
import sys

from fantasy_draft.backtest import load_actuals
from fantasy_draft.league import LeagueSettings
from fantasy_draft.outcomes import POSITION_SPREAD, RANK_OFFSET
from fantasy_draft.projections import SHRINKAGE, load_players
from fantasy_draft.sleeper import SleeperClient
from harness import league_id

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
        league = LeagueSettings.from_api(client.league(league_id()))
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


def sweep() -> None:
    """Re-derive the swept constants by replaying finished seasons.

    Every configuration faces identical drafts, so differences are paired and a
    few points of signal are visible against seasons that swing by hundreds.
    Slow: each configuration replays 60 full drafts.
    """
    import math
    import statistics

    from fantasy_draft import projections, valuation
    from fantasy_draft.backtest import prepare, run

    with SleeperClient() as client:
        league = LeagueSettings.from_api(client.league(league_id()))
        data = {s: prepare(client, league, s) for s in SEASONS}

    def points() -> list[float]:
        out: list[float] = []
        for _season, (players, replacement, actual) in data.items():
            res = run(
                league, players, replacement, actual, reps=3,
                slots=list(range(1, league.teams + 1)), strategies=("greedy",),
            )
            out.extend(res["greedy"].points)
        return out

    def against(reference: list[float]) -> tuple[float, float, float]:
        current = points()
        diffs = [a - b for a, b in zip(current, reference)]
        mean = statistics.mean(diffs)
        err = statistics.stdev(diffs) / math.sqrt(len(diffs))
        return mean, err, (mean / err if err else 0.0)

    reference = points()
    print(f"\nreference: shipped constants, {len(reference)} paired drafts")

    print(f"\nwaiver depth (rounds below the last starter)\n{'value':>8}{'diff':>8}{'+/-':>7}{'t':>7}")
    for rounds in (0.0, 0.25, 1.0, 2.0):
        projections.WAIVER_DEPTH_ROUNDS = rounds
        m, e, t = against(reference)
        print(f"{rounds:>8.2f}{m:>8.1f}{e:>7.1f}{t:>7.2f}")
    projections.WAIVER_DEPTH_ROUNDS = 0.5

    print(f"\nbench factor multiplier\n{'value':>8}{'diff':>8}{'+/-':>7}{'t':>7}")
    original = dict(valuation.BENCH_FACTOR)
    for mult in (0.0, 0.5, 3.0, 8.0):
        valuation.BENCH_FACTOR.update({k: v * mult for k, v in original.items()})
        m, e, t = against(reference)
        print(f"{mult:>8.1f}{m:>8.1f}{e:>7.1f}{t:>7.2f}")
    valuation.BENCH_FACTOR.update(original)


if __name__ == "__main__":
    main()
    if "--sweep" in sys.argv:
        sweep()


