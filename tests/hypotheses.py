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
      it costs might be better spent on another skill player. Tested as a ban on
      drafting the position at all, which is the *most* this theory can change -
      any softer version, such as lifting the empty-slot floor toward the best
      player left, can only drive that player's marginal value to zero, and zero
      ties with the deep bench body it would be taken over.
      REJECTED, at every position, and the loss grows with how much the slot is
      worth. Over 240 paired drafts: K and DEF together -3.3 +/- 1.3 (t=-2.61),
      K alone -5.2 +/- 1.1 (t=-4.78), DEF alone -6.2 +/- 1.1 (t=-5.49),
      TE -30.2 +/- 5.2 (t=-5.83), QB -25.0 +/- 3.5 (t=-7.08).

      The control is what makes the result readable. `stream_free` lifts the
      harness rule that forces every team to end with a kicker and a defence,
      and changes nothing else: +0.2 +/- 0.7 (t=0.26). So the rule was never
      binding. The engine already prices an unfilled slot at the waiver wire -
      that is what waiver_levels and the seat() floor are for - and, given the
      board in the last two rounds, it fills those slots by choice. The theory was already implemented; it just does not pay to push
      it further.

      Why filling wins is worth stating, because it is not obvious. A drafted
      starter is floored at the same waiver level as an empty slot, so he can
      only add points, while the player taken instead is a fourteenth-rounder
      sitting below the waiver line, worth roughly nothing at a BENCH_FACTOR of
      0.09. A last-round pick buys a free option on the upside or it buys
      nothing. Note this cuts in favour of drafting, so the backtest is if
      anything biased *toward* the theory it rejects - and the theory still
      loses.
"""

import math
import statistics

from fantasy_draft.backtest import prepare, run
from fantasy_draft.league import LeagueSettings
from fantasy_draft.sleeper import SleeperClient

LEAGUE_ID = "YOUR_LEAGUE_ID"
SEASONS = ("2025", "2024")
STRATEGIES = (
    "greedy", "reach_averse", "ceiling_bench",
    "posbonus_wr", "posbonus_te", "posbonus_qb", "tier_cliff",
    "handcuff", "stream_kd", "stream_k", "stream_def", "stream_te",
    "stream_qb", "stream_free",
)


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
