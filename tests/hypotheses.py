"""Record of three strategy-improvement hypotheses tested against the shipped
lineup strategy, each as a paired backtest over 2024 and 2025.

Run: PYTHONPATH=tests .venv/bin/python tests/hypotheses.py

Reads scoring settings from a real league: $FDRAFT_LEAGUE_ID, or the league
saved in fdraft.json.

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

H4 - Does Robust RB generalise? Same bonus (15%, first four rounds), applied to
     WR, TE and QB instead of RB, to test whether front-loading any position
     helps or whether the RB effect is specific.
     REJECTED for all three, and informative: WR -9.0 +/- 2.6 (t=-3.42, clear
     loss), TE -7.9 +/- 3.7 (t=-2.13, suggestive loss), QB -2.2 +/- 4.2
     (t=-0.52, no effect). Robust RB is not an artifact of generically
     rewarding early picks - the other three positions do not show it, and two
     of them lose money from it.

H5 - Tier-cliff bonus: reward taking the last player in his gap-based tier -
     the one right before a real drop to the next echelon at his position -
     using the same tiers already shown in the "Tier" column. Distinct from
     vona: a static gap check, no pick-timing or availability estimate.
     NO SIGNAL. +2.7 +/- 4.6 points over 120 paired drafts, t = 0.60.

H6 - Robust RB parameter sweep: is (4 rounds, 15% bonus) actually the best
     setting, or just the first one tried?
     NO IMPROVEMENT FOUND. Swept rounds in {2,3,5,6} and bonus in
     {0.08,0.10,0.20,0.30}, plus two other combinations, all paired against the
     shipped setting over 80 drafts each. Best alternative was (5 rounds, 15%)
     at +4.1 +/- 2.8 (t=1.49) - not significant. The shipped values sit at a
     flat local optimum; left unchanged.

H7 - Handcuffing: bonus a backup running back who shares a team with an RB
     already on the roster and is clearly not his committee-mate, on the theory
     that he is the direct beneficiary if the starter is hurt.
     REJECTED. -2.8 +/- 1.1 points over 280 paired drafts, t = -2.53 (screened
     at -5.2 +/- 2.2, t = -2.42, then confirmed at higher n). Plausible in
     theory; the model's own marginal-value ranking already does better than
     forcing a handcuff pick, most likely because a handcuff is a bench slot
     that is a bad bet most seasons and only pays off in the one where the
     starter is actually hurt, which the waiver floor already prices reasonably
     without a manual override.

H8 - Draft-run detection: boost a position's value when it is being taken
     faster than its fair share of league-wide demand in the actual draft
     underway, rather than from preseason ADP. Only tested where it has
     anything to exploit: against --opponent need, since ADP bots create no
     real runs by construction.
     NO SIGNAL. -4.2 +/- 3.0 points over 120 paired drafts, t = -1.43, even
     with opponents capable of producing genuine runs to detect.

By this point every decision-time heuristic layered on top of marginal value
had lost except Robust RB, which won for one position only. That pattern
suggested Robust RB was not "prioritising RB is a good heuristic" so much as
compensating for something the *input calibration* under-represents for RB
specifically - which would mean the more honest fix lives in the data layer
(waiver depth, bench value, shrinkage), not in another decision-time bonus.
Three more hypotheses tested that directly, sweeping existing calibration
constants for RB alone rather than adding new overlays. None replicated or
improved on the shipped bonus.

H9 - RB-specific waiver depth: give running backs their own depth-below-
     replacement instead of sharing the global WAIVER_DEPTH_ROUNDS with every
     other position.
     NO IMPROVEMENT FOUND. Swept 0.15 to 1.5 rounds against the shared 0.5;
     every value was flat or worse (best: 0.75 rounds at +1.3 +/- 2.3, t=0.56).

H10 - RB-specific bench factor: raise it, on the theory that RB bench depth
      deserves more credit than the shared BENCH_FACTOR curve gives it -
      motivated directly by handcuffing's failure, to see if a more general
      version of the same idea (without requiring a same-team match) fares
      better.
      REJECTED, and informatively backwards. Every value above the shipped 0.09
      was clearly worse (0.30: t=-4.02). Lowering it to 0.0 looked promising at
      first (+5.3 +/- 3.4 at 100 drafts, then +4.3 +/- 2.0 at 280, t=2.15) but
      regressed to +2.1 +/- 1.4 (t=1.51) at 600 paired drafts - a textbook false
      positive shrinking toward zero under more scrutiny. Left unchanged.

H11 - RB shrinkage uncapping: SHRINKAGE["RB"] was capped at 1.00 out of caution
      even though the measured regression slope was 1.11. If Robust RB's win
      were really a calibration gap, raising this should show it.
      NO IMPROVEMENT FOUND. Swept 0.85 to 1.30 against the shipped 1.00; the
      closest alternative (1.30) was flat (t=0.18), everything else worse
      (0.85: t=-2.09).

The data-layer explanation does not hold up. Robust RB stands as a genuine,
position-specific exception to "trust the marginal-value system," not a proxy
for a calibration constant that could replace it.

H12 - Leave a starter slot empty and stream it. You do not have to fill every
      role: an unfilled slot can be covered off waivers all season, so the pick
      it costs might be better spent on another skill player.
      SPLIT VERDICT, and the split tracks projection skill.

      Punting a slot whose projections carry information is expensive. Over 240
      paired drafts, TE -30.2 +/- 5.2 (t=-5.83) and QB -25.0 +/- 3.5 (t=-7.08).

      Skipping the defence pays: +3.6 +/- 0.8 (t=4.39) over 240 paired drafts.
      Skipping the kicker is neutral: swapping him for the best free skill
      player, holding the rest of the roster fixed, is +2.8 +/- 1.8 (t=1.56).

      Getting that right needs a control, and the first version of this test did
      not have one. A strategy that skips the defence frees its round-14 pick and
      takes its kicker a round early, drawing from a pool of about thirty kickers
      whose realised seasons swing 40 points and whose projections predict
      nothing (SHRINKAGE["K"] = 0.15, measured correlation -0.13). Measured
      directly: the two arms drafted kickers with near-identical projections
      (72.1 vs 72.8) whose actual seasons differed by 41 points. That artifact is
      larger than the effect under test, and it reversed its sign - the
      uncontrolled comparison read -6.2 +/- 1.1 against skipping. Holding the
      kicker to the final round in both arms (stream_def_lateK, verified to draft
      the same kicker in 10 of 10 seats) gives the +3.6 above.

      Any experiment that shifts what happens in rounds 14-15 is exposed to this.
      A sweep of SHRINKAGE["DEF"] appeared to show that trusting defence
      projections less costs 5 points; it does not. Lowering the constant makes
      the engine take its kicker at 14, after which _forced_fill supplies a
      defence at 15 anyway, so the sweep measured the kicker lottery too.

      The cause is not miscalibration. At shrinkage 0.0 every defence is worth
      exactly replacement level and the engine still drafts one, because by the
      final picks every candidate has a marginal value near zero and max() breaks
      the tie by board order. Measured with the roster held fixed, the defence
      pick is worth 1.4 blended points and the last skill pick 8.4 - the model is
      indifferent where the season is not, and indifference chooses badly.

      Not shipped yet: the fix is a tie-break among near-zero candidates, not a
      constant, and the +3.6 rests on scoring an unfilled defence slot at the
      10th-best defence's actual season - the assumption that streaming reaches
      DEF10. That is the least validated number in the comparison and it is the
      "in-season additions" gap the README already flags.
"""

import math
import statistics

from fantasy_draft.backtest import prepare, run
from fantasy_draft.league import LeagueSettings
from fantasy_draft.sleeper import SleeperClient
from harness import league_id

SEASONS = ("2025", "2024")
STRATEGIES = (
    "greedy", "reach_averse", "ceiling_bench",
    "posbonus_wr", "posbonus_te", "posbonus_qb", "tier_cliff",
    "handcuff", "stream_kd", "stream_k", "stream_def", "stream_te",
    "stream_qb", "stream_free", "stream_def_lateK",
)


def main() -> None:
    with SleeperClient() as client:
        league = LeagueSettings.from_api(client.league(league_id()))
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
        for h in STRATEGIES[1:]:
            diffs = [a - b for a, b in zip(pooled[h], pooled["greedy"])]
            mean = statistics.mean(diffs)
            err = statistics.stdev(diffs) / math.sqrt(len(diffs))
            print(f"  {h:<14} {mean:+7.1f} +/- {err:.1f}  t={mean / err:6.2f}")
        print(
            "\nRobust RB (H1) is folded into the shipped strategy, so it cannot be "
            "isolated this way any more; see recommend.ROBUST_RB_BONUS for its "
            "confirmed effect size."
        )
        print(
            "run_aware (H8) needs --opponent need to have anything to exploit and "
            "is not included in this pooled comparison; see the module docstring."
        )


if __name__ == "__main__":
    main()
