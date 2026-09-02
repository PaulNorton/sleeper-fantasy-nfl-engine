"""Record of three strategy-improvement hypotheses tested against the shipped
lineup strategy, each as a paired backtest over 2024 and 2025.

Run: PYTHONPATH=tests .venv/bin/python tests/hypotheses.py

H1 - Robust RB: prioritise running backs in the first few rounds beyond their
     raw marginal value, on the wisdom that touch-share concentration and
     in-season attrition make a bell-cow scarcer than his projection implies.
     KEPT. +3.1 +/- 1.1 points over 280 paired drafts, t = 2.79. Folded into
     the shipped `lineup` strategy as ROBUST_RB_BONUS in recommend.py.

H2 - Reach-aversion: discount a pick by how far it reaches past his ADP, to
     hedge model error on any one player against market consensus.
     REJECTED. -7.3 +/- 3.6 points over 120 paired drafts, t = -2.04. The
     model's own edge over ADP is exactly what a value model is for; pulling
     picks back toward consensus gives some of that edge away.

H3 - Ceiling-chasing once bench-bound: once a roster fills every starter slot,
     score further picks on ceiling instead of mean value, since the downside
     of a bench pick is already covered by the waiver floor.
     NO SIGNAL. +2.9 +/- 2.5 points over 120 paired drafts, t = 1.13. Plausible
     but not distinguishable from noise at this sample size; not shipped.
"""

import math
import statistics

from fantasy_draft.backtest import prepare, run
from fantasy_draft.league import LeagueSettings
from fantasy_draft.sleeper import SleeperClient

LEAGUE_ID = "YOUR_LEAGUE_ID"
SEASONS = ("2025", "2024")
STRATEGIES = ("greedy", "reach_averse", "ceiling_bench")


def main() -> None:
    with SleeperClient() as client:
        league = LeagueSettings.from_api(client.league(LEAGUE_ID))
        pooled: dict[str, list[float]] = {s: [] for s in STRATEGIES}
        for season in SEASONS:
            players, replacement, actual = prepare(client, league, season)
            res = run(
                league, players, replacement, actual, reps=6,
                slots=list(range(1, league.teams + 1)), strategies=STRATEGIES,
            )
            print(f"{season}: " + "  ".join(f"{s} {res[s].mean_points:.0f}" for s in STRATEGIES))
            for s in STRATEGIES:
                pooled[s].extend(res[s].points)

        print(f"\npooled {len(pooled['greedy'])} paired drafts, baseline = greedy (shipped lineup)")
        for h in ("reach_averse", "ceiling_bench"):
            diffs = [a - b for a, b in zip(pooled[h], pooled["greedy"])]
            mean = statistics.mean(diffs)
            err = statistics.stdev(diffs) / math.sqrt(len(diffs))
            print(f"  {h:<14} {mean:+7.1f} +/- {err:.1f}  t={mean / err:6.2f}")
        print(
            "\nRobust RB (H1) is folded into the shipped strategy, so it cannot be "
            "isolated this way any more; see recommend.ROBUST_RB_BONUS for its "
            "confirmed effect size."
        )


if __name__ == "__main__":
    main()
