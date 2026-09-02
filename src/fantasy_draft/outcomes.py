"""Ranges of outcomes: how wrong a projection is likely to be, and which way.

A projection is a mean. Two players with the same mean are not the same asset.
A rookie with an unsettled role busts more often *and* returns league-winning
seasons more often than a settled veteran with the same projection.

Uncertainty is modelled in **rank** space, not points, because that is where it
behaves sensibly. A player's realised finish is

    rank = (projected_rank + OFFSET) * exp(spread * Z) - OFFSET

and his points are then read off the position's value curve. Three things fall
out of this that a spread in points space gets wrong:

  - **Skew flips with quality, as it should.** The value curve is steep at the
    top and flat at the bottom. The projected RB1 has little room above him and
    a long way to fall, so his range is left-skewed. A projected RB40 has a
    bounded floor and a long way to climb, so his range is right-skewed. This
    is the rookie's "low floor, high ceiling", and it is not hand-coded.
  - **Ceilings stay realistic.** Upside is capped by the top of the curve
    rather than running off to point totals nobody has ever scored.
  - **Rank moves multiplicatively.** Finishing 10 places worse matters enormously
    for the RB2 and barely at all for the RB50, which matches how ranks move.

Spread has two independent sources, added in quadrature: irreducible season
variance (injury, game script, touchdown luck) and expert disagreement, taken
from the standard deviation of FantasyPros expert ranks. When 100+ analysts
cannot agree where a player belongs, his range of outcomes is genuinely wider.

The spread constants below are judgement, not fitted to data. They are grouped
here so they are easy to find and easy to argue with.
"""

from __future__ import annotations

import math
from statistics import NormalDist

NORM = NormalDist()

# Softens the multiplicative model at the very top of a position, where the
# difference between rank 1 and rank 2 is not a factor of two in anything.
RANK_OFFSET = 8.0

# Log-scale of rank movement over a season, by position. Quarterbacks hold
# their rank best; kickers and defences are close to noise year on year.
POSITION_SPREAD = {
    "QB": 0.55,
    "RB": 0.75,
    "WR": 0.72,
    "TE": 0.75,
    "K": 0.85,
    "DEF": 0.85,
    "DL": 0.70,
    "LB": 0.70,
    "DB": 0.70,
}
DEFAULT_SPREAD = 0.72

ROOKIE_MULTIPLIER = 1.25

INJURY_MULTIPLIER = {
    "IR": 1.45,
    "PUP": 1.40,
    "NA": 1.40,
    "Sus": 1.35,
    "Out": 1.25,
    "Doubtful": 1.12,
    "Questionable": 1.02,
}

MAX_SPREAD = 1.6

# How much a player can beat the best projection at his position. Applied per
# rank of headroom above first, so the projected QB1 and QB6 do not collapse
# onto the same ceiling.
EXTENSION_SLOPE = 0.045


def base_spread(
    position: str, years_exp: int | None, injury_status: str | None
) -> float:
    """Irreducible rank movement, before expert disagreement."""
    spread = POSITION_SPREAD.get(position, DEFAULT_SPREAD)
    if years_exp == 0:
        spread *= ROOKIE_MULTIPLIER
    if injury_status:
        spread *= INJURY_MULTIPLIER.get(injury_status, 1.15)
    return spread


def expert_spread(rank_std_positional: float, pos_rank: float) -> float:
    """Expert disagreement, converted to the same log-rank scale."""
    if rank_std_positional <= 0:
        return 0.0
    return rank_std_positional / (pos_rank + RANK_OFFSET)


def total_spread(base: float, expert: float) -> float:
    return min(MAX_SPREAD, math.hypot(base, expert))


def realised_rank(pos_rank: float, spread: float, z: float) -> float:
    """Where a player actually finishes, given a standard normal draw."""
    moved = (pos_rank + RANK_OFFSET) * math.exp(spread * z) - RANK_OFFSET
    # The asymptote of the transform; a rank cannot beat it however good the draw.
    return max(-RANK_OFFSET + 0.5, moved)


def curve_value(curve: list[float], rank: float) -> float:
    """Points at a fractional positional rank, interpolating the value curve.

    Above rank 1 the curve is extended a little, since a player can outscore
    what the best projection at his position called for.
    """
    if not curve:
        return 0.0
    if rank < 1.0:
        return curve[0] * (1.0 + EXTENSION_SLOPE * (1.0 - rank))
    i = min(rank - 1.0, len(curve) - 1.0)
    lo = int(i)
    hi = min(lo + 1, len(curve) - 1)
    frac = i - lo
    return curve[lo] * (1 - frac) + curve[hi] * frac


def outcome(
    curve: list[float],
    pos_rank: float,
    spread: float,
    z: float,
    mean: float | None = None,
) -> float:
    """One season's points, given a standard normal draw.

    Passing `mean` rescales the draw so the median lands on that number. The
    curve and the player's rank can drift apart whenever a projection is edited
    - a CSV override, a manual adjustment - and without this anchor the drawn
    season would quietly describe a different player than the one being valued.
    """
    value = curve_value(curve, realised_rank(pos_rank, spread, z))
    if mean is not None:
        anchor = curve_value(curve, pos_rank)
        if anchor > 0:
            return value * mean / anchor
        return mean
    return value


def percentile(
    curve: list[float],
    pos_rank: float,
    spread: float,
    q: float,
    mean: float | None = None,
) -> float:
    """The q-th percentile season. Note a *better* season is a *lower* rank."""
    return outcome(curve, pos_rank, spread, -NORM.inv_cdf(q), mean)
